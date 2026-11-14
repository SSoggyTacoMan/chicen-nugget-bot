"""Slack event glue for the AI agent."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from bot.ai.limits import user_retry_after
from bot.runtime import runtime
from bot.slack_api import (
    channel_info,
    fetch_slack_images,
    post,
    post_ephemeral,
    react,
    transcribe_slack_audio,
    unreact,
    user_info,
)
from bot.utils.text import (
    chunk_text,
    disarm_broadcasts,
    display_name,
    format_mrkdwn,
    has_owner_signature,
    incoming_text,
)

logger = logging.getLogger(__name__)

# channel_id -> monotonic time of the last unprompted reply, so the gate saying
# yes five times in a row doesn't turn into five replies in a row.
_last_ambient: dict[str, float] = {}
# (channel, thread_ts) -> monotonic time of the last unprompted thread reply, so
# a busy thread can't turn into a monologue.
_last_thread: dict[tuple[str, str], float] = {}
# (channel, thread_ts) -> monotonic time of the last UNSUBSCRIBE gag, so a
# thread can't turn into a resubscribe loop.
_last_unsubscribe: dict[tuple[str, str], float] = {}
_UNSUBSCRIBE_COOLDOWN = 15.0
_UNSUBSCRIBE_WORDS = {"unsubscribe", "unsub"}
_REACTION_COOLDOWN = 30.0
_last_reaction: dict[tuple[str, str, str, str], float] = {}
_NAME_ADDRESS = (
    re.compile(r"^\s*(?:hey|yo|hi|ok|okay|so|alright)?\s*(?:chicen\s+nugget|chicen|nugget)\b", re.IGNORECASE),
    re.compile(r"\b(?:chicen\s+nugget|chicen)\b", re.IGNORECASE),
    re.compile(r"\bnugget\s*[,?!:]", re.IGNORECASE),
)
_REQUEST_RE = re.compile(
    r"\b(?:can|could|would|will)\s+you\b|"
    r"\b(?:help|check|look at|fix|review|explain|thoughts|opinion|advice|debug)\b",
    re.IGNORECASE,
)
_names: dict[str, str] = {}


async def on_message(event: dict[str, Any], client) -> None:
    subtype = event.get("subtype")
    if (subtype and subtype != "file_share") or event.get("bot_id"):
        return
    if has_owner_signature(event.get("text") or ""):
        return
    user_id = event.get("user")
    if not user_id or user_id == runtime.bot_user_id or (runtime.user_id and user_id == runtime.user_id):
        return

    channel_type = event.get("channel_type")
    if channel_type == "im":
        await _handle_dm(event, client)
        return
    if channel_type == "mpim":
        await _handle_mpim(event, client)
        return
    if channel_type in {"channel", "group"}:
        if await _handle_unsubscribe(event, client):
            return
        if await _handle_named_mention(event, client):
            return
        await _handle_thread_continuation(event, client)
        await _maybe_ambient(event, client)


async def on_app_mention(event: dict[str, Any], client) -> None:
    subtype = event.get("subtype")
    if (subtype and subtype != "file_share") or event.get("bot_id"):
        return
    if has_owner_signature(event.get("text") or ""):
        return
    user_id = event.get("user")
    if not user_id or user_id == runtime.bot_user_id or (runtime.user_id and user_id == runtime.user_id):
        return
    channel = event.get("channel") or ""
    # DM mentions are handled by the message event.
    if channel.startswith("D"):
        return
    if not _ai_allowed(event):
        return
    images = await fetch_slack_images(event, runtime.settings.slack_bot_token)
    voice_transcripts = await transcribe_slack_audio(
        event, runtime.settings.slack_bot_token, runtime.settings
    )
    text = incoming_text(
        event.get("text") or "",
        bot_user_id=runtime.bot_user_id,
        assistant_user_id=runtime.user_id or "",
    )
    if voice_transcripts:
        voice_block = "\n".join(voice_transcripts)
        text = (
            f"{text}\n\n[Voice message]: {voice_block}".strip()
            if text
            else f"[Voice message]: {voice_block}"
        )
    if not text and images:
        text = "What is in this image?"
    await _handle_channel(
        event,
        client,
        text=text,
        trigger="mpim_mention" if event.get("channel_type") == "mpim" else "mention",
        images=images,
    )


async def on_reaction_added(event: dict[str, Any], client) -> None:
    """Small reaction shortcuts: 🔍 research, 🧵 summarize, 📌 pin."""
    item = event.get("item") or {}
    if item.get("type") != "message":
        return
    reaction = str(event.get("reaction") or "").lower().strip()
    if reaction not in {"mag", "thread", "pushpin"}:
        return
    channel = item.get("channel") or ""
    ts = item.get("ts") or ""
    user_id = event.get("user") or ""
    item_user = event.get("item_user") or ""
    if not channel or not ts or not user_id:
        return
    if user_id in {runtime.bot_user_id, runtime.user_id}:
        return
    if item_user in {runtime.bot_user_id, runtime.user_id}:
        return
    is_dm = channel.startswith("D")
    if is_dm:
        allowed = user_id == runtime.settings.owner_user_id or (
            runtime.settings.ai_allow_admins and runtime.settings.is_admin(user_id)
        )
        if not allowed:
            return
    elif not runtime.settings.ai_allowed(channel):
        return
    if reaction == "pushpin":
        if not (
            user_id == runtime.settings.owner_user_id
            or runtime.settings.is_admin(user_id)
        ):
            return
        try:
            await client.pins_add(channel=channel, timestamp=ts)
        except Exception as exc:  # noqa: BLE001
            logger.info("could not pin %s/%s: %s", channel, ts, exc)
            return
        await react(client, channel, ts, "white_check_mark")
        return
    if runtime.agent is None or not runtime.settings.ai_enabled:
        return
    if user_retry_after(user_id):
        return

    now = time.monotonic()
    key = (channel, ts, reaction, user_id)
    if now - _last_reaction.get(key, 0.0) < _REACTION_COOLDOWN:
        return
    _last_reaction[key] = now

    text, root_ts = await _reaction_message(client, channel, ts)
    if not text:
        return

    if reaction == "mag":
        prompt = (
            "someone reacted with a magnifying glass, meaning: research this message "
            "and reply with the useful answer. keep it short.\n\n" + text
        )
    else:
        prompt = (
            "someone reacted with a thread emoji, meaning: summarize this thread in "
            "a few lines.\n\n" + text
        )
    conversation = f"dm:{user_id}" if is_dm else f"ch:{channel}:{root_ts}"
    try:
        async with runtime.lock(conversation):
            answer = await runtime.agent.reply(
                conversation=conversation,
                user_id=user_id,
                text=prompt[:6000],
                channel_id=channel,
                thread_ts=None if is_dm else root_ts,
                is_dm=is_dm,
                trigger="reaction",
                client=client,
            )
        if not answer or answer.strip() in {"[NO_REPLY]", "NO_REPLY"}:
            return
        for chunk in chunk_text(disarm_broadcasts(format_mrkdwn(answer))):
            await post(client, channel, chunk, thread_ts=None if is_dm else root_ts)
        if not is_dm and runtime.db:
            runtime.db.mark_thread(channel, root_ts, owner="bot")
    except Exception:
        logger.exception("reaction agent turn failed")


async def _reaction_message(
    client, channel: str, ts: str
) -> tuple[str, str]:
    """Return (text, thread root) for the message behind a reaction."""
    for call in (
        lambda: client.conversations_history(
            channel=channel, latest=ts, inclusive=True, limit=1
        ),
        lambda: client.conversations_replies(channel=channel, ts=ts, limit=1),
    ):
        try:
            response = await call()
        except Exception:
            logger.debug("could not read the reacted message", exc_info=True)
            continue
        messages = response.get("messages") or []
        if not messages:
            continue
        message = messages[0]
        text = (message.get("text") or "").strip()
        if not text:
            continue
        return text, message.get("thread_ts") or message.get("ts") or ts
    return "", ts


async def _handle_dm(event: dict[str, Any], client) -> None:
    settings = runtime.settings
    user_id = event["user"]
    allowed = user_id == settings.owner_user_id or (
        settings.ai_allow_admins and settings.is_admin(user_id)
    )
    if not allowed:
        logger.info("ignored AI dm from non-admin %s", user_id)
        return
    if runtime.agent is None or not settings.ai_enabled:
        await post(
            client,
            event["channel"],
            "my ai brain isn't configured. add `AI_API_KEY` (and optionally `AI_BASE_URL` / `AI_MODEL`) "
            "to my .env and restart me.",
        )
        return

    images = await fetch_slack_images(event, settings.slack_bot_token)
    voice_transcripts = await transcribe_slack_audio(
        event, settings.slack_bot_token, settings
    )
    text = (event.get("text") or "").strip()
    if voice_transcripts:
        voice_block = "\n".join(voice_transcripts)
        text = (
            f"{text}\n\n[Voice message]: {voice_block}".strip()
            if text
            else f"[Voice message]: {voice_block}"
        )
    if not text and not images:
        return
    if not text and images:
        text = "Describe or look at this image."
    retry = user_retry_after(user_id)
    if retry:
        await post(
            client,
            event["channel"],
            f"slow down a lil, try again in {retry}s :3",
            thread_ts=event.get("thread_ts"),
        )
        return
    message_ts = event.get("ts")
    reply_thread_ts = event.get("thread_ts") or None
    if message_ts:
        await react(client, event["channel"], message_ts, "eyes")
    conversation = f"dm:{user_id}"
    try:
        async with runtime.lock(conversation):
            try:
                answer = await runtime.agent.reply(
                    conversation=conversation,
                    user_id=user_id,
                    text=text,
                    channel_id=event.get("channel"),
                    thread_ts=reply_thread_ts,
                    is_dm=True,
                    trigger="dm",
                    images=images,
                )
            except Exception:
                logger.exception("dm agent turn failed")
                answer = "something broke on my side. check my logs?"
        for chunk in chunk_text(disarm_broadcasts(format_mrkdwn(answer))):
            await post(client, event["channel"], chunk, thread_ts=reply_thread_ts)
    finally:
        if message_ts:
            await unreact(client, event["channel"], message_ts, "eyes")


def _ai_allowed(event: dict[str, Any]) -> bool:
    """Group DMs are direct-ish spaces, so don't require an AI_CHANNELS entry."""
    if event.get("channel_type") == "mpim":
        return True
    return runtime.settings.ai_allowed(event.get("channel") or "")


async def _handle_mpim(event: dict[str, Any], client) -> None:
    """Group DMs: mentions and bot-owned thread followups only, never ambient."""
    channel = event.get("channel") or ""
    text = (event.get("text") or "").strip()
    if not channel or not text:
        return
    if runtime.bot_user_id and f"<@{runtime.bot_user_id}>" in text:
        return  # app_mention owns this one
    if runtime.user_id and f"<@{runtime.user_id}>" in text:
        return  # Boi owns this one
    thread_ts = event.get("thread_ts")
    if not thread_ts or runtime.db is None:
        return
    if not runtime.db.is_thread(channel, thread_ts, owner="bot"):
        return
    if runtime.db.kv_get("config:thread_followups") == "false":
        return
    await _handle_thread_continuation(event, client)


def _is_unsubscribe(text: str | None) -> bool:
    cleaned = " ".join((text or "").split()).strip("!.?,;:")
    return cleaned.lower() in _UNSUBSCRIBE_WORDS


def _looks_named(text: str) -> bool:
    """True when a channel message addresses the bot by name instead of @."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _NAME_ADDRESS)


async def _handle_named_mention(event: dict[str, Any], client) -> bool:
    """Treat "hey nugget" like a real mention, without requiring the @."""
    channel = event.get("channel") or ""
    text = (event.get("text") or "").strip()
    if not channel or not text or not runtime.settings.ai_allowed(channel):
        return False
    if runtime.bot_user_id and f"<@{runtime.bot_user_id}>" in text:
        return False  # app_mention owns this one
    if runtime.user_id and f"<@{runtime.user_id}>" in text:
        return False  # the user gateway owns this one
    if not _looks_named(text):
        return False

    images = await fetch_slack_images(event, runtime.settings.slack_bot_token)
    voice_transcripts = await transcribe_slack_audio(
        event, runtime.settings.slack_bot_token, runtime.settings
    )
    clean_text = incoming_text(
        text,
        bot_user_id=runtime.bot_user_id,
        assistant_user_id=runtime.user_id or "",
    )
    if voice_transcripts:
        voice_block = "\n".join(voice_transcripts)
        clean_text = (
            f"{clean_text}\n\n[Voice message]: {voice_block}".strip()
            if clean_text
            else f"[Voice message]: {voice_block}"
        )
    if not clean_text and images:
        clean_text = "What is in this image?"

    await _handle_channel(
        event, client, text=clean_text, trigger="named", images=images
    )
    # Recognized as addressed to the bot even if the model chose [NO_REPLY],
    # so don't let ambient or thread logic answer the same message again.
    return True


async def _handle_unsubscribe(event: dict[str, Any], client) -> bool:
    """Do the gleeb-glorb bit: a bare UNSUBSCRIBE in a thread gets RESUBSCRIBE."""
    channel = event.get("channel")
    thread_ts = event.get("thread_ts")
    ts = event.get("ts")
    user_id = event.get("user")
    if not channel or not thread_ts or not ts or thread_ts == ts or not user_id:
        return False
    if not _is_unsubscribe(event.get("text")):
        return False

    key = (channel, thread_ts)
    now = time.monotonic()
    if now - _last_unsubscribe.get(key, 0.0) < _UNSUBSCRIBE_COOLDOWN:
        return True
    _last_unsubscribe[key] = now
    await post(
        client,
        channel,
        f'<@{user_id}> RESUBSCRIBE\n(use "turn off notifications for replies" instead)\nSee how',
        thread_ts=thread_ts,
    )
    return True


async def _handle_thread_continuation(event: dict[str, Any], client) -> None:
    channel = event.get("channel")
    thread_ts = event.get("thread_ts")
    user_id = event.get("user")
    if not channel or not thread_ts or not user_id:
        return
    if not _ai_allowed(event):
        return
    if not runtime.db.is_thread(channel, thread_ts, owner="bot"):
        return
    if runtime.db.kv_get("config:thread_followups") == "false":
        return
    text = (event.get("text") or "").strip()
    if not text:
        return
    if f"<@{runtime.bot_user_id}>" in text:
        return  # app_mention owns this one
    if runtime.user_id and f"<@{runtime.user_id}>" in text:
        return  # user gateway owns this one
    if user_retry_after(user_id):
        return
    if len(text) < 4:
        return

    images = await fetch_slack_images(event, runtime.settings.slack_bot_token)
    voice_transcripts = await transcribe_slack_audio(
        event, runtime.settings.slack_bot_token, runtime.settings
    )
    if voice_transcripts:
        voice_block = "\n".join(voice_transcripts)
        text = f"{text}\n\n[Voice message]: {voice_block}".strip()

    prefix = "mpim" if event.get("channel_type") == "mpim" else "ch"
    rows = runtime.db.history(f"{prefix}:{channel}:{thread_ts}", 8)
    bot_last = bool(rows and rows[-1].get("role") == "assistant")
    recent = await _recent_lines(client, rows)

    bot_names = {runtime.settings.bot_name.lower(), "nugget", "chicen", "chicennugget"}
    text_lower = text.lower()
    explicitly_named = any(re.search(rf"\b{re.escape(name)}\b", text_lower) for name in bot_names if name)

    if not explicitly_named:
        human_users = {r.get("user_id") for r in rows if r.get("role") == "user" and r.get("user_id")}
        if len(human_users) >= 2 and "?" not in text and not images:
            return
        if not images and len(text.split()) < 3 and "?" not in text:
            return
        # A statement is not an invitation. "i have 61 hours and can't do shit"
        # should not turn into an unprompted coaching question.
        if not images and "?" not in text and not _REQUEST_RE.search(text):
            return

    jev_hint = ""
    if runtime.gate is not None and runtime.settings.ai_gate_enabled:
        user_name = await _user_name(client, user_id)
        channel_name = await _channel_name(client, channel)
        decision = await runtime.gate.decide(
            text=text,
            channel_name=channel_name,
            user_name=user_name,
            user_id=user_id,
            recent=recent,
            bot_last=bot_last,
        )
        if not decision.reply:
            logger.info(
                "gate suppressed thread continuation for %s in %s: %s",
                user_id,
                channel,
                decision.kind,
            )
            return
        jev_hint = decision.hint()

    cooldown = runtime.settings.ai_thread_cooldown
    key = (channel, thread_ts)
    if (
        isinstance(cooldown, (int, float))
        and cooldown > 0
        and not explicitly_named
        and time.monotonic() - _last_thread.get(key, 0.0) < cooldown
    ):
        logger.info("thread followup cooldown in %s (%ss)", channel, cooldown)
        return

    posted = await _handle_channel(
        event,
        client,
        text=text,
        thread_ts=thread_ts,
        trigger="thread_followup",
        jev=jev_hint,
        images=images,
    )
    if posted:
        _last_thread[key] = time.monotonic()


async def _recent_lines(client, rows: list[dict[str, Any]]) -> list[str]:
    """Format prior turns for the gate so it can tell who is talking to whom."""
    lines: list[str] = []
    for row in rows[-6:]:
        text = " ".join((row.get("text") or "").split())
        if not text:
            continue
        if row.get("role") == "assistant":
            lines.append(f"you: {text[:200]}")
        else:
            uid = row.get("user_id") or ""
            name = await _user_name(client, uid) if uid else "someone"
            lines.append(f"{name}: {text[:200]}")
    return lines


async def _maybe_ambient(event: dict[str, Any], client) -> None:
    """Let jev decide whether an unaddressed channel message deserves a reply."""
    if runtime.db and runtime.db.kv_get("config:ambient_ai") == "false":
        return
    settings = runtime.settings
    channel = event.get("channel")
    user_id = event.get("user")
    text = (event.get("text") or "").strip()

    if not channel or not user_id or not text:
        return
    if runtime.agent is None or runtime.gate is None or not settings.ai_enabled:
        return
    if not settings.ai_allowed(channel) or not settings.ambient_allowed(channel):
        return
    # mentions are handled by app_mention, threads by _handle_thread_continuation
    if event.get("thread_ts"):
        return
    if f"<@{runtime.bot_user_id}>" in text:
        return
    if runtime.user_id and f"<@{runtime.user_id}>" in text:
        return
    if len(text) < 4:
        return
    if user_retry_after(user_id):
        return

    cooldown = settings.ai_ambient_cooldown
    last = _last_ambient.get(channel, 0.0)
    if cooldown and time.monotonic() - last < cooldown:
        return

    user_name = await _user_name(client, user_id)
    channel_name = await _channel_name(client, channel)
    decision = await runtime.gate.decide(
        text=text, channel_name=channel_name, user_name=user_name, user_id=user_id
    )
    if not decision.reply:
        return

    _last_ambient[channel] = time.monotonic()
    logger.info("gate let %s through in %s (%s)", user_id, channel, decision.kind)
    await _handle_channel(
        event,
        client,
        text=incoming_text(
            text,
            bot_user_id=runtime.bot_user_id,
            assistant_user_id=runtime.user_id or "",
        )
        or text,
        thread_ts=event.get("ts"),
        trigger="ambient",
        jev=decision.hint(),
    )


async def _user_name(client, user_id: str) -> str:
    if user_id not in _names:
        _names[user_id] = display_name(await user_info(client, user_id))
    return _names[user_id]


async def _channel_name(client, channel_id: str) -> str:
    key = f"ch:{channel_id}"
    if key not in _names:
        info = await channel_info(client, channel_id) or {}
        _names[key] = f"#{info.get('name')}" if info.get("name") else "a channel"
    return _names[key]


async def _handle_channel(
    event: dict[str, Any],
    client,
    *,
    text: str,
    thread_ts: str | None = None,
    trigger: str = "mention",
    jev: str = "",
    images: list[str] | None = None,
) -> bool:
    settings = runtime.settings
    channel = event.get("channel")
    user_id = event.get("user")
    if not channel or not user_id:
        return False
    if not settings.ai_enabled or runtime.agent is None:
        return False
    text = text.strip()
    if not text and not images:
        text = "hey"
    elif not text and images:
        text = "Look at this image."
    retry = user_retry_after(user_id)
    if retry:
        await post_ephemeral(
            client, channel, user_id, f"slow down a lil, try again in {retry}s :3"
        )
        return False
    root_ts = thread_ts or event.get("thread_ts") or event.get("ts")
    prefix = "mpim" if event.get("channel_type") == "mpim" else "ch"
    conversation = f"{prefix}:{channel}:{root_ts}"
    message_ts = event.get("ts")
    reply_thread_ts = (
        None
        if event.get("channel_type") == "mpim" and not event.get("thread_ts")
        else root_ts
    )

    if message_ts and trigger in {"mention", "mpim_mention", "named", "thread_followup"}:
        await react(client, channel, message_ts, "eyes")
    posted = False
    try:
        async with runtime.lock(conversation):
            answer = await runtime.agent.reply(
                conversation=conversation,
                user_id=user_id,
                text=text,
                channel_id=channel,
                thread_ts=reply_thread_ts,
                is_dm=False,
                is_mpim=event.get("channel_type") == "mpim",
                trigger=trigger,
                jev=jev,
                images=images,
            )
        if not answer or answer.strip() in {"[NO_REPLY]", "NO_REPLY"}:
            return False
        for chunk in chunk_text(disarm_broadcasts(format_mrkdwn(answer))):
            await post(client, channel, chunk, thread_ts=reply_thread_ts)
        runtime.db.mark_thread(channel, root_ts, owner="bot")
        posted = True
    except Exception:
        logger.exception("channel agent turn failed")
        await post(
            client,
            channel,
            "something broke while i was thinking. check my logs?",
            thread_ts=reply_thread_ts,
        )
        posted = True
    finally:
        if message_ts and trigger in {"mention", "mpim_mention", "named", "thread_followup"}:
            await unreact(client, channel, message_ts, "eyes")
    return posted

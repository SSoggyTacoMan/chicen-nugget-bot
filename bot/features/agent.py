"""Slack event glue for the AI agent."""

from __future__ import annotations

import logging
import time
from typing import Any

from bot.runtime import runtime
from bot.ai.limits import user_retry_after
from bot.slack_api import (
    channel_info,
    fetch_slack_images,
    post,
    post_ephemeral,
    react,
    unreact,
    user_info,
)
from bot.utils.text import chunk_text, disarm_broadcasts, display_name, strip_mentions

logger = logging.getLogger(__name__)

# channel_id -> monotonic time of the last unprompted reply, so the gate saying
# yes five times in a row doesn't turn into five replies in a row.
_last_ambient: dict[str, float] = {}
_names: dict[str, str] = {}


async def on_message(event: dict[str, Any], client) -> None:
    subtype = event.get("subtype")
    if (subtype and subtype != "file_share") or event.get("bot_id"):
        return
    user_id = event.get("user")
    if not user_id or user_id == runtime.bot_user_id or (runtime.user_id and user_id == runtime.user_id):
        return

    channel_type = event.get("channel_type")
    if channel_type == "im":
        await _handle_dm(event, client)
        return
    if channel_type in {"channel", "group"}:
        await _handle_thread_continuation(event, client)
        await _maybe_ambient(event, client)


async def on_app_mention(event: dict[str, Any], client) -> None:
    subtype = event.get("subtype")
    if (subtype and subtype != "file_share") or event.get("bot_id"):
        return
    user_id = event.get("user")
    if not user_id or user_id == runtime.bot_user_id or (runtime.user_id and user_id == runtime.user_id):
        return
    channel = event.get("channel") or ""
    # DM mentions are handled by the message event.
    if channel.startswith("D"):
        return
    if not runtime.settings.ai_allowed(channel):
        return
    images = await fetch_slack_images(event, runtime.settings.slack_bot_token)
    text = strip_mentions(event.get("text") or "").strip()
    if not text and images:
        text = "What is in this image?"
    await _handle_channel(
        event, client, text=text, trigger="mention", images=images
    )


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
    text = (event.get("text") or "").strip()
    if not text and not images:
        return
    if not text and images:
        text = "Describe or look at this image."
    retry = user_retry_after(user_id)
    if retry:
        await post(client, event["channel"], f"slow down a lil — try again in {retry}s :3")
        return
    message_ts = event.get("ts")
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
                    thread_ts=None,
                    is_dm=True,
                    trigger="dm",
                    images=images,
                )
            except Exception:  # noqa: BLE001
                logger.exception("dm agent turn failed")
                answer = "something broke on my side. check my logs?"
        for chunk in chunk_text(disarm_broadcasts(answer)):
            await post(client, event["channel"], chunk)
    finally:
        if message_ts:
            await unreact(client, event["channel"], message_ts, "eyes")


async def _handle_thread_continuation(event: dict[str, Any], client) -> None:
    channel = event.get("channel")
    thread_ts = event.get("thread_ts")
    user_id = event.get("user")
    if not channel or not thread_ts or not user_id:
        return
    if not runtime.settings.ai_allowed(channel):
        return
    if not runtime.db.is_thread(channel, thread_ts):
        return
    text = (event.get("text") or "").strip()
    if not text:
        return
    if f"<@{runtime.bot_user_id}>" in text:
        return  # app_mention owns this one
    if user_retry_after(user_id):
        return
    if len(text) < 4:
        return

    images = await fetch_slack_images(event, runtime.settings.slack_bot_token)

    jev_hint = ""
    is_owner = user_id == runtime.settings.owner_user_id
    if not is_owner and runtime.gate is not None and runtime.settings.ai_gate_enabled:
        user_name = await _user_name(client, user_id)
        channel_name = await _channel_name(client, channel)
        decision = await runtime.gate.decide(
            text=text, channel_name=channel_name, user_name=user_name, user_id=user_id
        )
        if not decision.reply:
            logger.info("gate suppressed thread continuation for %s in %s: %s", user_id, channel, decision.kind)
            return
        jev_hint = decision.hint()

    await _handle_channel(
        event,
        client,
        text=text,
        thread_ts=thread_ts,
        trigger="thread_followup",
        jev=jev_hint,
        images=images,
    )


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
        text=strip_mentions(text) or text,
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
) -> None:
    settings = runtime.settings
    channel = event.get("channel")
    user_id = event.get("user")
    if not channel or not user_id:
        return
    if not settings.ai_enabled or runtime.agent is None:
        return
    text = text.strip()
    if not text and not images:
        text = "hey"
    elif not text and images:
        text = "Look at this image."
    retry = user_retry_after(user_id)
    if retry:
        await post_ephemeral(
            client, channel, user_id, f"slow down a lil — try again in {retry}s :3"
        )
        return
    root_ts = thread_ts or event.get("thread_ts") or event.get("ts")
    conversation = f"ch:{channel}:{root_ts}"
    message_ts = event.get("ts")

    if message_ts and trigger in {"mention", "thread_followup"}:
        await react(client, channel, message_ts, "eyes")
    try:
        async with runtime.lock(conversation):
            answer = await runtime.agent.reply(
                conversation=conversation,
                user_id=user_id,
                text=text,
                channel_id=channel,
                thread_ts=root_ts,
                is_dm=False,
                trigger=trigger,
                jev=jev,
                images=images,
            )
        if not answer or answer.strip() in {"[NO_REPLY]", "NO_REPLY"}:
            return
        for chunk in chunk_text(disarm_broadcasts(answer)):
            await post(client, channel, chunk, thread_ts=root_ts)
        runtime.db.mark_thread(channel, root_ts)
    except Exception:  # noqa: BLE001
        logger.exception("channel agent turn failed")
        await post(
            client,
            channel,
            "something broke while i was thinking. check my logs?",
            thread_ts=root_ts,
        )
    finally:
        if message_ts and trigger in {"mention", "thread_followup"}:
            await unreact(client, channel, message_ts, "eyes")

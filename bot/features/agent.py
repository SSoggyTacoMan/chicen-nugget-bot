"""Slack event glue for the AI agent."""

from __future__ import annotations

import logging
from typing import Any

from bot.runtime import runtime
from bot.ai.limits import user_retry_after
from bot.slack_api import post, post_ephemeral, react, unreact
from bot.utils.text import chunk_text, strip_mentions

logger = logging.getLogger(__name__)


async def on_message(event: dict[str, Any], client) -> None:
    if event.get("subtype") or event.get("bot_id"):
        return
    user_id = event.get("user")
    if not user_id or user_id == runtime.bot_user_id:
        return

    channel_type = event.get("channel_type")
    if channel_type == "im":
        await _handle_dm(event, client)
        return
    if channel_type in {"channel", "group"}:
        await _handle_thread_continuation(event, client)


async def on_app_mention(event: dict[str, Any], client) -> None:
    if event.get("subtype") or event.get("bot_id"):
        return
    channel = event.get("channel") or ""
    # DM mentions are handled by the message event.
    if channel.startswith("D"):
        return
    if not runtime.settings.ai_allowed(channel):
        return
    await _handle_channel(event, client, text=strip_mentions(event.get("text") or ""))


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

    text = (event.get("text") or "").strip()
    if not text:
        return
    retry = user_retry_after(user_id)
    if retry:
        await post(client, event["channel"], f"slow down a lil — try again in {retry}s :3")
        return
    conversation = f"dm:{user_id}"
    async with runtime.lock(conversation):
        try:
            answer = await runtime.agent.reply(
                conversation=conversation,
                user_id=user_id,
                text=text,
                channel_id=event.get("channel"),
                thread_ts=None,
                is_dm=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("dm agent turn failed")
            answer = "something broke on my side. check my logs?"
    for chunk in chunk_text(answer):
        await post(client, event["channel"], chunk)


async def _handle_thread_continuation(event: dict[str, Any], client) -> None:
    channel = event.get("channel")
    thread_ts = event.get("thread_ts")
    if not channel or not thread_ts:
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
    await _handle_channel(event, client, text=text, thread_ts=thread_ts)


async def _handle_channel(
    event: dict[str, Any], client, *, text: str, thread_ts: str | None = None
) -> None:
    settings = runtime.settings
    channel = event.get("channel")
    user_id = event.get("user")
    if not channel or not user_id:
        return
    if not settings.ai_enabled or runtime.agent is None:
        return
    text = text.strip()
    if not text:
        text = "hey"
    retry = user_retry_after(user_id)
    if retry:
        await post_ephemeral(
            client, channel, user_id, f"slow down a lil — try again in {retry}s :3"
        )
        return
    root_ts = thread_ts or event.get("thread_ts") or event.get("ts")
    conversation = f"ch:{channel}:{root_ts}"
    message_ts = event.get("ts")

    if message_ts:
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
            )
        for chunk in chunk_text(answer):
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
        if message_ts:
            await unreact(client, channel, message_ts, "eyes")

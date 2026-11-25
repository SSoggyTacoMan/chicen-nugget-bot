"""Slack message shortcuts: explain a message, summarize its thread."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bot.ai.limits import user_retry_after
from bot.features import dm_chats
from bot.runtime import runtime
from bot.slack_api import message_text, post
from bot.utils.text import (
    MAX_SLACK_TEXT,
    chat_chunks,
    chunk_options,
    disarm_broadcasts,
    format_mrkdwn,
)

logger = logging.getLogger(__name__)


def register(app) -> None:
    app.shortcut("nugget_explain")(on_explain)
    app.shortcut("nugget_summarize")(on_summarize)


async def on_explain(ack, shortcut: dict[str, Any], client) -> None:
    await ack()
    await _run_shortcut(
        shortcut,
        client,
        "explain this message like a developer friend. keep it short.\n\n",
    )


async def on_summarize(ack, shortcut: dict[str, Any], client) -> None:
    await ack()
    await _run_shortcut(
        shortcut,
        client,
        "summarize this thread in a few lines. call `summarize_thread` first so you "
        "read every reply instead of just the last few lines; it paginates long "
        "threads. keep it useful, not chronological.\n\n",
    )


async def _run_shortcut(
    shortcut: dict[str, Any], client, prefix: str
) -> None:
    channel = (shortcut.get("channel") or {}).get("id") or ""
    message = shortcut.get("message") or {}
    text = message_text(message)
    ts = message.get("ts") or ""
    user_id = (shortcut.get("user") or {}).get("id") or ""
    if not channel or not ts or not user_id or not text:
        return
    if runtime.agent is None or not runtime.settings.ai_enabled:
        return
    if not runtime.settings.ai_allowed(channel):
        return
    if user_retry_after(user_id):
        return
    if runtime.db is not None and runtime.db.is_opted_out(user_id):
        return

    root_ts = message.get("thread_ts") or ts
    is_dm = channel.startswith("D")
    secret = None
    if is_dm:
        resolved = dm_chats.chat_for_thread(user_id, root_ts)
        if resolved is not None:
            conversation, secret = resolved
        else:
            conversation = f"dm:{user_id}"
    else:
        conversation = f"ch:{channel}:{root_ts}"
    discreet = bool(
        secret is not None
        and getattr(runtime.settings, "secret_chat_discreet", True)
    )
    try:
        async with runtime.lock(conversation):
            answer = await runtime.agent.reply(
                conversation=conversation,
                user_id=user_id,
                text=(prefix + text)[:6000],
                channel_id=channel,
                thread_ts=root_ts,
                is_dm=is_dm,
                trigger="shortcut",
                client=client,
                secret=secret,
                public_veto=not is_dm,
                safety_review=not is_dm,
                message_ts=ts,
            )
        if not answer or answer.strip() in {"[NO_REPLY]", "NO_REPLY"}:
            return
        max_fragments, delay = chunk_options(runtime.settings)
        preview = (
            getattr(runtime.settings, "secret_preview_text", dm_chats.SECRET_REPLY_PREVIEW)
            if discreet
            else None
        )
        chunks = chat_chunks(
            disarm_broadcasts(format_mrkdwn(answer)),
            max_fragments=max_fragments,
            limit=2900 if discreet else MAX_SLACK_TEXT,
        )
        for index, chunk in enumerate(chunks):
            await post(
                client,
                channel,
                chunk,
                thread_ts=root_ts,
                notification_text=preview,
            )
            if index + 1 < len(chunks) and delay > 0:
                await asyncio.sleep(delay)
        if not is_dm and runtime.db:
            runtime.db.mark_thread(channel, root_ts, owner="bot")
    except Exception:
        logger.exception("message shortcut failed")

"""Slack message shortcuts: explain a message, summarize its thread."""

from __future__ import annotations

import logging
from typing import Any

from bot.ai.limits import user_retry_after
from bot.runtime import runtime
from bot.slack_api import post
from bot.utils.text import chunk_text, disarm_broadcasts, format_mrkdwn

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
        "summarize this thread in a few lines. keep it useful, not chronological.\n\n",
    )


async def _run_shortcut(
    shortcut: dict[str, Any], client, prefix: str
) -> None:
    channel = (shortcut.get("channel") or {}).get("id") or ""
    message = shortcut.get("message") or {}
    text = (message.get("text") or "").strip()
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

    root_ts = message.get("thread_ts") or ts
    conversation = f"ch:{channel}:{root_ts}"
    try:
        async with runtime.lock(conversation):
            answer = await runtime.agent.reply(
                conversation=conversation,
                user_id=user_id,
                text=(prefix + text)[:6000],
                channel_id=channel,
                thread_ts=root_ts,
                is_dm=False,
                trigger="shortcut",
                client=client,
            )
        if not answer or answer.strip() in {"[NO_REPLY]", "NO_REPLY"}:
            return
        for chunk in chunk_text(disarm_broadcasts(format_mrkdwn(answer))):
            await post(client, channel, chunk, thread_ts=root_ts)
        if runtime.db:
            runtime.db.mark_thread(channel, root_ts, owner="bot")
    except Exception:
        logger.exception("message shortcut failed")

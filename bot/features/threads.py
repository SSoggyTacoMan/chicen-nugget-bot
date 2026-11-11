"""NovaBot-inspired usergroup helpers.

- Warn someone who replies in a thread on a usergroup ping.
- Offer (or automatically post) a safe message for people to thread on.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bot.runtime import runtime
from bot.slack_api import post, post_ephemeral

logger = logging.getLogger(__name__)

USERGROUP_RE = re.compile(r"<!subteam\^([A-Z0-9]+)(?:\|[^>]*)?>")
THREAD_MARKERS = (":thread:", "\U0001f9f5")  # :thread: / 🧵


def has_usergroup_mention(text: str) -> bool:
    return bool(USERGROUP_RE.search(text or ""))


def wants_thread_anchor(text: str) -> bool:
    return any(marker in (text or "") for marker in THREAD_MARKERS)


def register(app) -> None:
    app.event("message")(on_message)
    app.action("send_thread_msg")(on_send_thread_msg)
    app.action("dismiss_thread_msg")(on_dismiss_thread_msg)


async def on_message(event: dict[str, Any], client) -> None:
    settings = runtime.settings
    if not settings.usergroup_guard and not settings.thread_helper:
        return
    if event.get("subtype") or event.get("bot_id"):
        return
    user_id = event.get("user")
    if not user_id or user_id == runtime.bot_user_id:
        return
    if event.get("channel_type") not in {"channel", "group"}:
        return
    channel = event.get("channel")
    if not channel:
        return

    ts = event.get("ts")
    thread_ts = event.get("thread_ts")
    is_reply = bool(thread_ts and ts and thread_ts != ts)

    if is_reply:
        if settings.usergroup_guard:
            parent = await _parent_message(client, channel, thread_ts)
            if parent and has_usergroup_mention(parent.get("text") or ""):
                await post(
                    client,
                    user_id,
                    "hey! please don't thread on user group mentions: "
                    "replying in the thread pings the whole group again. :3",
                )
        return

    if not settings.thread_helper:
        return
    text = event.get("text") or ""
    if not has_usergroup_mention(text):
        return
    if wants_thread_anchor(text):
        await post(client, channel, ":thread: thread here!")
        return
    await post_ephemeral(
        client,
        channel,
        user_id,
        "do you want to send a message for people to thread on?",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "do you want to send a message for people to thread on?",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "yes", "emoji": True},
                        "action_id": "send_thread_msg",
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "no", "emoji": True},
                        "action_id": "dismiss_thread_msg",
                        "style": "danger",
                    },
                ],
            },
        ],
    )


async def _parent_message(client, channel: str, thread_ts: str) -> dict[str, Any] | None:
    try:
        response = await client.conversations_history(
            channel=channel, latest=thread_ts, inclusive=True, limit=1
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("could not read parent message for %s/%s: %s", channel, thread_ts, exc)
        return None
    messages = response.get("messages") or []
    return messages[0] if messages else None


async def on_send_thread_msg(ack, body, client, respond) -> None:
    await ack()
    channel = (body.get("channel") or {}).get("id") or (body.get("container") or {}).get("channel_id")
    if channel:
        await post(client, channel, ":thread: thread here!")
    try:
        await respond(delete_original=True)
    except Exception:  # noqa: BLE001 - deleting the ephemeral is best-effort
        pass


async def on_dismiss_thread_msg(ack, respond) -> None:
    await ack()
    try:
        await respond(delete_original=True)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["register", "has_usergroup_mention", "wants_thread_anchor"]

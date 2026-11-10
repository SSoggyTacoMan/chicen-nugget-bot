"""Daily recap flow inspired by NovaBot.

A scheduled prompt in a public channel asks the owner how their day went. When
the owner marks it done, the bot links their reply, celebrates it, and optionally
opens a second private recap in a private channel.
"""

from __future__ import annotations

import logging
from typing import Any

from bot.runtime import runtime
from bot.slack_api import error_code, post, post_ephemeral, react, unreact, update

logger = logging.getLogger(__name__)

PUBLIC_ACTION = "public_daily_recap"
PRIVATE_ACTION = "private_daily_recap"


def register(app) -> None:
    app.action(PUBLIC_ACTION)(on_public_done)
    app.action(PRIVATE_ACTION)(on_private_done)


async def post_public_prompt(client) -> dict[str, Any] | None:
    settings = runtime.settings
    channel = settings.recap_channel_id
    owner = settings.owner_user_id
    if not channel or not owner:
        return None
    text = f"hi <@{owner}>, daily recap time! how was your day? :3"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":crescent_moon: done!", "emoji": True},
                    "action_id": PUBLIC_ACTION,
                }
            ],
        },
    ]
    result = await post(client, channel, text, blocks=blocks)
    if result:
        await react(client, channel, result["ts"], "zzz")
    return result


async def post_private_prompt(
    client, public_permalink: str | None = None
) -> dict[str, Any] | None:
    settings = runtime.settings
    channel = settings.recap_private_channel_id
    owner = settings.owner_user_id
    if not channel or not owner:
        return None
    text = f"<@{owner}>, private recap time! :3"
    if public_permalink:
        text += f"\nwhile you wait, you can look at <{public_permalink}|the public recap>!"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":crescent_moon: done!", "emoji": True},
                    "value": public_permalink or "",
                    "action_id": PRIVATE_ACTION,
                }
            ],
        },
    ]
    result = await post(client, channel, text, blocks=blocks)
    if result:
        await react(client, channel, result["ts"], "zzz")
    return result


async def on_public_done(ack, body, client) -> None:
    await ack()
    if not await _owner_only(body, client):
        return
    channel, ts = _message_ref(body)
    if not channel or not ts:
        return
    try:
        await _finish(client, body, channel, ts, "daily recap")
        permalink = await _recap_permalink(client, channel, ts)
        text = (
            f":sparkles: <{permalink}|your recap> is done now!"
            if permalink
            else ":sparkles: your recap is done now!"
        )
        if runtime.settings.recap_ping:
            text += f" <!subteam^{runtime.settings.recap_ping}>"
        await post(client, channel, text)
        await post_private_prompt(client, permalink)
    except Exception:  # noqa: BLE001
        logger.exception("public recap finish failed")
        await _tell(body, client, "couldn't finish the recap. check my logs?")


async def on_private_done(ack, body, client) -> None:
    await ack()
    if not await _owner_only(body, client):
        return
    channel, ts = _message_ref(body)
    if not channel or not ts:
        return
    try:
        await _finish(client, body, channel, ts, "private recap")
        permalink = await _recap_permalink(client, channel, ts)
        text = (
            f":sparkles: <{permalink}|your private recap> is done now!"
            if permalink
            else ":sparkles: your private recap is done now!"
        )
        if runtime.settings.recap_private_ping:
            text += f" <!subteam^{runtime.settings.recap_private_ping}>"
        await post(client, channel, text)
    except Exception:  # noqa: BLE001
        logger.exception("private recap finish failed")
        await _tell(body, client, "couldn't finish the private recap. check my logs?")


async def cmd_recap(ack, command, client) -> None:
    await ack()
    user = command.get("user_id")
    channel = command.get("channel_id")
    settings = runtime.settings
    if not settings.is_admin(user):
        await post_ephemeral(client, channel, user, "only admins can trigger the recap.")
        return
    if not settings.recap_channel_id:
        await post_ephemeral(client, channel, user, "recaps are off. set `RECAP_CHANNEL_ID` first.")
        return
    result = await post_public_prompt(client)
    if result:
        await post_ephemeral(client, channel, user, "recap prompt sent.")
    else:
        await post_ephemeral(
            client, channel, user, "couldn't post the recap prompt; check that i'm in the channel."
        )


async def _owner_only(body: dict[str, Any], client) -> bool:
    user = (body.get("user") or {}).get("id")
    if user and user == runtime.settings.owner_user_id:
        return True
    await _tell(
        body,
        client,
        f"only <@{runtime.settings.owner_user_id}> can complete daily recaps, silly :3",
    )
    return False


async def _finish(client, body: dict[str, Any], channel: str, ts: str, text: str) -> None:
    blocks = [
        block
        for block in ((body.get("message") or {}).get("blocks") or [])
        if block.get("type") != "actions"
    ]
    if not blocks:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    await update(client, channel, ts, text, blocks=blocks)
    await unreact(client, channel, ts, "zzz")
    await react(client, channel, ts, "sparkles")


async def _recap_permalink(client, channel: str, ts: str) -> str | None:
    owner = runtime.settings.owner_user_id
    target = ts
    try:
        response = await client.conversations_replies(channel=channel, ts=ts)
        messages = response.get("messages") or []
        reply = next(
            (
                message
                for message in reversed(messages)
                if message.get("user") == owner
                and message.get("ts")
                and message.get("ts") != ts
            ),
            None,
        )
        if reply:
            target = reply["ts"]
        response = await client.chat_getPermalink(channel=channel, message_ts=target)
        return response.get("permalink")
    except Exception as exc:  # noqa: BLE001
        logger.info("could not build recap permalink: %s", error_code(exc))
        return None


def _message_ref(body: dict[str, Any]) -> tuple[str | None, str | None]:
    channel = (body.get("channel") or {}).get("id") or (body.get("container") or {}).get(
        "channel_id"
    )
    ts = (body.get("message") or {}).get("ts") or (body.get("container") or {}).get("message_ts")
    return channel, ts


async def _tell(body: dict[str, Any], client, text: str) -> None:
    channel = (body.get("channel") or {}).get("id") or (body.get("container") or {}).get(
        "channel_id"
    )
    user = (body.get("user") or {}).get("id")
    if channel and user:
        await post_ephemeral(client, channel, user, text)


__all__ = ["register", "post_public_prompt", "post_private_prompt"]

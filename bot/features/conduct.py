"""Code-of-conduct helpers: mute, unmute and relay a report to a human.

The bot is not a moderator and never adjudicates a report. These commands only
give people a switch for their own contact preferences and a relay to the
owner/moderators, alongside the real Hack Club routes.
"""

from __future__ import annotations

import logging
import time

from bot.runtime import runtime
from bot.slack_api import post, post_ephemeral
from bot.utils.text import disarm_broadcasts

logger = logging.getLogger(__name__)

REPORT_ROUTES = (
    "you can also report directly: `conduct@hackclub.com`, an anonymous or named "
    "report on Slack via Shroud, or `eps@hackclub.com` for a Slack incident."
)
REPORT_COOLDOWN = 30.0
_MAX_REPORT_CHARS = 2000


async def cmd_mute(ack, command, client) -> None:
    """Stop the bot from replying to this person, across DMs and channels."""
    await ack()
    user = command.get("user_id") or ""
    channel = command.get("channel_id") or user
    if runtime.db is not None and user:
        runtime.db.opt_out(user, source="command")
        logger.info("opt-out set by command for %s", user)
        text = "ok. i'll leave you alone. `/nugget unmute` when you want me back."
    else:
        text = "i couldn't save that. try again in a moment."
    await post_ephemeral(client, channel, user, text)


async def cmd_unmute(ack, command, client) -> None:
    """Undo a mute so the bot can answer again."""
    await ack()
    user = command.get("user_id") or ""
    channel = command.get("channel_id") or user
    existed = runtime.db is not None and runtime.db.opt_in(user)
    text = "back. mention me or dm me whenever." if existed else "you weren't muted."
    await post_ephemeral(client, channel, user, text)


async def cmd_report(ack, command, client) -> None:
    """Relay a conduct report to the owner and moderators, privately.

    The bot does not investigate, decide, or promise confidentiality. It passes
    the reporter's own words to the humans who can act, then points the reporter
    at Hack Club's official routes.
    """
    await ack()
    settings = runtime.settings
    db = runtime.db
    reporter = command.get("user_id") or ""
    channel = command.get("channel_id") or reporter

    if db is not None and reporter:
        now = time.time()
        last = db.kv_get(f"report:last:{reporter}")
        try:
            last_at = float(last or 0)
        except (TypeError, ValueError):
            last_at = 0.0
        if now - last_at < REPORT_COOLDOWN:
            await post_ephemeral(
                client,
                channel,
                reporter,
                "one report at a time. give it a moment, and use the direct routes if it's urgent.",
            )
            return
        db.kv_set(f"report:last:{reporter}", str(now))

    body = (command.get("text") or "").strip()
    if len(body) > _MAX_REPORT_CHARS:
        body = body[:_MAX_REPORT_CHARS] + "…"

    recipients: list[str] = []
    for user_id in [settings.owner_user_id, *sorted(settings.moderator_user_ids)]:
        if user_id and user_id != reporter and user_id not in recipients:
            recipients.append(user_id)

    if not recipients:
        await post_ephemeral(
            client,
            channel,
            reporter,
            "there's no moderator configured here, so i can't relay it. " + REPORT_ROUTES,
        )
        return

    message = (
        f":rotating_light: *conduct report* from <@{reporter}> in <#{channel}>\n"
        f"{disarm_broadcasts(body) if body else '_no details given_'}\n\n"
        "this is a relay, not a ruling. keep the reporter's identity private and "
        "follow the code of conduct: <https://hackclub.com/conduct>."
    )

    delivered = 0
    for user_id in recipients:
        if await post(client, user_id, message):
            delivered += 1

    if delivered:
        logger.info("relayed a conduct report from %s to %s people", reporter, delivered)
        await post_ephemeral(
            client,
            channel,
            reporter,
            "sent to the owner and moderators. i'm a bot, so i can't decide or "
            "investigate it, and i can't promise more than passing it along. "
            + REPORT_ROUTES,
        )
    else:
        await post_ephemeral(
            client,
            channel,
            reporter,
            "i couldn't reach a moderator. " + REPORT_ROUTES,
        )


__all__ = ["REPORT_ROUTES", "cmd_mute", "cmd_report", "cmd_unmute"]

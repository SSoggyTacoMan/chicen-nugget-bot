from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config import Settings
from bot.db import Database


def collect_stats(db: Database) -> dict[str, int]:
    return db.stats()


def format_stats(stats: dict[str, int]) -> str:
    return (
        "*workspace stats*\n"
        f"• members tracked: *{stats['members']}*\n"
        f"• joins: *{stats['joins_24h']}* in 24h · *{stats['joins_7d']}* in 7d\n"
        f"• leaves: *{stats['leaves_24h']}* in 24h\n"
        f"• tier 2 requests: *{stats['pending_access']}* pending · "
        f"*{stats['approved_access']}* approved · *{stats['denied_access']}* denied\n"
        f"• open polls: *{stats['open_polls']}* · active reminders: *{stats['reminders']}*\n"
        f"• ai memory: *{stats['messages']}* turns · *{stats['notes']}* notes"
    )


def build_digest(db: Database, settings: Settings, tz: ZoneInfo) -> str:
    stats = collect_stats(db)
    lines = [
        f"*good morning — {settings.bot_name} daily digest*",
        f"_{datetime.now(tz).strftime('%A %d %B %Y, %H:%M %Z')}_",
        "",
        f"*joins:* {stats['joins_24h']} in the last 24h · {stats['joins_7d']} this week",
        f"*leaves:* {stats['leaves_24h']} in the last 24h",
        f"*tier 2:* {stats['pending_access']} pending",
        f"*polls:* {stats['open_polls']} open · *reminders:* {stats['reminders']} active",
    ]

    pending = db.pending_access()
    if pending:
        lines.append("")
        lines.append("*pending requests:*")
        for request in pending[:10]:
            reason = (request.get("reason") or "no reason given").replace("\n", " ")
            if len(reason) > 100:
                reason = reason[:97] + "..."
            lines.append(f"• `#{request['id']}` <@{request['user_id']}> — {reason}")

    upcoming = db.list_reminders()
    if upcoming:
        lines.append("")
        lines.append("*next reminders:*")
        for reminder in upcoming[:5]:
            when = datetime.fromtimestamp(reminder["next_run"], tz)
            body = reminder["text"].replace("\n", " ")
            if len(body) > 80:
                body = body[:77] + "..."
            lines.append(f"• {when.strftime('%a %H:%M')} — {body}")

    return "\n".join(lines)

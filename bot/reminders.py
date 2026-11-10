"""Reminder helpers shared by the slash commands, the AI tool and the scheduler."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config import Settings
from bot.db import Database
from bot.utils.text import channel_mention
from bot.utils.timeparse import ParsedWhen, describe_recurrence, format_when, next_occurrence

DM_PREFIX = "dm:"


def dm_target(user_id: str) -> str:
    return f"{DM_PREFIX}{user_id}"


def target_label(target: str) -> str:
    if target.startswith(DM_PREFIX):
        return "your DMs"
    if target.startswith("C") or target.startswith("G"):
        return channel_mention(target)
    return target


def create_reminder(
    db: Database,
    settings: Settings,
    *,
    user_id: str,
    target: str,
    parsed: ParsedWhen,
    text: str,
    tz: ZoneInfo,
    kind: str = "reminder",
) -> dict | None:
    if db.active_reminder_count() >= settings.max_reminders:
        return None
    when = parsed.when
    if parsed.recurrence and when <= datetime.now(tz):
        when = next_occurrence(parsed.recurrence, datetime.now(tz), tz) or when
    reminder_id = db.add_reminder(
        user_id=user_id,
        target=target,
        text=text,
        next_run=when,
        recurrence=parsed.recurrence,
        kind=kind,
    )
    return {
        "id": reminder_id,
        "next_run": when,
        "recurrence": parsed.recurrence,
    }


def confirmation_text(reminder: dict, tz: ZoneInfo, target: str) -> str:
    return (
        f"ok, reminder *#{reminder['id']}* set for "
        f"*{format_when(reminder['next_run'], tz)}* ({describe_recurrence(reminder['recurrence'])}) "
        f"→ {target_label(target)}"
    )


def reminder_line(reminder: dict, tz: ZoneInfo) -> str:
    body = reminder["text"].replace("\n", " ")
    if len(body) > 120:
        body = body[:117] + "..."
    return (
        f"*#{reminder['id']}* {body}\n"
        f"    next: {format_when(datetime.fromtimestamp(reminder['next_run'], tz), tz)} "
        f"({describe_recurrence(reminder.get('recurrence'))}) → "
        f"{target_label(reminder['target'])}"
    )

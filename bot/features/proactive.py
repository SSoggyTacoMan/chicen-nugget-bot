"""Quiet hours and a daily cap for unprompted messages."""

from __future__ import annotations

from datetime import datetime

from bot.config import Settings
from bot.db import Database
from bot.utils.tz import tzinfo


def is_quiet_now(
    settings: Settings, now: datetime | None = None
) -> bool:
    start = settings.quiet_hours_start
    end = settings.quiet_hours_end
    if start < 0 or end < 0 or start == end:
        return False
    current = now or datetime.now(tzinfo())
    minutes = current.hour * 60 + current.minute
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def _key(now: datetime | None = None) -> str:
    current = now or datetime.now(tzinfo())
    return f"proactive:{current.date().isoformat()}"


def proactive_available(
    db: Database, settings: Settings, now: datetime | None = None
) -> bool:
    if settings.proactive_daily_cap <= 0:
        return True
    try:
        used = int(db.kv_get(_key(now)) or 0)
    except (TypeError, ValueError):
        used = 0
    return used < settings.proactive_daily_cap


def note_proactive(db: Database, settings: Settings, now: datetime | None = None) -> None:
    if settings.proactive_daily_cap <= 0:
        return
    key = _key(now)
    try:
        used = int(db.kv_get(key) or 0)
    except (TypeError, ValueError):
        used = 0
    db.kv_set(key, str(used + 1))

"""Natural-ish "when" parsing for reminders.

Examples that work:

    10m                    2h                     1d 4h
    in 30 minutes          tomorrow 09:00         today 18:30
    mon 09:00              friday 17:00           2026-11-10 09:00
    daily 09:00            weekly mon 09:00       every 2h

The first matching time expression is removed from the front of the text and
everything left is the reminder body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

_UNIT_MINUTES = {
    "m": 1,
    "min": 1,
    "mins": 1,
    "minute": 1,
    "minutes": 1,
    "h": 60,
    "hr": 60,
    "hrs": 60,
    "hour": 60,
    "hours": 60,
    "d": 1440,
    "day": 1440,
    "days": 1440,
    "w": 10080,
    "week": 10080,
    "weeks": 10080,
}

_UNIT_RE = r"(?:weeks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m)"
_TIME_PAT = r"(?:(\d{1,2}):(\d{2})(?:\s*(am|pm))?|(\d{1,2})\s*(am|pm)|(midnight|noon))\b"
_DURATION_RE = re.compile(
    rf"^(?:in\s+)?((?:\d+\s*{_UNIT_RE}\b\s*)+)", re.IGNORECASE
)
_DURATION_PART_RE = re.compile(rf"(\d+)\s*({_UNIT_RE})\b", re.IGNORECASE)
_EVERY_RE = re.compile(
    rf"^every\s+(\d+)\s*({_UNIT_RE})\b\s*", re.IGNORECASE
)
_DAILY_RE = re.compile(rf"^(?:daily|every day)\s+(?:at\s+)?{_TIME_PAT}\s*", re.IGNORECASE)
_WEEKLY_RE = re.compile(
    rf"^(?:weekly|every)\s+({'|'.join(WEEKDAYS)})\s+(?:at\s+)?{_TIME_PAT}\s*",
    re.IGNORECASE,
)
_TOMORROW_RE = re.compile(rf"^tomorrow\s+(?:at\s+)?{_TIME_PAT}\s*", re.IGNORECASE)
_TODAY_RE = re.compile(rf"^today\s+(?:at\s+)?{_TIME_PAT}\s*", re.IGNORECASE)
_WEEKDAY_RE = re.compile(
    rf"^({'|'.join(WEEKDAYS)})(?:day)?\s+(?:at\s+)?{_TIME_PAT}\s*",
    re.IGNORECASE,
)
_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{1,2}):(\d{2})\b\s*")
_CLOCK_RE = re.compile(rf"^(?:at\s+)?{_TIME_PAT}\s*", re.IGNORECASE)


def _extract_hm(groups: tuple[str | None, ...]) -> tuple[int, int] | None:
    h_col, m_col, ap_col, h_bare, ap_bare, word = groups
    if word:
        return (0, 0) if word.lower() == "midnight" else (12, 0)
    hour_str = h_col if h_col is not None else h_bare
    min_str = m_col if h_col is not None else "0"
    ampm = ap_col if h_col is not None else ap_bare
    if hour_str is None:
        return None
    hour = int(hour_str)
    minute = int(min_str)
    if not (0 <= minute <= 59):
        return None
    if ampm:
        if not (1 <= hour <= 12):
            return None
        ap = ampm.lower()
        if ap == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    elif not (0 <= hour <= 23):
        return None
    return hour, minute


@dataclass(frozen=True)
class ParsedWhen:
    when: datetime
    recurrence: str | None
    rest: str
    matched: str


def _at(now: datetime, day_offset: int, hour: int, minute: int) -> datetime | None:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return datetime.combine(
        (now + timedelta(days=day_offset)).date(), time(hour, minute), tzinfo=now.tzinfo
    )


def _next_clock(now: datetime, hour: int, minute: int) -> datetime | None:
    same_day = _at(now, 0, hour, minute)
    if same_day is None:
        return None
    return same_day if same_day > now else same_day + timedelta(days=1)


def _duration_minutes(value: str) -> int | None:
    total = 0
    found = False
    for number, unit in _DURATION_PART_RE.findall(value):
        multiplier = _UNIT_MINUTES.get(unit.lower())
        if multiplier is None:
            return None
        total += int(number) * multiplier
        found = True
    return total if found and total > 0 else None


def parse_when(
    text: str,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
    require_rest: bool = True,
) -> ParsedWhen | None:
    """Pull a time expression off the front of `text`.

    `now` must be timezone-aware when supplied; otherwise it is built in `tz`
    (or UTC).
    """
    if tz is None:
        tz = ZoneInfo("UTC")
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    source = text.strip()
    if not source:
        return None

    def result(when: datetime | None, end: int, recurrence: str | None = None) -> ParsedWhen | None:
        if when is None:
            return None
        rest = source[end:].strip()
        rest = rest.lstrip(" -–—:|,").strip()
        if require_rest and not rest:
            return None
        return ParsedWhen(when=when, recurrence=recurrence, rest=rest, matched=source[:end].strip())

    # Recurring schedules first, so "every 2h" doesn't look like a one-shot.
    match = _DAILY_RE.match(source)
    if match:
        hm = _extract_hm(match.groups())
        if hm:
            hour, minute = hm
            first = _next_clock(now, hour, minute)
            return result(first, match.end(), f"daily:{hour:02d}:{minute:02d}")

    match = _WEEKLY_RE.match(source)
    if match:
        weekday = WEEKDAYS[match.group(1).lower()]
        hm = _extract_hm(match.groups()[1:])
        if hm:
            hour, minute = hm
            days = (weekday - now.weekday()) % 7
            first = _at(now, days, hour, minute)
            if first and first <= now:
                first += timedelta(days=7)
            return result(first, match.end(), f"weekly:{weekday}:{hour:02d}:{minute:02d}")

    match = _EVERY_RE.match(source)
    if match:
        minutes = int(match.group(1)) * _UNIT_MINUTES.get(match.group(2).lower(), 0)
        if minutes >= 1:
            return result(now + timedelta(minutes=minutes), match.end(), f"every:{minutes}")

    # One-shot relative durations: "10m", "in 2 hours", "1d 4h".
    match = _DURATION_RE.match(source)
    if match:
        minutes = _duration_minutes(match.group(1))
        if minutes:
            return result(now + timedelta(minutes=minutes), match.end())

    match = _TOMORROW_RE.match(source)
    if match:
        hm = _extract_hm(match.groups())
        if hm:
            return result(_at(now, 1, hm[0], hm[1]), match.end())

    match = _TODAY_RE.match(source)
    if match:
        hm = _extract_hm(match.groups())
        if hm:
            when = _at(now, 0, hm[0], hm[1])
            return result(when, match.end()) if when and when > now else None

    match = _WEEKDAY_RE.match(source)
    if match:
        weekday = WEEKDAYS[match.group(1).lower()]
        hm = _extract_hm(match.groups()[1:])
        if hm:
            hour, minute = hm
            days = (weekday - now.weekday()) % 7
            when = _at(now, days, hour, minute)
            if when and when <= now:
                when += timedelta(days=7)
            return result(when, match.end())

    match = _ISO_RE.match(source)
    if match:
        try:
            when = datetime.fromisoformat(f"{match.group(1)}T{int(match.group(2)):02d}:{int(match.group(3)):02d}")
        except ValueError:
            return None
        return result(when.replace(tzinfo=tz), match.end())

    match = _CLOCK_RE.match(source)
    if match:
        hm = _extract_hm(match.groups())
        if hm:
            when = _next_clock(now, hm[0], hm[1])
            return result(when, match.end())

    return None


def next_occurrence(
    recurrence: str | None, after: datetime, tz: ZoneInfo
) -> datetime | None:
    """Next run for a recurrence string, strictly after `after`."""
    if not recurrence:
        return None
    if after.tzinfo is None:
        after = after.replace(tzinfo=tz)

    kind, _, value = recurrence.partition(":")
    if kind == "every":
        try:
            minutes = max(1, int(value))
        except ValueError:
            return None
        interval = timedelta(minutes=minutes)
        candidate = after + interval
        # Catch up cheaply if the process was asleep for a long time.
        now = after.astimezone(tz)
        while candidate <= now:
            candidate += interval
        return candidate

    if kind == "daily":
        try:
            hour, minute = (int(part) for part in value.split(":", 1))
        except ValueError:
            return None
        candidate = _at(after, 0, hour, minute)
        if candidate and candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    if kind == "weekly":
        try:
            weekday_s, clock = value.split(":", 1)
            weekday = int(weekday_s)
            hour, minute = (int(part) for part in clock.split(":", 1))
        except ValueError:
            return None
        days = (weekday - after.weekday()) % 7
        candidate = _at(after, days, hour, minute)
        if candidate and candidate <= after:
            candidate += timedelta(days=7)
        return candidate

    return None


def format_when(when: datetime, tz: ZoneInfo) -> str:
    local = when.astimezone(tz)
    return local.strftime("%a %d %b %Y, %H:%M %Z")


def describe_recurrence(recurrence: str | None) -> str:
    if not recurrence:
        return "once"
    kind, _, value = recurrence.partition(":")
    if kind == "every":
        try:
            minutes = int(value)
        except ValueError:
            return recurrence
        if minutes % 1440 == 0:
            return f"every {minutes // 1440}d"
        if minutes % 60 == 0:
            return f"every {minutes // 60}h"
        return f"every {minutes}m"
    if kind == "daily":
        return f"daily at {value}"
    if kind == "weekly":
        try:
            weekday_s, clock = value.split(":", 1)
            names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            return f"weekly on {names[int(weekday_s)]} at {clock}"
        except (ValueError, IndexError):
            return recurrence
    return recurrence

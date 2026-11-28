"""Track why Hack Club AI is unavailable and when to try it again.

Not every error means the user's daily budget is gone. HCAI returns a
different shape for its own upstream billing problems (OpenRouter credits,
TypeSafe credits) than for the user's daily spending limit. The first should
be retried after a short cooldown; the second can wait until the next UTC day.

This module keeps that distinction in SQLite so the agent, ``/nugget status``
and ``/nugget doctor`` all describe the same state.
"""

from __future__ import annotations

import time

from bot.ai.budget import utc_day
from bot.db import Database

DAY_PREFIX = "quota:hcai:"
COOLDOWN_KEY = "quota:hcai:cooldown"
KIND_KEY = "quota:hcai:kind"
REASON_KEY = "quota:hcai:reason"
OUTAGE_KEY = "quota:hcai:outage"

KIND_DAILY = "daily"
KIND_UPSTREAM = "upstream"
KIND_UNKNOWN = "unknown"

UPSTREAM_RETRY_SECONDS = 5 * 60
UNKNOWN_RETRY_SECONDS = 15 * 60


def classify_quota_error(exc: Exception | str) -> str:
    """Return ``daily``, ``upstream``, ``unknown``, or ``""``.

    HCAI's own daily limit is a 429 with ``Daily spending limit of $X reached``.
    A 402 normally comes from OpenRouter and means HCAI's shared upstream
    credits are empty; that must not lock the bot out for the rest of the UTC
    day because HCAI can top up at any time.
    """

    text = str(exc)
    lower = text.lower()

    upstream_markers = (
        "openrouter_credits",
        "insufficient credits",
        "no available typesafe api credits",
        "typesafe api credits",
        "add more using https://openrouter.ai",
        "limit_source': 'openrouter",
        'limit_source": "openrouter',
    )
    if any(marker in lower for marker in upstream_markers):
        return KIND_UPSTREAM

    daily_markers = (
        "daily spending limit",
        "daily limit",
        "daily budget",
        "daily quota",
        "spending limit",
        "quota exceeded",
        "user_daily",
        "daily_user",
        "openrouter_free_tier_daily",
        "free-models-per-day",
    )
    if any(marker in lower for marker in daily_markers):
        return KIND_DAILY

    if "402" in text or "payment required" in lower:
        # HCAI's daily limit is a 429. An unlabelled 402 is almost always an
        # upstream credit problem, so retry it soon instead of tomorrow.
        return KIND_UPSTREAM
    if "quota" in lower:
        return KIND_UNKNOWN
    return ""


def retry_seconds(kind: str) -> int:
    if kind == KIND_UPSTREAM:
        return UPSTREAM_RETRY_SECONDS
    return UNKNOWN_RETRY_SECONDS


def mark_unavailable(db: Database | None, kind: str, reason: str = "") -> None:
    """Remember why the primary provider is unavailable."""

    if db is None:
        return
    if kind == KIND_DAILY:
        db.kv_set(f"{DAY_PREFIX}{utc_day()}", "empty")
        db.kv_del(COOLDOWN_KEY)
        db.kv_del(OUTAGE_KEY)
    else:
        db.kv_set(COOLDOWN_KEY, str(time.time() + retry_seconds(kind)))
        db.kv_set(OUTAGE_KEY, kind or KIND_UNKNOWN)
    db.kv_set(KIND_KEY, kind or KIND_UNKNOWN)
    if reason:
        db.kv_set(REASON_KEY, reason[:300])


def clear_cooldown(db: Database | None) -> None:
    """Clear a temporary block after the primary answers successfully."""

    if db is None:
        return
    db.kv_del(COOLDOWN_KEY)
    db.kv_del(KIND_KEY)
    db.kv_del(REASON_KEY)
    db.kv_del(OUTAGE_KEY)


def outage(db: Database | None) -> str:
    """The persistent reason a known outage is still being watched."""

    return (db.kv_get(OUTAGE_KEY) if db else "") or ""


def primary_status(db: Database | None) -> tuple[bool, str]:
    """Return ``(blocked, kind)`` for the primary provider."""

    if db is None:
        return False, ""
    if db.kv_get(f"{DAY_PREFIX}{utc_day()}"):
        return True, KIND_DAILY

    raw = db.kv_get(COOLDOWN_KEY)
    if raw:
        try:
            until = float(raw)
        except (TypeError, ValueError):
            until = 0.0
        if until > time.time():
            return True, db.kv_get(KIND_KEY) or KIND_UNKNOWN
        db.kv_del(COOLDOWN_KEY)
        db.kv_del(KIND_KEY)
        db.kv_del(REASON_KEY)
    return False, ""


def retry_after_seconds(db: Database | None) -> int:
    if db is None:
        return 0
    raw = db.kv_get(COOLDOWN_KEY)
    if not raw:
        return 0
    try:
        return max(0, int(float(raw) - time.time()))
    except (TypeError, ValueError):
        return 0


def describe(db: Database | None) -> str:
    """A short human status for ``/nugget status`` and ``/nugget doctor``."""

    blocked, kind = primary_status(db)
    if not blocked:
        kind = outage(db)
        if not kind:
            return ""
        if kind == KIND_UPSTREAM:
            return "hacai: upstream billing outage, retry pending"
        return "hacai: unavailable, retry pending"
    if kind == KIND_DAILY:
        return "hacai quota: empty today"
    minutes = max(1, (retry_after_seconds(db) + 59) // 60)
    if kind == KIND_UPSTREAM:
        return f"hacai: upstream billing outage, retrying in {minutes}m"
    return f"hacai: unavailable, retrying in {minutes}m"


def notice_key(kind: str) -> str:
    """One owner notice per UTC day and reason."""

    return f"{DAY_PREFIX}notified:{utc_day()}:{kind or KIND_UNKNOWN}"


def back_notified_key() -> str:
    """At most one 'hacai is back' ping per UTC day."""

    return f"{DAY_PREFIX}back_notified:{utc_day()}"


__all__ = [
    "KIND_DAILY",
    "KIND_UNKNOWN",
    "KIND_UPSTREAM",
    "OUTAGE_KEY",
    "back_notified_key",
    "classify_quota_error",
    "clear_cooldown",
    "describe",
    "mark_unavailable",
    "notice_key",
    "outage",
    "primary_status",
    "retry_after_seconds",
    "retry_seconds",
]

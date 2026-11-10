from __future__ import annotations

from zoneinfo import ZoneInfo

from bot.config import settings


def tzinfo() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001 - bad env value, fall back to UTC
        return ZoneInfo("UTC")


def tz_name() -> str:
    return settings.timezone

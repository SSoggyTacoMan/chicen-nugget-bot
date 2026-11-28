"""Effective unprompted-reply settings.

``AI_AMBIENT_CHANNELS`` is the startup default. The owner can override both the
master switch and the channel whitelist at runtime, from chat or the setup
panel, without a restart:

- ``config:ambient_ai=false`` is ping-only mode. Mentions, name calls, DMs and
  threads the bot is already in still work; it just stops deciding on its own
  to chime in.
- ``config:ambient_channels`` is a comma-separated allowlist (channel ids, or
  ``*``). When unset, the env default is used. An empty stored value means
  "nowhere", which is different from an unset value.
"""

from __future__ import annotations

from bot.config import Settings
from bot.db import Database

AMBIENT_ENABLED_KEY = "config:ambient_ai"
AMBIENT_CHANNELS_KEY = "config:ambient_channels"


def enabled(db: Database | None) -> bool:
    """Master switch. False means only reply when addressed or in a thread."""
    if db is None:
        return True
    value = db.kv_get(AMBIENT_ENABLED_KEY)
    return not (isinstance(value, str) and value.strip().lower() == "false")


def channels(settings: Settings, db: Database | None) -> frozenset[str]:
    """The effective ambient channel whitelist.

    A stored runtime value wins over the env default, including an explicitly
    empty value (`"off"`/`none` in chat), which disables ambient everywhere.
    """
    raw = db.kv_get(AMBIENT_CHANNELS_KEY) if db is not None else None
    if not isinstance(raw, str):
        default = getattr(settings, "ai_ambient_channels", frozenset())
        if not isinstance(default, (set, frozenset, list, tuple)):
            default = frozenset()
        return frozenset(str(part) for part in default if str(part).strip())
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def allowed(settings: Settings, db: Database | None, channel_id: str | None) -> bool:
    """May the bot listen here and decide on its own to reply?"""
    if not channel_id or not settings.ai_gate_enabled or not enabled(db):
        return False
    if not settings.ai_allowed(channel_id):
        return False
    allowed_channels = channels(settings, db)
    return "*" in allowed_channels or channel_id in allowed_channels


def describe(settings: Settings, db: Database | None) -> str:
    """Short human summary for logs and chat replies."""
    if not enabled(db):
        return "off (ping-only mode)"
    allowed_channels = channels(settings, db)
    if "*" in allowed_channels:
        return "everywhere"
    if not allowed_channels:
        return "nowhere (set ambient_channels to pick a channel)"
    return ", ".join(f"<#{channel}>" for channel in sorted(allowed_channels))

"""Shared runtime state, filled in once at startup.

Handlers live in small modules and reach for this instead of threading the
whole app through every function signature.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from slack_sdk.web.async_client import AsyncWebClient

from bot.config import Settings, settings as default_settings
from bot.db import Database

if TYPE_CHECKING:  # pragma: no cover
    from bot.ai.agent import Agent
    from bot.ai.limits import RateLimiter
    from slack_bolt.async_app import AsyncApp


@dataclass
class Runtime:
    settings: Settings = default_settings
    db: Database | None = None
    app: "AsyncApp | None" = None
    client: AsyncWebClient | None = None
    user_client: AsyncWebClient | None = None
    bot_user_id: str = ""
    bot_id: str = ""
    agent: "Agent | None" = None
    ai_limiter: "RateLimiter | None" = None
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    started_at: float = 0.0

    def lock(self, key: str) -> asyncio.Lock:
        lock = self.locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[key] = lock
        return lock


runtime = Runtime()

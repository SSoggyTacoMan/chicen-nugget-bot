"""Secondary AI provider used when Hack Club AI quota is empty."""

from __future__ import annotations

from dataclasses import dataclass

from bot.config import Settings
from bot.db import Database


@dataclass(frozen=True)
class FallbackProvider:
    base_url: str
    api_key: str
    model: str


def _text(settings: Settings, name: str, default: str = "") -> str:
    value = getattr(settings, name, None)
    return value.strip() if isinstance(value, str) and value.strip() else default


def fallback_provider(
    settings: Settings, db: Database | None = None
) -> FallbackProvider | None:
    """Effective 9router fallback, with SQLite overrides taking priority."""

    def raw(key: str) -> str:
        value = db.kv_get(f"config:{key}") if db else None
        return value.strip() if isinstance(value, str) else ""

    base_url = raw("ai_router_base_url") or _text(settings, "ai_router_base_url")
    api_key = raw("ai_router_api_key") or _text(settings, "ai_router_api_key")
    model = (
        raw("ai_router_model")
        or _text(settings, "ai_router_model")
        or "kgw/kilo-auto/free"
    )
    if not base_url or not api_key:
        return None
    return FallbackProvider(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
    )


__all__ = ["FallbackProvider", "fallback_provider"]

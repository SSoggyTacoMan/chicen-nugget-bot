"""Secondary AI provider used when Hack Club AI quota is empty."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bot.config import Settings
from bot.db import Database

DEFAULT_MODELS: tuple[str, ...] = ("kgw/kilo-auto/free",)


@dataclass(frozen=True)
class FallbackProvider:
    base_url: str
    api_key: str
    models: tuple[str, ...]

    @property
    def model(self) -> str:
        """First model in the ordered list, for logs and one-line status."""
        return self.models[0] if self.models else ""


def parse_models(value: str | None) -> tuple[str, ...]:
    """Accept comma or newline separated models, preserving order."""
    if not value:
        return ()
    parts = re.split(r"[,\n]+", value)
    models: list[str] = []
    for part in parts:
        model = part.strip()
        if model and model not in models:
            models.append(model)
    return tuple(models)


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
    configured = parse_models(raw("ai_router_models"))
    if not configured:
        configured = parse_models(raw("ai_router_model"))
    if not configured:
        configured = parse_models(_text(settings, "ai_router_models"))
    if not configured:
        configured = parse_models(_text(settings, "ai_router_model"))
    if not configured:
        configured = DEFAULT_MODELS
    if not base_url or not api_key:
        return None
    return FallbackProvider(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        models=configured,
    )


__all__ = [
    "DEFAULT_MODELS",
    "FallbackProvider",
    "fallback_provider",
    "parse_models",
]

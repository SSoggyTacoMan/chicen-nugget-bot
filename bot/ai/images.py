"""Image generation settings shared by /imagine, the tool and the persona prompt."""

from __future__ import annotations

from dataclasses import dataclass

from bot.config import Settings
from bot.db import Database

DEFAULT_MODEL = "google/gemini-3.1-flash-image"
DEFAULT_FALLBACK = "google/gemini-2.5-flash-image"
DEFAULT_COST_USD = 0.04

MODEL_OPTIONS: tuple[tuple[str, str], ...] = (
    (
        "google/gemini-3.1-flash-image",
        "Nano Banana 2 - default, balanced",
    ),
    (
        "google/gemini-nano-banana-2.1",
        "Nano Banana 2.1 - newer, better",
    ),
    (
        "google/gemini-3-pro-image",
        "Nano Banana Pro - best, slowest + priciest",
    ),
    (
        "google/gemini-3.1-flash-lite-image",
        "Nano Banana 2 Lite - cheapest + fastest",
    ),
)

FALLBACK_OPTIONS: tuple[tuple[str, str], ...] = (
    ("none", "no fallback"),
    *MODEL_OPTIONS,
)


@dataclass(frozen=True)
class ImageConfig:
    model: str | None
    fallback: str | None
    cost_usd: float


def _text(settings: Settings, name: str) -> str | None:
    value = getattr(settings, name, None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def imagine_config(settings: Settings, db: Database | None = None) -> ImageConfig:
    """Effective image settings, with SQLite overrides taking priority over env."""

    def raw(key: str) -> str | None:
        value = db.kv_get(f"config:{key}") if db else None
        return value.strip() if isinstance(value, str) and value.strip() else None

    model = raw("imagine_model") or _text(settings, "imagine_model")
    fallback = raw("imagine_fallback_model")
    if fallback is None:
        fallback = _text(settings, "imagine_fallback_model")
    if fallback == "none":
        fallback = None
    configured_cost = getattr(settings, "ai_image_cost_usd", DEFAULT_COST_USD)
    if not isinstance(configured_cost, (int, float)) or isinstance(
        configured_cost, bool
    ):
        configured_cost = DEFAULT_COST_USD
    try:
        cost = max(0.0, float(raw("imagine_cost_usd") or configured_cost))
    except (TypeError, ValueError):
        cost = configured_cost
    return ImageConfig(model=model, fallback=fallback, cost_usd=cost)


__all__ = [
    "DEFAULT_COST_USD",
    "DEFAULT_FALLBACK",
    "DEFAULT_MODEL",
    "FALLBACK_OPTIONS",
    "MODEL_OPTIONS",
    "ImageConfig",
    "imagine_config",
]

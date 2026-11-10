"""Daily AI spend tracking and hard stops.

Hack Club AI gives the key a daily allowance. A bug, a tool loop, or a very
chatty channel should never be able to eat the whole thing in a few minutes,
so every completion is priced and written to SQLite before the next one starts.

The tracker is deliberately conservative:

- days are UTC, matching the provider's reset;
- unknown models are priced high rather than low;
- the bot stops at ``AI_DAILY_BUDGET_USD`` even if the account still has room.

It only knows what the bot itself spent. Usage from other apps on the same key
is invisible here, so keep the limit below the account allowance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from bot.config import Settings
from bot.db import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input_per_million: float = 1.0
    output_per_million: float = 5.0


# Longest-prefix matching means dated snapshots ("...-1017") and ":batch"
# variants inherit the base model's price.
PRICES: dict[str, Price] = {
    "openai/gpt-5.6-luna": Price(0.20, 1.20),
    "openai/gpt-5.6-luna-pro": Price(0.20, 1.20),
    "anthropic/claude-haiku-5.5": Price(0.10, 0.50),
    "anthropic/claude-sonnet-5.5": Price(2.00, 10.00),
    "deepseek/deepseek-v4.1-flash": Price(0.006, 0.50),
    "z-ai/glm-5.3-flash": Price(0.15, 0.50),
    "qwen/qwen3.8-flash": Price(0.15, 0.47),
    "google/gemini-3.8-flash": Price(0.75, 3.75),
    "openai/gpt-5.5": Price(5.00, 30.00),
    "openai/gpt-5.4-mini": Price(0.75, 4.50),
    "openai/gpt-5.4": Price(2.50, 15.00),
    "x-ai/grok-4.3": Price(1.25, 2.50),
    "jev-latest": Price(0.042, 0.0),
    "jev-": Price(0.042, 0.0),
}

# If we don't know a model, assume it's expensive. Under-counting is how a
# budget quietly disappears.
DEFAULT_PRICE = Price(1.00, 5.00)


def price_for(model: str | None) -> Price:
    key = (model or "").strip().lower()
    if key in PRICES:
        return PRICES[key]
    for name in sorted(PRICES, key=len, reverse=True):
        if key.startswith(name):
            return PRICES[name]
    return DEFAULT_PRICE


def estimate_cost(
    model: str | None, prompt_tokens: int = 0, completion_tokens: int = 0
) -> float:
    price = price_for(model)
    tokens = max(0, prompt_tokens) * price.input_per_million
    tokens += max(0, completion_tokens) * price.output_per_million
    return tokens / 1_000_000


def estimate_tokens(text: str) -> int:
    """Cheap upper-ish estimate when a provider doesn't report usage."""
    return max(1, len(text) // 4)


def utc_day(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class BudgetStatus:
    day: str
    spent: float
    limit: float
    calls: int

    @property
    def remaining(self) -> float:
        if self.limit <= 0:
            return float("inf")
        return max(0.0, self.limit - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.limit > 0 and self.spent >= self.limit


class BudgetExceeded(RuntimeError):
    def __init__(self, status: BudgetStatus) -> None:
        self.status = status
        super().__init__(
            f"daily ai budget exhausted (${status.spent:.2f} of ${status.limit:.2f})"
        )


class Budget:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    def status(self) -> BudgetStatus:
        day = utc_day()
        return BudgetStatus(
            day=day,
            spent=self.db.ai_spend(day),
            limit=self.settings.ai_daily_budget_usd,
            calls=self.db.ai_calls(day),
        )

    def exhausted(self) -> bool:
        return self.status().exhausted

    def record(
        self,
        *,
        kind: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> float:
        cost = (
            cost_usd
            if cost_usd is not None
            else estimate_cost(model, prompt_tokens, completion_tokens)
        )
        day = utc_day()
        self.db.record_ai_usage(
            day=day,
            kind=kind,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )
        spent = self.db.ai_spend(day)
        logger.info(
            "ai spend kind=%s model=%s tokens=%s/%s cost=$%.6f today=$%.4f/$%.2f",
            kind,
            model,
            prompt_tokens,
            completion_tokens,
            cost,
            spent,
            self.settings.ai_daily_budget_usd,
        )
        return cost


__all__ = [
    "Budget",
    "BudgetExceeded",
    "BudgetStatus",
    "DEFAULT_PRICE",
    "PRICES",
    "Price",
    "estimate_cost",
    "estimate_tokens",
    "price_for",
    "utc_day",
]

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.ai.agent import fit_history
from bot.ai.budget import (
    DEFAULT_PRICE,
    Budget,
    BudgetExceeded,
    estimate_cost,
    estimate_tokens,
    price_for,
    utc_day,
)
from bot.db import Database


def make_budget(tmp_path, limit=2.5):
    db = Database(tmp_path / "budget.db")
    db.init()
    return Budget(SimpleNamespace(ai_daily_budget_usd=limit), db), db


def test_known_model_pricing():
    assert price_for("openai/gpt-5.5").input_per_million == 5.0
    assert price_for("openai/gpt-5.6-luna").output_per_million == 1.2


def test_prefix_matching_covers_dated_and_batch_ids():
    assert price_for("deepseek/deepseek-v4.1-flash-1017") == price_for(
        "deepseek/deepseek-v4.1-flash"
    )
    assert price_for("openai/gpt-5.5:batch") == price_for("openai/gpt-5.5")


def test_jev_version_strings_use_jev_pricing():
    price = price_for("jev-1.14.0")
    assert price.input_per_million == 0.042
    assert price.output_per_million == 0.0


def test_unknown_model_uses_a_pessimistic_default():
    assert price_for("mystery/model") == DEFAULT_PRICE
    assert price_for(None) == DEFAULT_PRICE


def test_estimate_cost_math():
    # 1M input + 1M output at luna prices
    assert estimate_cost("openai/gpt-5.6-luna", 1_000_000, 1_000_000) == pytest.approx(1.4)
    # a normal turn is fractions of a cent
    assert estimate_cost("openai/gpt-5.6-luna", 4_000, 300) == pytest.approx(0.00116)


def test_estimate_tokens_never_returns_zero():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 400) == 100


def test_utc_day_is_utc():
    moment = datetime(2026, 11, 10, 23, 59, tzinfo=timezone.utc)
    assert utc_day(moment) == "2026-11-10"
    chicago = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)
    assert utc_day(chicago) == "2026-11-10"


def test_database_roundtrip(tmp_path):
    budget, db = make_budget(tmp_path)
    budget.record(kind="chat", model="openai/gpt-5.6-luna", prompt_tokens=4000, completion_tokens=300)
    budget.record(kind="jev", model="jev-latest", prompt_tokens=600, completion_tokens=40)
    day = utc_day()
    assert db.ai_calls(day) == 2
    assert db.ai_spend(day) == pytest.approx(0.00116 + 0.0000252)
    rows = db.ai_usage(day)
    assert {row["kind"] for row in rows} == {"chat", "jev"}
    assert db.ai_spend("1999-01-01") == 0.0


def test_budget_status_and_exhaustion(tmp_path):
    budget, _ = make_budget(tmp_path, limit=0.001)
    status = budget.status()
    assert not status.exhausted
    assert status.remaining == pytest.approx(0.001)

    budget.record(kind="chat", model="openai/gpt-5.5", prompt_tokens=1000, completion_tokens=100)
    status = budget.status()
    assert status.spent > 0.001
    assert status.exhausted
    assert budget.exhausted()
    assert status.remaining == 0.0


def test_zero_limit_means_unlimited(tmp_path):
    budget, _ = make_budget(tmp_path, limit=0)
    budget.record(kind="chat", model="openai/gpt-5.5", prompt_tokens=100_000, completion_tokens=10_000)
    assert not budget.exhausted()
    assert budget.status().remaining == float("inf")


def test_explicit_image_cost(tmp_path):
    budget, db = make_budget(tmp_path)
    cost = budget.record(kind="image", model="google/gemini-3.1-flash-lite-image", cost_usd=0.04)
    assert cost == 0.04
    assert db.ai_spend(utc_day()) == pytest.approx(0.04)


def test_budget_exceeded_carries_status(tmp_path):
    budget, _ = make_budget(tmp_path, limit=1.0)
    budget.record(kind="chat", model="openai/gpt-5.5", prompt_tokens=1_000_000, completion_tokens=0)
    with pytest.raises(BudgetExceeded) as info:
        raise BudgetExceeded(budget.status())
    assert info.value.status.spent == pytest.approx(5.0)
    assert "budget" in str(info.value).lower()


def test_fit_history_keeps_the_newest_turns():
    history = [
        {"role": "user", "text": "a" * 100},
        {"role": "assistant", "text": "b" * 100},
        {"role": "user", "text": "c" * 100},
    ]
    kept = fit_history(history, "x" * 50, max_chars=250)
    assert [item["content"][0] for item in kept] == ["b", "c"]


def test_fit_history_skips_a_turn_that_does_not_fit():
    history = [
        {"role": "user", "text": "a" * 100},
        {"role": "assistant", "text": "b" * 100},
        {"role": "user", "text": "c" * 100},
    ]
    kept = fit_history(history, "x" * 50, max_chars=160)
    assert [item["content"][0] for item in kept] == ["c"]


def test_fit_history_ignores_unknown_roles_and_empty_text():
    history = [
        {"role": "system", "text": "do not send me"},
        {"role": "user", "text": "   "},
        {"role": "assistant", "text": "hello"},
    ]
    kept = fit_history(history, "", max_chars=100)
    assert kept == [{"role": "assistant", "content": "hello"}]


def test_fit_history_never_returns_more_than_the_cap():
    history = [{"role": "user", "text": "x" * 500} for _ in range(20)]
    kept = fit_history(history, "y" * 100, max_chars=1000)
    assert len(kept) <= 1
    assert sum(len(item["content"]) for item in kept) <= 900

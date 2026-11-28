import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from bot.ai import quota_watch
from bot.ai.budget import utc_day
from bot.ai.quota import (
    KIND_DAILY,
    KIND_UPSTREAM,
    back_notified_key,
    mark_unavailable,
    outage,
)
from bot.db import Database

UPSTREAM = (
    "Error code: 402 - {'error': {'message': 'Insufficient credits. Add more "
    "using https://openrouter.ai/settings/credits', 'code': 402, "
    "'metadata': {'limit_source': 'openrouter_credits'}}}"
)
DAILY = "429 Daily spending limit of $3 reached"


def _setup(tmp_path, error=None):
    db = Database(tmp_path / "watch.db")
    db.init()
    settings = SimpleNamespace(ai_model="primary-model", owner_user_id="U_OWNER")
    llm = MagicMock()
    if error is None:
        llm.chat.completions.create = AsyncMock(return_value=MagicMock())
    else:
        llm.chat.completions.create = AsyncMock(side_effect=error)
    agent = SimpleNamespace(llm=llm)
    agent._models_for = lambda is_user_identity: ["primary-model"]
    client = AsyncMock()
    return settings, db, agent, client


def _tick(settings, db, agent, client):
    return asyncio.run(
        quota_watch.tick(settings=settings, db=db, agent=agent, client=client)
    )


def test_no_outage_does_not_probe(tmp_path):
    settings, db, agent, client = _setup(tmp_path)
    assert _tick(settings, db, agent, client) is False
    agent.llm.chat.completions.create.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()


def test_recovery_clears_outage_and_pings_once(tmp_path):
    settings, db, agent, client = _setup(tmp_path)
    mark_unavailable(db, KIND_UPSTREAM, UPSTREAM)

    assert _tick(settings, db, agent, client) is True
    assert outage(db) == ""
    assert db.kv_get(back_notified_key()) == "1"
    client.chat_postMessage.assert_awaited_once()
    assert client.chat_postMessage.await_args.kwargs["channel"] == "U_OWNER"

    # A second tick is quiet because the outage is gone.
    assert _tick(settings, db, agent, client) is False
    assert client.chat_postMessage.await_count == 1


def test_still_down_keeps_watching_without_a_dm(tmp_path):
    settings, db, agent, client = _setup(tmp_path, error=Exception(UPSTREAM))
    mark_unavailable(db, KIND_UPSTREAM, UPSTREAM)

    assert _tick(settings, db, agent, client) is False
    assert outage(db) == KIND_UPSTREAM
    client.chat_postMessage.assert_not_awaited()


def test_daily_limit_stops_the_recovery_watch(tmp_path):
    settings, db, agent, client = _setup(tmp_path, error=Exception(DAILY))
    mark_unavailable(db, KIND_UPSTREAM, UPSTREAM)

    assert _tick(settings, db, agent, client) is False
    assert outage(db) == ""
    assert db.kv_get(f"quota:hcai:{utc_day()}") == "empty"
    client.chat_postMessage.assert_not_awaited()


def test_recovery_ping_is_limited_to_once_per_day(tmp_path):
    settings, db, agent, client = _setup(tmp_path)
    mark_unavailable(db, KIND_UPSTREAM, UPSTREAM)
    db.kv_set(back_notified_key(), "1")

    assert _tick(settings, db, agent, client) is False
    assert outage(db) == ""
    client.chat_postMessage.assert_not_awaited()


def test_daily_block_is_not_probed(tmp_path):
    settings, db, agent, client = _setup(tmp_path)
    mark_unavailable(db, KIND_DAILY, DAILY)

    assert _tick(settings, db, agent, client) is False
    agent.llm.chat.completions.create.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()

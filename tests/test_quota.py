from bot.ai.budget import utc_day
from bot.ai.quota import (
    COOLDOWN_KEY,
    KIND_DAILY,
    KIND_UNKNOWN,
    KIND_UPSTREAM,
    classify_quota_error,
    describe,
    mark_unavailable,
    notice_key,
    primary_status,
)
from bot.db import Database

UPSTREAM = (
    "Error code: 402 - {'error': {'message': 'Insufficient credits. Add more "
    "using https://openrouter.ai/settings/credits', 'code': 402, "
    "'metadata': {'limit_source': 'openrouter_credits'}}}"
)


def _db(tmp_path):
    db = Database(tmp_path / "quota.db")
    db.init()
    return db


def test_classify_daily_limit():
    assert classify_quota_error("429 Daily spending limit of $3 reached") == KIND_DAILY


def test_classify_upstream_billing():
    assert classify_quota_error(UPSTREAM) == KIND_UPSTREAM
    assert (
        classify_quota_error(
            "402: Your organization has no available TypeSafe API credits"
        )
        == KIND_UPSTREAM
    )


def test_classify_unknown_quota_and_non_quota():
    assert classify_quota_error("some quota problem") == KIND_UNKNOWN
    assert classify_quota_error("model not found") == ""


def test_daily_limit_blocks_until_utc_reset(tmp_path):
    db = _db(tmp_path)
    mark_unavailable(db, KIND_DAILY, "daily spending limit")
    blocked, kind = primary_status(db)
    assert blocked is True
    assert kind == KIND_DAILY
    assert db.kv_get(f"quota:hcai:{utc_day()}") == "empty"
    assert db.kv_get(COOLDOWN_KEY) is None
    assert describe(db) == "hacai quota: empty today"


def test_upstream_uses_a_short_cooldown(tmp_path):
    db = _db(tmp_path)
    mark_unavailable(db, KIND_UPSTREAM, UPSTREAM)
    blocked, kind = primary_status(db)
    assert blocked is True
    assert kind == KIND_UPSTREAM
    assert db.kv_get(f"quota:hcai:{utc_day()}") is None
    assert "upstream billing outage" in describe(db)


def test_expired_cooldown_unblocks_primary(tmp_path):
    db = _db(tmp_path)
    db.kv_set(COOLDOWN_KEY, "0")
    db.kv_set("quota:hcai:kind", KIND_UPSTREAM)
    blocked, kind = primary_status(db)
    assert blocked is False
    assert kind == ""
    assert db.kv_get(COOLDOWN_KEY) is None
    assert db.kv_get("quota:hcai:kind") is None


def test_notice_key_includes_day_and_reason():
    assert notice_key(KIND_DAILY) == f"quota:hcai:notified:{utc_day()}:daily"
    assert notice_key(KIND_UPSTREAM) == f"quota:hcai:notified:{utc_day()}:upstream"

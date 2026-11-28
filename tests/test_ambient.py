"""Runtime ambient-reply settings."""

from __future__ import annotations

from types import SimpleNamespace

from bot import ambient
from bot.db import Database


class _Settings(SimpleNamespace):
    def ai_allowed(self, channel_id: str | None) -> bool:
        return bool(channel_id) and (
            "*" in self.ai_channels or channel_id in self.ai_channels
        )


def _settings(**overrides) -> _Settings:
    values = {
        "ai_gate_enabled": True,
        "ai_channels": frozenset({"*"}),
        "ai_ambient_channels": frozenset({"C_ENV"}),
    }
    values.update(overrides)
    return _Settings(**values)


def test_env_defaults_apply_until_a_runtime_override(tmp_path):
    db = Database(str(tmp_path / "ambient.db"))
    db.init()
    settings = _settings()

    assert ambient.enabled(db) is True
    assert ambient.channels(settings, db) == frozenset({"C_ENV"})
    assert ambient.allowed(settings, db, "C_ENV") is True
    assert ambient.allowed(settings, db, "C_OTHER") is False


def test_runtime_whitelist_overrides_env(tmp_path):
    db = Database(str(tmp_path / "ambient.db"))
    db.init()
    settings = _settings()

    db.kv_set("config:ambient_channels", "C_ONE,C_TWO")
    assert ambient.channels(settings, db) == frozenset({"C_ONE", "C_TWO"})
    assert ambient.allowed(settings, db, "C_ONE") is True
    assert ambient.allowed(settings, db, "C_ENV") is False

    db.kv_set("config:ambient_channels", "*")
    assert ambient.allowed(settings, db, "C_ANY") is True

    # An explicitly empty value means nowhere, unlike an unset value.
    db.kv_set("config:ambient_channels", "")
    assert ambient.allowed(settings, db, "C_ANY") is False


def test_ping_only_master_switch_beats_the_whitelist(tmp_path):
    db = Database(str(tmp_path / "ambient.db"))
    db.init()
    settings = _settings()
    db.kv_set("config:ambient_channels", "*")

    db.kv_set("config:ambient_ai", "false")
    assert ambient.enabled(db) is False
    assert ambient.allowed(settings, db, "C_ANY") is False
    assert "ping-only" in ambient.describe(settings, db)

    db.kv_set("config:ambient_ai", "true")
    assert ambient.allowed(settings, db, "C_ANY") is True


def test_ambient_needs_the_gate_and_ai_access(tmp_path):
    db = Database(str(tmp_path / "ambient.db"))
    db.init()
    db.kv_set("config:ambient_channels", "*")

    assert ambient.allowed(_settings(ai_gate_enabled=False), db, "C_ANY") is False
    assert ambient.allowed(
        _settings(ai_channels=frozenset({"C_ALLOWED"})), db, "C_OTHER"
    ) is False

from bot.features.access import admin_channel_id
from bot.features.scheduler import _shift_hhmm
from bot.features.threads import has_usergroup_mention, wants_thread_anchor
from bot.runtime import runtime


def test_has_usergroup_mention():
    assert has_usergroup_mention("hey <!subteam^S0123456789> look here")
    assert has_usergroup_mention("<!subteam^S0123456789|designers>")
    assert not has_usergroup_mention("hey <@U0123456789>")
    assert not has_usergroup_mention("@designers")
    assert not has_usergroup_mention("")


def test_wants_thread_anchor():
    assert wants_thread_anchor("<!subteam^S0123456789> :thread:")
    assert wants_thread_anchor("<!subteam^S0123456789> 🧵")
    assert not wants_thread_anchor("<!subteam^S0123456789> hello")
    assert not wants_thread_anchor("")


def test_shift_hhmm():
    assert _shift_hhmm("20:00", -10) == "19:50"
    assert _shift_hhmm("00:05", -10) == "23:55"
    assert _shift_hhmm("09:30", 45) == "10:15"
    assert _shift_hhmm("23:30", 60) == "00:30"
    assert _shift_hhmm("nonsense", -10) == "00:00"


def test_admin_channel_id_resolution(tmp_path):
    from unittest.mock import MagicMock

    from bot.db import Database

    db = Database(str(tmp_path / "test_access.db"))
    db.init()
    mock_settings = MagicMock()
    mock_settings.admin_channel_id = "C_DEFAULT_ADMIN"

    old_db = runtime.db
    old_settings = runtime.settings
    try:
        runtime.db = db
        runtime.settings = mock_settings

        # Default fallback
        assert admin_channel_id() == "C_DEFAULT_ADMIN"

        # Explicit channel in db
        db.kv_set("config:admin_channel_id", "C_CUSTOM_ADMIN")
        assert admin_channel_id() == "C_CUSTOM_ADMIN"

        # 'dm' or 'dms' routes to None (meaning admin DMs)
        db.kv_set("config:admin_channel_id", "dm")
        assert admin_channel_id() is None

        db.kv_set("config:admin_channel_id", "dms")
        assert admin_channel_id() is None
    finally:
        runtime.db = old_db
        runtime.settings = old_settings

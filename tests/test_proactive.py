from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from bot.db import Database
from bot.features.proactive import is_quiet_now, note_proactive, proactive_available
from bot.utils.tz import tzinfo


def _settings(**overrides):
    defaults = {
        "quiet_hours_start": 23 * 60,
        "quiet_hours_end": 7 * 60 + 30,
        "proactive_daily_cap": 2,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_quiet_hours_wrap_midnight():
    settings = _settings()
    midnight = datetime(2026, 11, 14, 0, 30, tzinfo=tzinfo())
    late = datetime(2026, 11, 14, 23, 45, tzinfo=tzinfo())
    noon = datetime(2026, 11, 14, 12, 0, tzinfo=tzinfo())
    assert is_quiet_now(settings, midnight) is True
    assert is_quiet_now(settings, late) is True
    assert is_quiet_now(settings, noon) is False
    assert is_quiet_now(_settings(quiet_hours_start=-1, quiet_hours_end=-1), noon) is False


def test_proactive_daily_cap(tmp_path):
    db = Database(str(tmp_path / "proactive.db"))
    db.init()
    settings = _settings()
    now = datetime(2026, 11, 14, 12, 0, tzinfo=tzinfo())
    assert proactive_available(db, settings, now) is True
    note_proactive(db, settings, now)
    note_proactive(db, settings, now)
    assert proactive_available(db, settings, now) is False
    # a new day resets the budget
    tomorrow = datetime(2026, 11, 15, 12, 0, tzinfo=tzinfo())
    assert proactive_available(db, settings, tomorrow) is True

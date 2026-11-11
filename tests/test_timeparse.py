from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.utils.timeparse import next_occurrence, parse_when

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)  # a monday


def parse(text):
    return parse_when(text, now=NOW, tz=UTC)


def test_relative_minutes():
    result = parse("10m stretch")
    assert result is not None
    assert result.when == datetime(2026, 11, 9, 12, 10, tzinfo=timezone.utc)
    assert result.rest == "stretch"
    assert result.recurrence is None


def test_relative_with_in_and_hours():
    result = parse("in 2 hours meeting")
    assert result is not None
    assert result.when == datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)
    assert result.rest == "meeting"


def test_compound_duration():
    result = parse("1d 2h 30m big thing")
    assert result is not None
    assert result.when == datetime(2026, 11, 10, 14, 30, tzinfo=timezone.utc)
    assert result.rest == "big thing"


def test_tomorrow():
    result = parse("tomorrow 09:00 standup")
    assert result is not None
    assert result.when == datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    assert result.rest == "standup"


def test_daily():
    result = parse("daily 08:30 good morning")
    assert result is not None
    assert result.recurrence == "daily:08:30"
    assert result.when == datetime(2026, 11, 10, 8, 30, tzinfo=timezone.utc)
    assert result.rest == "good morning"


def test_weekly():
    result = parse("weekly mon 18:00 recap")
    assert result is not None
    assert result.recurrence == "weekly:0:18:00"
    assert result.when == datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)


def test_every():
    result = parse("every 2h drink water")
    assert result is not None
    assert result.recurrence == "every:120"
    assert result.when == datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)
    assert result.rest == "drink water"


def test_weekday_rolls_to_next_week():
    result = parse("mon 09:00 weekly sync")
    assert result is not None
    assert result.when == datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def test_iso():
    result = parse("2026-11-10 09:00 thing")
    assert result is not None
    assert result.when == datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


def test_clock():
    result = parse("18:00 dinner")
    assert result is not None
    assert result.when == datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)


def test_requires_body():
    assert parse("10m") is None
    assert parse("buy milk") is None


def test_next_occurrence_daily():
    after = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    assert next_occurrence("daily:08:30", after, UTC) == datetime(
        2026, 11, 10, 8, 30, tzinfo=timezone.utc
    )


def test_next_occurrence_weekly():
    after = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    assert next_occurrence("weekly:0:18:00", after, UTC) == datetime(
        2026, 11, 9, 18, 0, tzinfo=timezone.utc
    )


def test_next_occurrence_every():
    after = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    assert next_occurrence("every:10", after, UTC) == datetime(
        2026, 11, 9, 12, 10, tzinfo=timezone.utc
    )


def test_12h_and_standalone_times():
    r1 = parse("tomorrow at 12am respond to email")
    assert r1 is not None
    assert r1.when == datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
    assert r1.rest == "respond to email"

    r2 = parse("tomorrow midnight check mail")
    assert r2 is not None
    assert r2.when == datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)

    r3 = parse("3pm coffee")
    assert r3 is not None
    assert r3.when == datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)

    r4 = parse("12:30 pm lunch")
    assert r4 is not None
    assert r4.when == datetime(2026, 11, 9, 12, 30, tzinfo=timezone.utc)

    r5 = parse_when("tomorrow 00:00", now=NOW, tz=UTC, require_rest=False)
    assert r5 is not None
    assert r5.when == datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
    assert r5.rest == ""

    r6 = parse_when("in 85m", now=NOW, tz=UTC, require_rest=False)
    assert r6 is not None
    assert r6.when == datetime(2026, 11, 9, 13, 25, tzinfo=timezone.utc)

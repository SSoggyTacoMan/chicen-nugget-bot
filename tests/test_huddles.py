import asyncio
from unittest.mock import AsyncMock, patch

from bot.features import huddles


def test_latest_huddle_picks_newest_with_transcript():
    async def fake_call(http, method, token, cookie, **fields):
        assert method == "huddles.history"
        return {
            "ok": True,
            "huddles": [
                {"id": "H1", "date_start": 100, "transcript_file_id": "F1"},
                {"id": "H2", "date_start": 200, "transcript_file_id": ""},
                {"id": "H3", "date_start": 300, "transcript_file_id": "F3"},
            ],
        }

    with patch.object(huddles, "_call", new=fake_call):
        huddle = asyncio.run(
            huddles.latest_huddle(AsyncMock(), "xoxc-token", "cookie", "C1")
        )
    assert huddle is not None
    assert huddle["id"] == "H3"


def test_fetch_transcript_resolves_canvas_and_lines():
    async def fake_call(http, method, token, cookie, **fields):
        assert method == "files.info"
        if fields["file"] == "CANVAS":
            return {"ok": True, "file": {"huddle_transcript_file_id": "TX"}}
        if fields["file"] == "TX":
            return {
                "ok": True,
                "file": {
                    "huddle_transcription": {
                        "lines": [
                            {"user_id": "U1", "start_time_ms": 0, "contents": "hi"},
                            {"user_id": "U2", "start_time_ms": 5000, "contents": "yo"},
                        ]
                    }
                },
            }
        raise AssertionError(fields)

    with patch.object(huddles, "_call", new=fake_call):
        lines = asyncio.run(
            huddles.fetch_transcript(
                AsyncMock(),
                "xoxc-token",
                "cookie",
                {"transcript_file_id": "CANVAS"},
            )
        )
    assert [line["contents"] for line in lines] == ["hi", "yo"]


def test_format_transcript_merges_consecutive_speakers():
    lines = [
        {"user_id": "U1", "start_time_ms": 0, "contents": "hello"},
        {"user_id": "U1", "start_time_ms": 5000, "contents": "again"},
        {"user_id": "U2", "start_time_ms": 65000, "contents": "bye"},
    ]

    async def fake_user_info(client, user_id):
        return {"profile": {"display_name": "alice" if user_id == "U1" else "bob"}}

    with patch.object(huddles, "user_info", new=fake_user_info):
        text = asyncio.run(huddles.format_transcript(lines, AsyncMock()))
    assert "*alice* [00:00]: hello again" in text
    assert "*bob* [01:05]: bye" in text


def test_credentials_require_xoxc_and_cookie(monkeypatch):
    from types import SimpleNamespace
    from bot.runtime import runtime

    monkeypatch.setattr(
        runtime,
        "settings",
        SimpleNamespace(
            slack_web_token="xoxb-nope",
            slack_user_token="xoxp-nope",
            slack_web_cookie="",
            slack_user_cookie="",
        ),
    )
    assert huddles._credentials() is None

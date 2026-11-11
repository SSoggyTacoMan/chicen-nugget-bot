import asyncio
from unittest.mock import AsyncMock

from bot.ai.tools import Toolbox
from bot.config import Settings


def _ctx(client):
    from bot.ai.tools import ToolContext

    return ToolContext(
        db=None,
        settings=Settings.__new__(Settings),
        client=client,
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_OWNER",
        channel_id="C1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )


def test_make_chart_uploads_png():
    async def run():
        client = AsyncMock()
        box = Toolbox(Settings.__new__(Settings))
        out = await box.execute(
            "make_chart",
            {
                "type": "bar",
                "title": "signups",
                "labels": ["w1", "w2", "w3"],
                "values": [3, 7, 5],
            },
            _ctx(client),
        )
        assert "uploaded" in out
        upload = client.files_upload_v2.call_args[1]
        assert upload["filename"].endswith(".png")
        assert upload["file"][:8] == b"\x89PNG\r\n\x1a\n"

    asyncio.run(run())


def test_make_deck_uploads_pptx():
    async def run():
        client = AsyncMock()
        box = Toolbox(Settings.__new__(Settings))
        out = await box.execute(
            "make_deck",
            {
                "title": "launch plan",
                "slides": [
                    {"title": "goals", "bullets": ["ship v1", "get users"]},
                    {"title": "risks", "bullets": ["time"]},
                ],
            },
            _ctx(client),
        )
        assert "deck uploaded" in out
        upload = client.files_upload_v2.call_args[1]
        assert upload["filename"].endswith(".pptx")
        assert upload["file"][:2] == b"PK"

    asyncio.run(run())


def test_make_chart_rejects_mismatched_labels():
    async def run():
        client = AsyncMock()
        box = Toolbox(Settings.__new__(Settings))
        out = await box.execute(
            "make_chart",
            {"type": "bar", "labels": ["a", "b"], "values": [1]},
            _ctx(client),
        )
        assert "same length" in out
        assert not client.files_upload_v2.called

    asyncio.run(run())

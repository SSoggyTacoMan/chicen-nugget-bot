from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.ai.tools import Toolbox, ToolContext


class FakeResponse:
    def __init__(self, status_code: int = 201, data: dict | None = None):
        self.status_code = status_code
        self._data = data or {}
        self.content = b"json"
        self.text = "error"

    def json(self):
        return self._data


class FakeClient:
    def __init__(self):
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _record(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(data={"number": 42, "html_url": "https://github.com/o/r/pull/42", "state": "open", "merged": True})

    async def post(self, url, **kwargs):
        return await self._record("POST", url, **kwargs)

    async def patch(self, url, **kwargs):
        return await self._record("PATCH", url, **kwargs)

    async def put(self, url, **kwargs):
        return await self._record("PUT", url, **kwargs)


def _ctx(**overrides) -> ToolContext:
    settings = SimpleNamespace(
        github_token="ghp_test",
        github_username="owner",
        repo_url="https://github.com/owner/repo",
    )
    defaults = {
        "db": MagicMock(),
        "settings": settings,
        "client": AsyncMock(),
        "user_client": None,
        "bot_user_id": "U_BOT",
        "user_id": "U_OWNER",
        "channel_id": "D1",
        "thread_ts": None,
        "is_dm": True,
        "is_owner": True,
        "is_admin": True,
    }
    defaults.update(overrides)
    return ToolContext(**defaults)


def test_github_write_creates_pr_and_guards_merge():
    box = Toolbox(MagicMock())
    ctx = _ctx()
    fake = FakeClient()
    ctx.db.kv_get.return_value = ""

    async def run():
        with patch("bot.ai.tools.httpx.AsyncClient", return_value=fake):
            created = await box.execute(
                "github_write",
                {
                    "action": "create_pr",
                    "title": "fix the thing",
                    "head": "fix-branch",
                    "base": "main",
                    "body": "details",
                },
                ctx,
            )
        assert '"number": 42' in created
        assert fake.calls[0]["url"] == "https://api.github.com/repos/owner/repo/pulls"
        assert fake.calls[0]["json"]["head"] == "fix-branch"

        refused = await box.execute(
            "github_write", {"action": "merge", "number": 42}, ctx
        )
        assert "confirm=true" in refused

        with patch("bot.ai.tools.httpx.AsyncClient", return_value=fake):
            merged = await box.execute(
                "github_write",
                {"action": "merge", "number": 42, "confirm": True, "method": "squash"},
                ctx,
            )
        assert '"merged": true' in merged
        assert fake.calls[-1]["method"] == "PUT"
        assert fake.calls[-1]["json"]["merge_method"] == "squash"


def test_github_write_requires_dm_and_token():
    box = Toolbox(MagicMock())
    channel_ctx = _ctx(is_dm=False)
    denied = asyncio.run(
        box.execute(
            "github_write", {"action": "comment", "number": 1, "body": "hi"}, channel_ctx
        )
    )
    assert "direct messages" in denied

    no_token_settings = SimpleNamespace(
        github_token="", github_username="", repo_url="https://github.com/owner/repo"
    )
    no_token_ctx = _ctx(settings=no_token_settings)
    no_token_ctx.db.kv_get.return_value = ""
    missing = asyncio.run(
        box.execute(
            "github_write", {"action": "comment", "number": 1, "body": "hi"}, no_token_ctx
        )
    )
    assert "github isn't connected" in missing

from __future__ import annotations

import asyncio
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

from bot.ai.tools import Toolbox, ToolContext


def _ctx(**overrides) -> ToolContext:
    defaults = {
        "db": MagicMock(),
        "settings": MagicMock(),
        "client": AsyncMock(),
        "user_client": None,
        "bot_user_id": "U_BOT",
        "user_id": "U_OWNER",
        "channel_id": "C_HERE",
        "thread_ts": None,
        "is_dm": True,
        "is_owner": True,
        "is_admin": True,
    }
    defaults.update(overrides)
    return ToolContext(**defaults)


def test_search_slack_messages_files_and_local_fallback():
    box = Toolbox(MagicMock())
    user_client = MagicMock()
    user_client.search_messages = AsyncMock(
        return_value={
            "messages": {
                "matches": [
                    {
                        "text": "deploy is green",
                        "user": "U1",
                        "channel": {"id": "C1", "name": "dev"},
                        "permalink": "https://example.slack.com/archives/C1/p1",
                        "ts": "1.0",
                    }
                ]
            }
        }
    )
    user_client.search_files = AsyncMock(
        return_value={
            "files": {
                "matches": [
                    {
                        "name": "screenshot.png",
                        "filetype": "png",
                        "permalink": "https://files.example/screenshot.png",
                    }
                ]
            }
        }
    )
    user_client.search_all = AsyncMock(
        return_value={
            "messages": {"matches": [{"text": "hello", "channel": {"id": "C1"}}]},
            "files": {"matches": [{"name": "deck.pptx", "filetype": "pptx"}]},
        }
    )

    async def run():
        ctx = _ctx(user_client=user_client)
        messages = await box.execute("search_slack", {"query": "deploy"}, ctx)
        assert "deploy is green" in messages

        files = await box.execute(
            "search_slack", {"query": "screenshot", "kind": "files"}, ctx
        )
        assert "screenshot.png" in files

        everything = await box.execute(
            "search_slack", {"query": "hello", "kind": "all"}, ctx
        )
        assert "deck.pptx" in everything

        # No user session: local history is still searchable.
        local_ctx = _ctx(user_client=None)
        local_ctx.db.search_messages.return_value = [
            {
                "conversation": "ch:C1:1.0",
                "user_id": "U1",
                "text": "local note",
                "created_at": "today",
            }
        ]
        local = await box.execute("search_slack", {"query": "local"}, local_ctx)
        assert "local history" in local
        assert "local note" in local

        empty_ctx = _ctx(user_client=None)
        empty_ctx.db.search_messages.return_value = []
        empty = await box.execute("search_slack", {"query": "nope"}, empty_ctx)
        assert "no slack results" in empty

    asyncio.run(run())


def test_manage_channel_join_leave_list_and_create():
    box = Toolbox(MagicMock())

    async def run():
        client = AsyncMock()
        client.conversations_join = AsyncMock(return_value={"ok": True})
        client.conversations_leave = AsyncMock(return_value={"ok": True})
        client.conversations_create = AsyncMock(
            return_value={"channel": {"id": "C_NEW", "name": "new-room", "is_private": False}}
        )
        ctx = _ctx(client=client, is_dm=False)

        channel = {"id": "C1", "name": "dev", "is_member": False}
        with patch("bot.ai.tools.all_channels", AsyncMock(return_value=[channel])), \
             patch("bot.ai.tools.channel_info", AsyncMock(return_value={"id": "C1", "name": "dev"})):
            joined = await box.execute(
                "manage_channel", {"action": "join", "channel": "C1"}, ctx
            )
            assert "joined <#C1>" in joined
            client.conversations_join.assert_called_once_with(channel="C1")

            left = await box.execute(
                "manage_channel", {"action": "leave", "channel": "C1", "as": "bot"}, ctx
            )
            assert "left <#C1>" in left
            client.conversations_leave.assert_called_once_with(channel="C1")

            listed = await box.execute("manage_channel", {"action": "list"}, ctx)
            assert "dev" in listed

        created = await box.execute(
            "manage_channel", {"action": "create", "channel": "New Room"}, ctx
        )
        assert "new-room" in created
        client.conversations_create.assert_called_once_with(
            name="new-room", is_private=False
        )

        denied = await box.execute(
            "manage_channel",
            {"action": "join", "channel": "C1"},
            _ctx(client=client, is_owner=False, is_admin=False),
        )
        assert "owner/admin" in denied

    asyncio.run(run())


def test_api_request_uses_stored_token_and_guards_paths():
    from bot.api_keys import save_api_token

    class FakeResponse:
        status_code = 200
        is_success = True
        text = '{"ok": true}'

        def json(self):
            return {"ok": True}

    class FakeClient:
        calls: ClassVar[list[dict]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, **kwargs):
            FakeClient.calls.append({"method": method, "url": url, **kwargs})
            return FakeResponse()

    box = Toolbox(MagicMock())
    db = MagicMock()
    db.kv_get.return_value = "vercel-token"

    async def run():
        ctx = _ctx(db=db)
        with patch("bot.ai.tools.httpx.AsyncClient", return_value=FakeClient()):
            result = await box.execute(
                "api_request",
                {"service": "vercel", "method": "GET", "path": "/v6/deployments"},
                ctx,
            )
        assert '"status": 200' in result
        assert FakeClient.calls[0]["url"] == "https://api.vercel.com/v6/deployments"
        assert FakeClient.calls[0]["headers"]["Authorization"] == "Bearer vercel-token"

        bad_path = await box.execute(
            "api_request",
            {"service": "vercel", "path": "https://evil.example/steal"},
            ctx,
        )
        assert "relative API path" in bad_path

        channel_ctx = _ctx(db=db, is_dm=False)
        denied = await box.execute(
            "api_request", {"service": "vercel", "path": "/v6/deployments"}, channel_ctx
        )
        assert "direct messages" in denied

        # Storage round trip: db values are used when no env token is present.
        save_api_token(db, "vercel", "new-token")
        db.kv_set.assert_any_call("config:api_token:vercel", "new-token")

    asyncio.run(run())


def test_owner_posting_checks_granted_customize_scope():
    from bot import slack_api

    class FakeResponse(dict):
        def __init__(self, data, headers):
            super().__init__(data)
            self.headers = headers

    class FakeClient:
        token = "xoxb-scope-test"

        async def auth_test(self):
            return FakeResponse(
                {"ok": True, "bot_id": "B1"},
                {"x-oauth-scopes": "chat:write,users:read"},
            )

        async def bots_info(self, bot):
            return {"bot": {"app_id": "A123"}}

        async def chat_postMessage(self, **kwargs):
            raise AssertionError("should not post when the scope is missing")

    slack_api._scope_cache.clear()
    slack_api._app_id_cache.clear()
    box = Toolbox(MagicMock())
    ctx = _ctx(client=FakeClient(), bot_client=FakeClient())

    async def run():
        result = await box.execute(
            "post_message",
            {"channel": "C1", "text": "hello", "as_user": True},
            ctx,
        )
        assert "chat:write.customize" in result
        assert "reinstall" in result
        assert "A123" in result

    asyncio.run(run())

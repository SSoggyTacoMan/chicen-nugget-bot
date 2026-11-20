from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from bot.ai.tools import Toolbox, ToolContext


def _ctx(tmp_path, *, user_client=None, channel_id="C1"):
    settings = MagicMock()
    db = MagicMock()
    db.search_messages.return_value = []
    client = AsyncMock()
    client.conversations_list.return_value = {
        "channels": [{"id": "C_CONF", "name": "confessions"}]
    }
    return ToolContext(
        db=db,
        settings=settings,
        client=client,
        user_client=user_client,
        bot_user_id="U_BOT",
        user_id="U_OWNER",
        channel_id=channel_id,
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )


def test_search_falls_back_to_channel_history_when_slack_search_misses():
    user_client = AsyncMock()
    user_client.search_messages.return_value = {"messages": {"matches": []}}
    user_client.conversations_history.return_value = {
        "messages": [
            {"text": "42252: something else", "user": "U_OWL", "ts": "2.0"},
            {"text": "42253: if owl gained consciousness...", "user": "U_OWL", "ts": "1.0"},
        ],
        "has_more": False,
    }
    ctx = _ctx(None, user_client=user_client)
    ctx.client.conversations_history.return_value = (
        user_client.conversations_history.return_value
    )

    async def run():
        result = await Toolbox(ctx.settings).execute(
            "search_slack", {"query": "42253", "channel": "#confessions"}, ctx
        )
        return json.loads(result)

    payload = asyncio.run(run())
    assert payload["source"] == "recent history scan of confessions"
    assert payload["messages"][0]["ts"] == "1.0"
    assert "42253" in payload["messages"][0]["text"]
    assert all(row["ts"] != "2.0" for row in payload["messages"])


def test_search_keeps_workspace_results_without_scanning_history():
    user_client = AsyncMock()
    user_client.search_messages.return_value = {
        "messages": {
            "matches": [
                {
                    "text": "42253: here it is",
                    "user": "U_OWL",
                    "channel": {"id": "C_CONF", "name": "confessions"},
                    "ts": "9.0",
                    "permalink": "https://example.slack.com/archives/C_CONF/p9",
                }
            ]
        }
    }
    ctx = _ctx(None, user_client=user_client)

    async def run():
        result = await Toolbox(ctx.settings).execute(
            "search_slack", {"query": "42253", "channel": "#confessions"}, ctx
        )
        return json.loads(result)

    payload = asyncio.run(run())
    assert payload[0]["text"] == "42253: here it is"
    ctx.client.conversations_history.assert_not_awaited()


def test_search_reads_channel_from_in_operator():
    user_client = AsyncMock()
    user_client.search_messages.return_value = {"messages": {"matches": []}}
    user_client.conversations_history.return_value = {
        "messages": [{"text": "42253: yes", "user": "U_OWL", "ts": "1.0"}],
        "has_more": False,
    }
    ctx = _ctx(None, user_client=user_client)
    ctx.client.conversations_history.return_value = (
        user_client.conversations_history.return_value
    )

    async def run():
        result = await Toolbox(ctx.settings).execute(
            "search_slack", {"query": "42253 in:#confessions"}, ctx
        )
        return json.loads(result)

    payload = asyncio.run(run())
    assert payload["messages"][0]["text"] == "42253: yes"


def test_search_reports_the_miss_instead_of_going_quiet():
    user_client = AsyncMock()
    user_client.search_messages.return_value = {"messages": {"matches": []}}
    user_client.conversations_history.return_value = {"messages": [], "has_more": False}
    ctx = _ctx(None, user_client=user_client)
    ctx.client.conversations_history.return_value = (
        user_client.conversations_history.return_value
    )

    async def run():
        return await Toolbox(ctx.settings).execute(
            "search_slack", {"query": "42253", "channel": "#confessions"}, ctx
        )

    result = asyncio.run(run())
    assert "no slack results" in result
    assert "42253" in result
    assert "#confessions" in result

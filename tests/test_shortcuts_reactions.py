from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from bot.features.agent import _last_reaction, on_reaction_added
from bot.features.shortcuts import _run_shortcut
from bot.runtime import runtime


def test_reaction_research_routes_to_agent():
    old_agent, old_settings, old_db = runtime.agent, runtime.settings, runtime.db
    runtime.agent = MagicMock()
    runtime.agent.reply = AsyncMock(return_value="the answer")
    runtime.settings = MagicMock()
    runtime.settings.ai_enabled = True
    runtime.settings.ai_allowed.return_value = True
    runtime.settings.bot_user_id = "UBOT"
    runtime.settings.owner_user_id = "UOWNER"
    runtime.db = MagicMock()
    _last_reaction.clear()
    client = AsyncMock()

    event = {
        "type": "reaction_added",
        "user": "U1",
        "item_user": "U2",
        "reaction": "mag",
        "item": {"type": "message", "channel": "C1", "ts": "10.0"},
    }

    async def run():
        with patch("bot.features.agent._reaction_message", AsyncMock(return_value=("why is this failing", "9.0"))), \
             patch("bot.features.agent.user_retry_after", return_value=None), \
             patch("bot.features.agent.post", AsyncMock()) as mock_post:
            await on_reaction_added(event, client)
        runtime.agent.reply.assert_called_once()
        kwargs = runtime.agent.reply.call_args[1]
        assert kwargs["trigger"] == "reaction"
        assert kwargs["thread_ts"] == "9.0"
        assert "research this message" in kwargs["text"]
        mock_post.assert_called_once()
        assert mock_post.call_args[1]["thread_ts"] == "9.0"

    try:
        asyncio.run(run())
    finally:
        runtime.agent, runtime.settings, runtime.db = old_agent, old_settings, old_db


def test_reaction_pin_uses_pins_api():
    old_settings, old_agent = runtime.settings, runtime.agent
    runtime.settings = MagicMock()
    runtime.agent = MagicMock()
    runtime.settings.ai_enabled = True
    runtime.settings.ai_allowed.return_value = True
    runtime.settings.bot_user_id = "UBOT"
    runtime.settings.owner_user_id = "UOWNER"
    _last_reaction.clear()
    client = AsyncMock()
    client.pins_add = AsyncMock(return_value={"ok": True})

    event = {
        "type": "reaction_added",
        "user": "U1",
        "item_user": "U2",
        "reaction": "pushpin",
        "item": {"type": "message", "channel": "C1", "ts": "10.0"},
    }

    async def run():
        with patch("bot.features.agent._reaction_message", AsyncMock(return_value=("", "10.0"))):
            await on_reaction_added(event, client)
        client.pins_add.assert_called_once_with(channel="C1", timestamp="10.0")

    try:
        asyncio.run(run())
    finally:
        runtime.settings = old_settings
        runtime.agent = old_agent


def test_message_shortcut_runs_agent():
    old_agent, old_settings, old_db = runtime.agent, runtime.settings, runtime.db
    runtime.agent = MagicMock()
    runtime.agent.reply = AsyncMock(return_value="short answer")
    runtime.settings = MagicMock()
    runtime.settings.ai_enabled = True
    runtime.settings.ai_allowed.return_value = True
    runtime.db = MagicMock()
    client = AsyncMock()

    shortcut = {
        "channel": {"id": "C1"},
        "user": {"id": "U1"},
        "message": {"text": "why is this broken", "ts": "20.0", "thread_ts": "19.0"},
    }

    async def run():
        with patch("bot.features.shortcuts.user_retry_after", return_value=None), \
             patch("bot.features.shortcuts.post", AsyncMock()) as mock_post:
            await _run_shortcut(shortcut, client, "explain this:\n\n")
        runtime.agent.reply.assert_called_once()
        kwargs = runtime.agent.reply.call_args[1]
        assert kwargs["trigger"] == "shortcut"
        assert kwargs["thread_ts"] == "19.0"
        assert kwargs["text"].startswith("explain this:")
        mock_post.assert_called_once()
        assert mock_post.call_args[1]["thread_ts"] == "19.0"

    try:
        asyncio.run(run())
    finally:
        runtime.agent, runtime.settings, runtime.db = old_agent, old_settings, old_db

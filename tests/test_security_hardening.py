from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from bot.ai.tools import Toolbox, ToolContext
from bot.db import Database
from bot.slack_api import is_private_host


def _ctx(**overrides) -> ToolContext:
    defaults = {
        "db": MagicMock(),
        "settings": MagicMock(),
        "client": AsyncMock(),
        "user_client": None,
        "bot_user_id": "U_BOT",
        "user_id": "U_GUEST",
        "channel_id": "C_HERE",
        "thread_ts": None,
        "is_dm": False,
        "is_owner": False,
        "is_admin": False,
    }
    defaults.update(overrides)
    return ToolContext(**defaults)


def test_guest_tool_schema_excludes_owner_tools():
    box = Toolbox(MagicMock())
    guest = {item["function"]["name"] for item in box.definitions_for(is_owner=False)}
    owner = {item["function"]["name"] for item in box.definitions_for(is_owner=True)}

    assert "roll_dice" in guest
    assert "get_weather" in guest
    # High-risk capabilities must never be advertised to a guest.
    for name in ("sandbox", "run_code", "browser", "github_info", "github_write", "api_request"):
        assert name not in guest
        assert name in owner
    assert len(guest) < len(owner)
    # The filtered list is cached rather than rebuilt for every completion.
    assert box.definitions_for(is_owner=False) is box.definitions_for(is_owner=False)


def test_github_info_is_owner_only():
    box = Toolbox(MagicMock())
    result = asyncio.run(box.execute("github_info", {"action": "repo", "target": "o/r"}, _ctx()))
    assert "owner/admin" in result


def test_lookup_user_hides_email_from_guests():
    box = Toolbox(MagicMock())
    user = {
        "id": "U12345678",
        "name": "maaren",
        "real_name": "Maaren",
        "profile": {"display_name": "maaren", "email": "maaren@example.com"},
    }
    with patch("bot.ai.tools.user_info", new=AsyncMock(return_value=user)):
        guest = asyncio.run(
            box.execute("lookup_user", {"query": "U12345678"}, _ctx())
        )
        owner = asyncio.run(
            box.execute(
                "lookup_user",
                {"query": "U12345678"},
                _ctx(is_owner=True, is_admin=True),
            )
        )
    assert "email" not in guest
    assert "maaren@example.com" in owner


def test_ssrf_guard_blocks_shared_and_metadata_addresses():
    async def check():
        assert await is_private_host("100.64.0.1")  # CGNAT
        assert await is_private_host("169.254.169.254")  # metadata
        assert await is_private_host("::ffff:127.0.0.1")  # IPv4-mapped loopback
        assert not await is_private_host("8.8.8.8")

    asyncio.run(check())


def test_database_reuses_and_closes_connections(tmp_path):
    db = Database(tmp_path / "reuse.db")
    db.init()
    assert db._connect() is db._connect()
    db.kv_set("k", "v")
    db.close()
    assert db.kv_get("k") == "v"
    db.close()


def test_mac_bridge_requires_a_token():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "mac_companion.py"
    spec = importlib.util.spec_from_file_location("mac_companion", path)
    assert spec and spec.loader
    mac_companion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mac_companion)

    handler = object.__new__(mac_companion.BridgeHandler)
    handler.headers = {"Authorization": "Bearer anything"}
    mac_companion.BridgeHandler.token = ""
    assert handler._authorized() is False

    mac_companion.BridgeHandler.token = "secret"
    handler.headers = {"Authorization": "Bearer wrong"}
    assert handler._authorized() is False
    handler.headers = {"Authorization": "Bearer secret"}
    assert handler._authorized() is True

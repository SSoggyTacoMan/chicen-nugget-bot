"""Chat text that behaves like a /nugget command."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from bot.ai.tools import Toolbox, ToolContext
from bot.features import dm_chats, router, setup


def _ctx(client=None, **overrides) -> ToolContext:
    defaults = {
        "db": MagicMock(),
        "settings": MagicMock(),
        "client": client or AsyncMock(),
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


def test_natural_command_shapes():
    assert router.normalize_message_command("nugget start secret chat") == "secret"
    assert router.normalize_message_command("/nugget secret about rust") == "secret about rust"
    assert router.normalize_message_command("nugget set up welcomer") == "setup welcomer"
    assert router.normalize_message_command("nugget start a new chat about rust") == (
        "newchat about rust"
    )
    assert router.normalize_message_command("nugget remind 10m standup") == (
        "remind 10m standup"
    )
    assert router.normalize_message_command("nugget do the thing") is None


def test_typo_and_phrasing_normalization():
    assert router.normalize_message_command("nugget setp welcomer") == "setup welcomer"
    assert router.normalize_message_command("nugget secert chat") == "secret"
    assert router.normalize_message_command("nugget remnd me in 10m stretch") == (
        "remind in 10m stretch"
    )
    assert router.normalize_message_command("nugget start the timer for 10 minutes") == (
        "timer 10 minutes"
    )
    assert router.normalize_message_command("nugget help me debug") is None
    assert router.normalize_message_command("nugget help") == "help"


def test_bare_commands_only_when_allowed():
    assert router.normalize_message_command("setup welcome") is None
    assert router.normalize_message_command("setup welcome", allow_bare=True) == "setup welcome"


def test_secret_intent_understands_the_nugget_prefix():
    assert dm_chats.secret_intent("nugget start secret chat") == ("start", "")
    assert dm_chats.secret_intent("/nugget secret about rust") == ("start", "about rust")
    assert dm_chats.secret_intent("nugget end") == ("end", "")
    assert dm_chats.secret_intent("nugget end --burn") == ("burn", "")
    assert dm_chats.secret_intent("start a secret chat") == ("start", "")
    assert dm_chats.secret_intent("unrelated") is None


def test_chat_setup_guide_covers_welcome():
    script = setup.chat_setup_script("welcomer")
    assert script is not None
    assert "guided chat setup: welcome & ping group" in script
    assert "watched_channel_ids" in script
    assert "welcome_dm_enabled" in script
    assert "one short question per message" in script.lower()
    assert setup.chat_setup_area("set up welcomer") is None
    assert setup.chat_setup_area("Voice") == "voice"


def test_nugget_command_starts_a_guided_setup():
    box = Toolbox(MagicMock())
    out = asyncio.run(
        box.execute(
            "nugget_command",
            {"command": "nugget set up welcomer"},
            _ctx(),
        )
    )
    assert "guided chat setup: welcome & ping group" in out
    assert "watched_channel_ids" in out


def test_nugget_command_keeps_setup_out_of_channels():
    box = Toolbox(MagicMock())
    out = asyncio.run(
        box.execute(
            "nugget_command",
            {"command": "setup welcome"},
            _ctx(channel_id="C1", is_dm=False),
        )
    )
    assert "private" in out


def test_nugget_command_runs_a_real_subcommand():
    async def run():
        client = AsyncMock()
        client.chat_postMessage.return_value.data = {"ts": "1"}
        box = Toolbox(MagicMock())
        out = await box.execute(
            "nugget_command",
            {"command": "nugget fact"},
            _ctx(client),
        )
        assert "ran `/nugget fact`" in out
        assert client.chat_postMessage.await_count == 1
        assert "nugget fact" in client.chat_postMessage.await_args.kwargs["text"]

    asyncio.run(run())


def test_nugget_command_requires_confirmation_for_destructive_commands():
    async def run():
        client = AsyncMock()
        box = Toolbox(MagicMock())
        out = await box.execute(
            "nugget_command",
            {"command": "nugget forget all"},
            _ctx(client),
        )
        assert "destructive" in out
        client.chat_postMessage.assert_not_awaited()

    asyncio.run(run())


def test_nugget_command_rejects_recursive_ai_commands():
    box = Toolbox(MagicMock())
    out = asyncio.run(box.execute("nugget_command", {"command": "ask hi"}, _ctx()))
    assert "another AI turn" in out


def test_nugget_command_is_owner_only():
    box = Toolbox(MagicMock())
    out = asyncio.run(
        box.execute(
            "nugget_command",
            {"command": "nugget fact"},
            _ctx(is_owner=False, is_admin=False),
        )
    )
    assert "owner" in out


def test_nugget_command_posts_with_the_active_identity():
    async def run():
        boi = AsyncMock()
        boi.chat_postMessage.return_value.data = {"ts": "1"}
        bot = AsyncMock()
        box = Toolbox(MagicMock())
        out = await box.execute(
            "nugget_command",
            {"command": "nugget fact"},
            _ctx(boi, bot_client=bot),
        )
        assert "ran `/nugget fact`" in out
        boi.chat_postMessage.assert_awaited()
        bot.chat_postMessage.assert_not_awaited()

    asyncio.run(run())


def test_dm_reminders_deliver_as_boi(monkeypatch):
    from bot.features import scheduler
    from bot.runtime import runtime

    calls = []

    async def fake_post(client, channel, text, **kwargs):
        calls.append((client, channel, text))
        return {"ts": "1"}

    monkeypatch.setattr(scheduler, "post", fake_post)
    monkeypatch.setattr(runtime, "client", object())
    monkeypatch.setattr(runtime, "user_client", object())

    asyncio.run(
        scheduler._deliver(
            {"id": 1, "kind": "timer", "target": "dm:U1", "text": "stand up"}
        )
    )

    assert calls == [(runtime.user_client, "U1", "stand up")]


def test_dm_reminders_fall_back_to_the_bot(monkeypatch):
    from bot.features import scheduler
    from bot.runtime import runtime

    calls = []

    async def fake_post(client, channel, text, **kwargs):
        calls.append(client)
        return None if client is runtime.user_client else {"ts": "1"}

    monkeypatch.setattr(scheduler, "post", fake_post)
    bot = object()
    monkeypatch.setattr(runtime, "client", bot)
    monkeypatch.setattr(runtime, "user_client", object())

    asyncio.run(
        scheduler._deliver(
            {"id": 1, "kind": "timer", "target": "dm:U1", "text": "stand up"}
        )
    )

    assert calls == [runtime.user_client, bot]


def test_bot_config_accepts_welcome_and_watcher_keys():
    async def run():
        db = MagicMock()
        box = Toolbox(MagicMock())
        ctx = _ctx(db=db)

        out = await box.execute(
            "bot_config",
            {"action": "set", "key": "welcome_dm_enabled", "value": "off"},
            ctx,
        )
        assert "updated" in out
        db.kv_set.assert_any_call("config:welcome_dm_enabled", "false")

        await box.execute(
            "bot_config",
            {"action": "set", "key": "watch_urls", "value": "https://example.com"},
            ctx,
        )
        db.kv_set.assert_any_call("config:watch_urls", "https://example.com")

        await box.execute(
            "bot_config",
            {"action": "set", "key": "usergroup_id", "value": "off"},
            ctx,
        )
        db.kv_set.assert_any_call("config:usergroup_id", "")

    asyncio.run(run())

"""Code-of-conduct commands and the mute/report plumbing."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from bot.db import Database
from bot.features import agent as agent_features
from bot.features import conduct, router
from bot.runtime import runtime


async def _ack() -> None:
    pass


def _settings(**overrides):
    base = {
        "owner_user_id": "U_OWNER",
        "moderator_user_ids": frozenset({"U_MOD"}),
        "ai_api_key": "test-key",
        "ai_enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _command(user="U1", channel="C1", text=""):
    return {"user_id": user, "channel_id": channel, "text": text}


def _setup(tmp_path, monkeypatch, settings=None):
    db = Database(tmp_path / "conduct.db")
    db.init()
    monkeypatch.setattr(runtime, "db", db)
    monkeypatch.setattr(runtime, "settings", settings or _settings())
    client = AsyncMock()
    client.chat_postMessage.return_value.data = {"ts": "1"}
    return db, client


def test_mute_and_unmute_roundtrip(tmp_path, monkeypatch):
    db, client = _setup(tmp_path, monkeypatch)

    asyncio.run(conduct.cmd_mute(_ack, _command(), client))
    assert db.is_opted_out("U1")
    assert "leave you alone" in client.chat_postEphemeral.await_args.kwargs["text"]

    asyncio.run(conduct.cmd_unmute(_ack, _command(), client))
    assert not db.is_opted_out("U1")
    assert "back" in client.chat_postEphemeral.await_args.kwargs["text"]


def test_conduct_commands_are_wired_into_the_router():
    assert router.resolve("mute")[0] is conduct.cmd_mute
    assert router.resolve("unmute")[0] is conduct.cmd_unmute
    assert router.resolve("report a problem")[0] is conduct.cmd_report
    assert router.normalize_message_command("nugget report a problem") == "report a problem"
    assert router.normalize_message_command("nugget leave me alone") is None


def test_unmute_without_a_mute_says_so(tmp_path, monkeypatch):
    _db, client = _setup(tmp_path, monkeypatch)
    asyncio.run(conduct.cmd_unmute(_ack, _command(), client))
    assert "weren't muted" in client.chat_postEphemeral.await_args.kwargs["text"]


def test_standing_silence_sets_a_durable_optout(tmp_path, monkeypatch):
    _db, client = _setup(tmp_path, monkeypatch)
    asyncio.run(
        agent_features._note_silence(
            user_id="U1",
            text="please leave me alone",
            is_dm=True,
            channel="D1",
            thread_ts=None,
            client=client,
        )
    )
    assert runtime.db.is_opted_out("U1")
    assert "leave you alone" in client.chat_postMessage.await_args.kwargs["text"]


def test_one_off_silence_does_not_opt_out(tmp_path, monkeypatch):
    _db, client = _setup(tmp_path, monkeypatch)
    asyncio.run(
        agent_features._note_silence(
            user_id="U1",
            text="shut up lol",
            is_dm=True,
            channel="D1",
            thread_ts=None,
            client=client,
        )
    )
    assert not runtime.db.is_opted_out("U1")


def test_opted_out_user_gets_no_channel_reply(tmp_path, monkeypatch):
    db, client = _setup(tmp_path, monkeypatch)
    db.opt_out("U1")
    runtime.agent = MagicMock()
    runtime.agent.reply = AsyncMock(return_value="hi")
    monkeypatch.setattr(runtime, "agent", runtime.agent)

    posted = asyncio.run(
        agent_features._handle_channel(
            {"user": "U1", "channel": "C1", "text": "hey nugget"},
            client,
            text="hey nugget",
        )
    )

    assert posted is False
    runtime.agent.reply.assert_not_awaited()


def test_come_back_clears_the_optout(tmp_path, monkeypatch):
    db, client = _setup(tmp_path, monkeypatch)
    db.opt_out("U1")

    handled = asyncio.run(
        agent_features._respect_optout(
            {"user": "U1", "channel": "D1", "text": "ok come back"},
            client,
        )
    )

    assert handled is True
    assert not db.is_opted_out("U1")
    assert "back" in client.chat_postMessage.await_args.kwargs["text"]


def test_other_opted_out_messages_are_dropped(tmp_path, monkeypatch):
    db, client = _setup(tmp_path, monkeypatch)
    db.opt_out("U1")

    handled = asyncio.run(
        agent_features._respect_optout(
            {"user": "U1", "channel": "C1", "text": "what's the weather?"},
            client,
        )
    )

    assert handled is True
    assert db.is_opted_out("U1")
    client.chat_postMessage.assert_not_awaited()


def test_report_relays_to_owner_and_moderators(tmp_path, monkeypatch):
    _db, client = _setup(tmp_path, monkeypatch)

    asyncio.run(
        conduct.cmd_report(
            _ack,
            _command(text="someone is harassing me in a thread"),
            client,
        )
    )

    targets = [
        call.kwargs["channel"] for call in client.chat_postMessage.await_args_list
    ]
    assert targets == ["U_OWNER", "U_MOD"]
    relayed = client.chat_postMessage.await_args_list[0].kwargs["text"]
    assert "conduct report" in relayed
    assert "someone is harassing me" in relayed
    assert "<@U1>" in relayed
    # the reporter gets a private confirmation, never a public post
    assert client.chat_postEphemeral.await_count == 1
    confirmation = client.chat_postEphemeral.await_args.kwargs["text"]
    assert "conduct@hackclub.com" in confirmation
    assert "Shroud" in confirmation
    assert "eps@hackclub.com" in confirmation


def test_report_does_not_ping_the_whole_mod_channel(tmp_path, monkeypatch):
    _db, client = _setup(tmp_path, monkeypatch)
    asyncio.run(
        conduct.cmd_report(_ack, _command(text="<!channel> look at this"), client)
    )
    relayed = client.chat_postMessage.await_args_list[0].kwargs["text"]
    assert "<!channel>" not in relayed
    assert "@channel" in relayed


def test_report_with_no_human_recipient_uses_direct_routes(tmp_path, monkeypatch):
    _db, client = _setup(
        tmp_path,
        monkeypatch,
        settings=_settings(owner_user_id="", moderator_user_ids=frozenset()),
    )
    asyncio.run(conduct.cmd_report(_ack, _command(text="help"), client))
    assert client.chat_postMessage.await_count == 0
    text = client.chat_postEphemeral.await_args.kwargs["text"]
    assert "conduct@hackclub.com" in text


def test_report_is_rate_limited(tmp_path, monkeypatch):
    _db, client = _setup(tmp_path, monkeypatch)
    asyncio.run(conduct.cmd_report(_ack, _command(text="first"), client))
    asyncio.run(conduct.cmd_report(_ack, _command(text="second"), client))
    assert client.chat_postMessage.await_count == 2  # one relay, not two
    assert "one report at a time" in client.chat_postEphemeral.await_args.kwargs["text"]


def test_imagine_refuses_an_unsafe_prompt(tmp_path, monkeypatch):
    from bot.features import ai_commands

    db, client = _setup(tmp_path, monkeypatch)
    gate = MagicMock()
    gate.review_public = AsyncMock(return_value=False)
    monkeypatch.setattr(runtime, "gate", gate, raising=False)
    monkeypatch.setattr(runtime, "budget", None, raising=False)

    generated = AsyncMock()
    monkeypatch.setattr(ai_commands, "_generate_image", generated)
    monkeypatch.setattr(
        ai_commands.image_access,
        "can_generate",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        ai_commands.image_settings,
        "imagine_config",
        lambda *_args, **_kwargs: SimpleNamespace(model="test-image"),
    )

    asyncio.run(
        ai_commands.cmd_imagine(
            _ack,
            {"user_id": "U1", "channel_id": "C1", "text": "something awful"},
            client,
        )
    )

    generated.assert_not_awaited()
    text = client.chat_postEphemeral.await_args.kwargs["text"]
    assert "can't make that one" in text
    assert db is not None


def test_imagine_allows_a_safe_prompt(tmp_path, monkeypatch):
    from bot.features import ai_commands

    _db, client = _setup(tmp_path, monkeypatch)
    gate = MagicMock()
    gate.review_public = AsyncMock(return_value=True)
    monkeypatch.setattr(runtime, "gate", gate, raising=False)
    monkeypatch.setattr(runtime, "budget", None, raising=False)

    generated = AsyncMock(return_value=(b"png", "image/png"))
    monkeypatch.setattr(ai_commands, "_generate_image", generated)
    monkeypatch.setattr(
        ai_commands.image_access,
        "can_generate",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        ai_commands.image_settings,
        "imagine_config",
        lambda *_args, **_kwargs: SimpleNamespace(model="test-image", cost_usd=0.0),
    )

    asyncio.run(
        ai_commands.cmd_imagine(
            _ack,
            {"user_id": "U1", "channel_id": "C1", "text": "a tiny astronaut"},
            client,
        )
    )

    generated.assert_awaited_once()
    gate.review_public.assert_awaited_once()

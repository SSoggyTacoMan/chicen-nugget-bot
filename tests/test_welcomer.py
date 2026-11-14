from types import SimpleNamespace
from unittest.mock import MagicMock

from bot.features.welcomer import join_text, leave_text, render


def test_watched_channels_are_explicit_or_wildcard():
    from bot.features import welcomer

    assert welcomer._watches("C123456789", ("C123456789", "C234567890"))
    assert welcomer._watches("C234567890", ("C123456789", "C234567890"))
    assert not welcomer._watches("C999999999", ("C123456789", "C234567890"))
    assert welcomer._watches("CANYCHANNEL", ("*",))


def test_welcome_config_prefers_db_over_env(monkeypatch):
    from bot.features import welcomer

    db = MagicMock()
    db.kv_get.side_effect = lambda key: {
        "config:welcome_message": "db welcome",
        "config:welcome_dm": "db dm",
        "config:usergroup_id": "S123",
        "config:watched_channel_ids": "C1,C2",
    }.get(key)
    settings = SimpleNamespace(
        welcome_public=True,
        welcome_message="env welcome",
        welcome_dm="env dm",
        welcome_dm_once=True,
        welcome_thread_reply=False,
        welcome_thread_text="hey {user} :wave:",
        welcome_ping="",
        welcome_ping_auto_add=False,
        welcome_ping_button=True,
        watched_channel_ids=("C9",),
        usergroup_id=None,
        usergroup_prompt=True,
        usergroup_prompt_text="join?",
        usergroup_prompt_once=True,
    )
    fake_runtime = SimpleNamespace(db=db, settings=settings)
    monkeypatch.setattr(welcomer, "runtime", fake_runtime)

    cfg = welcomer.welcome_config()
    assert cfg.message == "db welcome"
    assert cfg.dm == "db dm"
    assert cfg.usergroup_id == "S123"
    assert cfg.watched == ("C1", "C2")
    assert welcomer.message_for("C1", cfg) == "db welcome"


def test_join_text():
    assert "in the ping group" in join_text("added")
    assert "already" in join_text("already")
    assert "couldn't add" in join_text("failed")


def test_leave_text():
    assert "removed" in leave_text("removed")
    assert "weren't" in leave_text("not_member")
    assert "couldn't remove" in leave_text("failed")


def test_render_placeholders():
    assert render("hi {user} in {channel}", user="<@U1>", channel="<#C1>") == (
        "hi <@U1> in <#C1>"
    )


def _cfg(**overrides):
    from bot.features.welcomer import WelcomeConfig

    values = dict(
        public=True,
        message="welcome {user} to {channel}",
        dm="hey {user}",
        dm_enabled=True,
        dm_once=True,
        thread_reply=False,
        thread_text="hey {user} :wave:",
        ping="",
        auto_add=False,
        ping_button=True,
        watched=("C1",),
        usergroup_id="S123",
        prompt=True,
        prompt_text="join the ping group?",
        prompt_once=True,
    )
    values.update(overrides)
    return WelcomeConfig(**values)


def test_welcome_auto_adds_and_dms_leave_button(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from bot.features import welcomer
    from bot.features.welcomer import LEAVE_ACTION

    db = MagicMock()
    db.kv_get.return_value = None
    db.was_welcomed.return_value = False
    db.member.return_value = {"joins": 1}
    client = AsyncMock()
    runtime = SimpleNamespace(
        db=db,
        settings=SimpleNamespace(bot_name="nugget", access_enabled=False),
        user_client=object(),
        bot_user_id="UBOT",
    )
    cfg = _cfg(auto_add=True, thread_reply=True)
    post = AsyncMock(side_effect=[{"ts": "123.45"}, {"ok": True}, {"ok": True}])

    monkeypatch.setattr(welcomer, "runtime", runtime)
    monkeypatch.setattr(welcomer, "welcome_config", lambda: cfg)
    monkeypatch.setattr(welcomer, "user_info", AsyncMock(return_value={"profile": {"display_name": "alice"}}))
    monkeypatch.setattr(welcomer, "ensure_usergroup_member", AsyncMock(return_value="added"))
    monkeypatch.setattr(welcomer, "ensure_usergroup_not_member", AsyncMock(return_value="removed"))
    monkeypatch.setattr(welcomer, "post", post)
    monkeypatch.setattr(welcomer, "_log", AsyncMock())

    asyncio.run(
        welcomer.on_member_joined(
            {"channel": "C1", "user": "U1", "user_type": "member"}, client
        )
    )

    welcomer.ensure_usergroup_member.assert_awaited_once_with(
        runtime.user_client, "S123", "U1"
    )
    # main welcome + thread reply + DM
    assert post.await_count == 3
    assert post.await_args_list[1].kwargs["thread_ts"] == "123.45"
    dm_call = post.await_args_list[2]
    assert dm_call.args[1] == "U1"
    assert any(
        element.get("action_id") == LEAVE_ACTION
        for block in dm_call.kwargs["blocks"]
        for element in block.get("elements") or []
    )


def test_welcome_without_auto_add_offers_join(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from bot.features import welcomer
    from bot.features.welcomer import JOIN_ACTION

    db = MagicMock()
    db.kv_get.return_value = None
    db.member.return_value = {"joins": 1}
    client = AsyncMock()
    runtime = SimpleNamespace(
        db=db,
        settings=SimpleNamespace(bot_name="nugget", access_enabled=False),
        user_client=object(),
        bot_user_id="UBOT",
    )
    cfg = _cfg(auto_add=False)
    post = AsyncMock(side_effect=[{"ts": "123.45"}, {"ok": True}])

    monkeypatch.setattr(welcomer, "runtime", runtime)
    monkeypatch.setattr(welcomer, "welcome_config", lambda: cfg)
    monkeypatch.setattr(welcomer, "user_info", AsyncMock(return_value={"profile": {"display_name": "alice"}}))
    monkeypatch.setattr(welcomer, "usergroup_members", AsyncMock(return_value={"U2"}))
    monkeypatch.setattr(welcomer, "post", post)
    monkeypatch.setattr(welcomer, "_log", AsyncMock())

    asyncio.run(
        welcomer.on_member_joined(
            {"channel": "C1", "user": "U1", "user_type": "member"}, client
        )
    )

    dm_call = post.await_args_list[1]
    assert any(
        element.get("action_id") == JOIN_ACTION
        for block in dm_call.kwargs["blocks"]
        for element in block.get("elements") or []
    )

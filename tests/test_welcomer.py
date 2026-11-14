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
        welcome_ping="",
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

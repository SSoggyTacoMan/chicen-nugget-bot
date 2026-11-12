from types import SimpleNamespace
from unittest.mock import MagicMock

from bot.features.welcomer import join_text, leave_text, render


def test_runtime_watched_channel_override(monkeypatch):
    from bot.features import welcomer

    db = MagicMock()
    db.kv_get.return_value = "C123456789,C234567890"
    fake_runtime = SimpleNamespace(db=db, settings=MagicMock())
    monkeypatch.setattr(welcomer, "runtime", fake_runtime)

    assert welcomer._watches("C123456789")
    assert welcomer._watches("C234567890")
    assert not welcomer._watches("C999999999")

    db.kv_get.return_value = "*"
    assert welcomer._watches("CANYCHANNEL")

    db.kv_get.return_value = None
    fake_runtime.settings.watches.return_value = True
    assert welcomer._watches("C999999999")
    fake_runtime.settings.watches.assert_called_once_with("C999999999")


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

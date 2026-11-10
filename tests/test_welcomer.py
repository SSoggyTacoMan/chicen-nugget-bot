from bot.features.welcomer import join_text, leave_text, render


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

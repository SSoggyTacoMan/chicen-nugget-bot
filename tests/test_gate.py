from bot.ai.gate import decision


def test_yes():
    assert decision("YES")
    assert decision("yes")
    assert decision("Yes.")
    assert decision(" yep ")
    assert decision("YES, because they asked a question")


def test_no():
    assert not decision("NO")
    assert not decision("no")
    assert not decision("No.")
    assert not decision("nope")
    assert not decision("NO, that's chatter")


def test_garbage_fails_closed():
    assert not decision("")
    assert not decision(None)
    assert not decision("maybe?")
    assert not decision("I cannot decide")

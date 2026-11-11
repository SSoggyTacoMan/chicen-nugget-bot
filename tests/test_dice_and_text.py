import random

from bot.utils.dice import roll
from bot.utils.text import chunk_text, format_mrkdwn


def test_dice_basic():
    result = roll("2d6+3", random.Random(1))
    assert result is not None
    assert len(result.rolls) == 2
    assert all(1 <= value <= 6 for value in result.rolls)
    assert result.total == sum(result.rolls) + 3


def test_dice_negative_modifier():
    result = roll("4d8-1", random.Random(1))
    assert result is not None
    assert result.modifier == -1
    assert result.total == sum(result.rolls) - 1


def test_coin_is_d2():
    result = roll("coin", random.Random(1))
    assert result is not None
    assert result.sides == 2


def test_invalid_dice():
    assert roll("hello") is None
    assert roll("0d6") is None
    assert roll("d1") is None
    assert roll("100d6") is None


def test_chunk_short():
    assert chunk_text("hello") == ["hello"]


def test_chunk_long():
    text = "word " * 2000
    chunks = chunk_text(text, limit=100)
    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert " ".join(chunks).split() == text.split()


def test_format_mrkdwn():
    sample = (
        "## Title\n"
        "Here is **bold**, ***bold italic***, and ~~strikethrough~~.\n"
        "Check [our docs](https://example.com/docs) — now!\n"
        "Inline `**keep literal**` and:\n"
        "```python\ndef test(**kwargs):\n    # **keep comment**\n    return 1\n```\n"
    )
    res = format_mrkdwn(sample)
    assert "*Title*" in res
    assert "##" not in res
    assert "*bold*" in res
    assert "**bold**" not in res
    assert "*_bold italic_*" in res
    assert "~strikethrough~" in res
    assert "~~" not in res
    assert "<https://example.com/docs|our docs>" in res
    assert "[our docs]" not in res
    assert " - now!" in res
    assert "—" not in res
    assert "`**keep literal**`" in res
    assert "def test(**kwargs):" in res
    assert "# **keep comment**" in res

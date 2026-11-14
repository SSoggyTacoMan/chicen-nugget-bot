import random

from bot.utils.dice import roll
from bot.utils.text import (
    asks_for_silence,
    chat_chunks,
    chunk_text,
    extract_id,
    format_mrkdwn,
    has_owner_signature,
    incoming_text,
    is_ignored_message,
)


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
    assert "```python" not in res
    assert "```\ndef test(**kwargs):" in res


def test_format_mrkdwn_closes_dangling_fences():
    truncated = "```toml\n[package]\nname = \"spotifast\""
    res = format_mrkdwn(truncated)
    assert "```toml" not in res
    assert res.startswith("```\n")
    assert res.endswith("\n```")

    dangling_inline = "use `reqwest"
    assert format_mrkdwn(dangling_inline) == "use `reqwest`"


def test_extract_id():
    assert extract_id("<@U0OWNER01|owner>", "@") == "U0OWNER01"
    assert extract_id("<@U0OWNER01>", "@") == "U0OWNER01"
    assert extract_id("@U0OWNER01", "@") == "U0OWNER01"
    assert extract_id("U0OWNER01", "@") == "U0OWNER01"
    assert extract_id("<@U0OWNER01|owner>", "U") == "U0OWNER01"
    assert extract_id("W0123456789", "@") == "W0123456789"
    assert extract_id("<#C0C83BAUZCK|general>", "#") == "C0C83BAUZCK"
    assert extract_id("C0C83BAUZCK", "#") == "C0C83BAUZCK"
    assert extract_id("G0C83BAUZCK", "#") == "G0C83BAUZCK"
    assert extract_id("owner", "@") is None
    assert extract_id("general", "#") is None
    assert extract_id("#general", "#") is None
    assert extract_id("wendy", "@") is None
    assert extract_id("@wendy", "@") is None
    assert extract_id("ubuntu", "@") is None
    assert extract_id("charles", "#") is None


def test_owner_signature_detection():
    assert has_owner_signature("test test\n\n_Sent by <@U0BOT0001>_")
    assert has_owner_signature("Sent by <@U0BOI0001>")
    assert not has_owner_signature("regular message")
    assert not has_owner_signature("")


def test_incoming_text_keeps_mentions_readable():
    raw = "i have <@U0BOI0001> and <@U0BOT0001>, cc <@U0ALICE>"
    cleaned = incoming_text(
        raw, bot_user_id="U0BOT0001", assistant_user_id="U0BOI0001"
    )
    assert cleaned == "i have @boi and @nugget, cc <@U0ALICE>"
    assert "cut off" not in cleaned


def test_asks_for_silence():
    assert asks_for_silence("pls dont respond to this chicken")
    assert asks_for_silence("please don't reply")
    assert asks_for_silence("don't answer this")
    assert asks_for_silence("shut up")
    assert asks_for_silence("can you not")
    assert asks_for_silence("chicen nobody asked u shi")
    assert asks_for_silence("stop replying")
    assert asks_for_silence("why are u responding")
    assert not asks_for_silence("i dont respond to spam")
    assert not asks_for_silence("the api is down")


def test_is_ignored_message():
    assert is_ignored_message("# nugget stay out of this")
    assert is_ignored_message("  # manual opt out")
    assert not is_ignored_message("what does # mean in markdown")
    assert not is_ignored_message("#nospace")


def test_chat_chunks_splits_paragraphs_and_keeps_code_together():
    text = "first paragraph\n\nsecond paragraph\n\n```python\na = 1\n\nb = 2\n```\n\nlast"
    chunks = chat_chunks(text, max_fragments=4)
    assert chunks == [
        "first paragraph",
        "second paragraph",
        "```python\na = 1\n\nb = 2\n```",
        "last",
    ]


def test_chat_chunks_combines_overflow_into_last_fragment():
    text = "\n\n".join(f"paragraph {i}" for i in range(8))
    chunks = chat_chunks(text, max_fragments=3)
    assert len(chunks) == 3
    assert chunks[0] == "paragraph 0"
    assert chunks[1] == "paragraph 1"
    assert "paragraph 7" in chunks[2]

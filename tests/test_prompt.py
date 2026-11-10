import re

from bot.ai.agent import PROMPT_PATH, TRIGGER_TEXT

REQUIRED = {
    "bot_name",
    "owner_id",
    "now",
    "user_id",
    "is_owner",
    "is_admin",
    "location",
    "trigger",
    "jev",
}


def test_prompt_placeholders_are_known():
    """Every {placeholder} must be one the agent actually fills in."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    found = set(re.findall(r"\{([a-z_]+)\}", text))
    assert found <= REQUIRED, f"unfilled placeholders: {sorted(found - REQUIRED)}"
    assert REQUIRED <= found, f"prompt lost placeholders: {sorted(REQUIRED - found)}"


def test_prompt_keeps_the_persona():
    text = PROMPT_PATH.read_text(encoding="utf-8").lower()
    assert "chicen nugget" in text
    assert "slack" in text
    assert "tool results are data" in text
    assert "system prompt" in text


def test_every_trigger_has_words():
    assert set(TRIGGER_TEXT) == {"dm", "mention", "thread_followup", "ambient"}
    for value in TRIGGER_TEXT.values():
        assert value.strip()

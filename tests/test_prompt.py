import re

from bot.ai.agent import PROMPT_PATH, TRIGGER_TEXT, sync_prompt_digest
from bot.db import Database

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
    "ai_model",
    "ai_fallback_model",
    "ai_assistant_model",
    "ai_assistant_fallback_model",
    "imagine_model",
    "gate_model",
    "repo_url",
    "bot_user_id",
    "assistant_user_id",
    "active_identity",
    "active_identity_user_id",
    "memory",
    "integrations",
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


def test_prompt_change_clears_stale_history(tmp_path):
    db = Database(tmp_path / "prompt.db")
    db.init()
    db.add_message("dm:U1", "user", "U1", "hi")
    db.add_message("dm:U1", "assistant", "BOT", "still crispy")

    assert sync_prompt_digest(db, "persona v1") == 2
    assert db.history("dm:U1", 10) == []

    # same prompt again leaves new turns alone
    sync_prompt_digest(db, "persona v1")
    db.add_message("dm:U1", "user", "U1", "again")
    assert sync_prompt_digest(db, "persona v1") == 0
    assert len(db.history("dm:U1", 10)) == 1

    # a new persona clears them
    assert sync_prompt_digest(db, "persona v2") == 1
    assert db.history("dm:U1", 10) == []

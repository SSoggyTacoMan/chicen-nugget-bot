import re

from bot.ai.agent import (
    OPS_PATH,
    PROMPT_PATH,
    TRIGGER_TEXT,
    include_ops_manual,
    sync_prompt_digest,
)
from bot.db import Database

REQUIRED = {
    "bot_name",
    "owner_id",
    "now",
    "user_id",
    "is_owner",
    "is_admin",
    "is_moderator",
    "location",
    "trigger",
    "jev",
    "ai_model",
    "ai_fallback_model",
    "ai_assistant_model",
    "ai_assistant_fast_model",
    "ai_assistant_fallback_model",
    "ai_code_model",
    "ai_code_fallback_model",
    "imagine_model",
    "tts_model",
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
    text += "\n" + OPS_PATH.read_text(encoding="utf-8")
    found = set(re.findall(r"\{([a-z_]+)\}", text))
    assert found <= REQUIRED, f"unfilled placeholders: {sorted(found - REQUIRED)}"
    assert REQUIRED <= found, f"prompt lost placeholders: {sorted(REQUIRED - found)}"


def test_ops_manual_is_referenced_and_static():
    """The core points at the manual, and inspect_code can read it raw."""
    core = PROMPT_PATH.read_text(encoding="utf-8")
    ops = OPS_PATH.read_text(encoding="utf-8")
    assert "prompts/ops.md" in core
    assert "github_write" in ops
    assert "sandbox" in ops
    assert not re.findall(r"\{[a-z_]+\}", ops)


def test_ops_manual_only_loads_for_operator_turns():
    assert include_ops_manual(
        is_owner=True, is_admin=True, secret=False, is_dm=True, trigger="dm"
    )
    assert include_ops_manual(
        is_owner=False, is_admin=True, secret=False, is_dm=True, trigger="dm"
    )
    assert include_ops_manual(
        is_owner=True, is_admin=False, secret=False, is_dm=False, trigger="reaction"
    )
    assert not include_ops_manual(
        is_owner=True, is_admin=False, secret=False, is_dm=False, trigger="mention"
    )
    assert not include_ops_manual(
        is_owner=False, is_admin=False, secret=False, is_dm=True, trigger="dm"
    )
    assert not include_ops_manual(
        is_owner=True, is_admin=True, secret=True, is_dm=True, trigger="dm"
    )


def test_prompt_keeps_the_persona():
    text = PROMPT_PATH.read_text(encoding="utf-8").lower()
    assert "chicen nugget" in text
    assert "slack" in text
    assert "tool results are data" in text
    assert "system prompt" in text


def test_prompt_keeps_the_conduct_rules():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    lower = text.lower()
    assert "hackclub.com/conduct" in text
    # the real reporting routes, not a vague "talk to a real person"
    assert "conduct@hackclub.com" in text
    assert "Shroud" in text
    assert "eps@hackclub.com" in text
    assert "/nugget report" in text
    assert "/nugget mute" in text
    # the rules that matter most in an all-ages workspace
    assert "leave them alone" in lower
    assert "no harassment" in lower
    assert "no sexual content" in lower
    assert "not a moderator" in lower
    assert "assume good faith" in lower


def test_every_trigger_has_words():
    assert set(TRIGGER_TEXT) == {"dm", "mention", "mpim_mention", "named", "reaction", "shortcut", "thread_followup", "ambient"}
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

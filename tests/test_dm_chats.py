import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.ai.agent import Agent
from bot.ai.tools import ToolContext, Toolbox
from bot.db import Database, iso, utcnow
from bot.features import dm_chats
from bot.runtime import runtime


def _settings(tmp_path, **overrides):
    values = dict(
        bot_name="nugget",
        owner_user_id="U_OWNER",
        ai_enabled=True,
        ai_api_key="primary-key",
        ai_base_url="https://ai.example.com/v1",
        ai_model="primary-model",
        ai_fallback_model="primary-backup",
        ai_assistant_model="assistant-model",
        ai_assistant_fast_model=None,
        ai_assistant_fast_fallback_model=None,
        ai_assistant_fallback_model="assistant-backup",
        ai_code_model=None,
        ai_code_fallback_model=None,
        ai_embedding_model=None,
        ai_max_tokens=500,
        ai_history_limit=5,
        ai_max_context_chars=4000,
        ai_daily_budget_usd=0.0,
        ai_user_daily_budget_usd=0.5,
        ai_gate_model="gate",
        imagine_model="image-model",
        imagine_fallback_model=None,
        ai_image_cost_usd=0.04,
        tts_model="tts-model",
        tts_voice="Aiden",
        tts_language="English",
        tts_style=None,
        tts_max_chars=500,
        tts_cost_per_1k_chars=0.02,
        transcribe_model="audio-model",
        transcribe_fallback_model="audio-backup",
        ai_router_base_url="https://router.example/v1",
        ai_router_api_key="fallback-key",
        ai_router_models=("kgw/kilo-auto/free",),
        data_dir=tmp_path,
        repo_url="https://github.com/repo",
        slack_bot_token="xoxb-test",
        ai_allow_admins=False,
        admin_private_access=False,
        dm_chats=True,
        dm_thread_minutes=5,
        dm_thread_classifier=False,
        dm_chat_max=50,
        secret_chats=True,
        secret_chat_ttl_minutes=120,
        secret_chat_max_per_user=3,
        secret_chat_tools="web",
        secret_chat_discreet=True,
        secret_preview_text="🔒 secret chat reply",
        is_admin=lambda user_id: user_id == "U_OWNER",
        is_moderator=lambda user_id: False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "dm_chats.db"))
    database.init()
    yield database
    database.close()


@pytest.fixture
def env(db, tmp_path, monkeypatch):
    old = (runtime.db, runtime.settings, runtime.gate)
    runtime.db = db
    runtime.settings = _settings(tmp_path)
    runtime.gate = None
    dm_chats._secret.clear()
    dm_chats._last_message.clear()
    dm_chats._nudged.clear()
    yield db
    runtime.db, runtime.settings, runtime.gate = old
    dm_chats._secret.clear()
    dm_chats._last_message.clear()
    dm_chats._nudged.clear()


def _event(text, ts, thread_ts=None, user="U_OWNER", channel="D1"):
    event = {"channel": channel, "user": user, "text": text, "ts": ts}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


def _route(event):
    return asyncio.run(dm_chats.route_dm(event))


def test_split_new_chat_prefix():
    assert dm_chats.split_new_chat("new chat about rust") == (True, "about rust")
    assert dm_chats.split_new_chat("start a new thread: hello") == (True, "hello")
    assert dm_chats.split_new_chat("hello there") == (False, "hello there")


def test_first_dm_starts_a_thread(env):
    route = _route(_event("hello", "1.0"))
    assert route.conversation == "dm:U_OWNER:1.0"
    assert route.thread_ts == "1.0"
    assert route.created is True
    assert route.secret is None
    assert route.title == "hello"
    assert env.dm_chat("U_OWNER", "1.0") is not None


def test_continuation_cue_stays_in_the_same_thread(env):
    first = _route(_event("hello", "1.0"))
    dm_chats._last_message.clear()
    second = _route(_event("and what about lifetimes", "2.0"))
    assert second.conversation == first.conversation
    assert second.thread_ts == "1.0"
    assert second.redirected is True


def test_unrelated_message_starts_a_new_thread(env):
    _route(_event("hello", "1.0"))
    dm_chats._last_message.clear()
    second = _route(
        _event("i think rust ownership is interesting because it prevents data races", "2.0")
    )
    assert second.created is True
    assert second.conversation == "dm:U_OWNER:2.0"


def test_explicit_new_chat_wins(env):
    _route(_event("hello", "1.0"))
    dm_chats._last_message.clear()
    second = _route(_event("new chat rust borrow checker stuff", "2.0"))
    assert second.created is True
    assert second.conversation == "dm:U_OWNER:2.0"
    assert second.text == "rust borrow checker stuff"
    assert second.title == "rust borrow checker stuff"


def test_thread_reply_routes_to_its_chat(env):
    first = _route(_event("hello", "1.0"))
    second = _route(_event("more detail", "1.2", thread_ts="1.0"))
    assert second.conversation == first.conversation
    assert second.thread_ts == "1.0"
    assert second.redirected is False


def test_continuation_window_expires(env):
    _route(_event("hello", "1.0"))
    env._write(
        "UPDATE dm_chats SET last_used_at = ? WHERE user_id = ? AND thread_ts = ?",
        (iso(utcnow() - timedelta(minutes=30)), "U_OWNER", "1.0"),
    )
    dm_chats._last_message.clear()
    second = _route(_event("and what about lifetimes", "2.0"))
    assert second.created is True
    assert second.conversation == "dm:U_OWNER:2.0"


def test_secret_thread_never_writes_content(env):
    env.mark_secret_thread(
        "U_OWNER", "9.0", expires_at=iso(utcnow() + timedelta(minutes=10))
    )
    env.set_active_dm_chat("U_OWNER", "9.0")
    route = _route(_event("tell me a secret", "9.1", thread_ts="9.0"))
    assert route.secret is not None
    route.secret.add("user", "U_OWNER", "tell me a secret")
    route.secret.add("assistant", None, "the secret reply")

    assert env.history("dm:U_OWNER:9.0", 10) == []
    assert env.search_messages("secret reply") == []
    assert route.secret.history(10)[-1]["text"] == "the secret reply"


def test_secret_marker_stays_until_ended(env):
    env.mark_secret_thread(
        "U_OWNER", "8.0", expires_at=iso(utcnow() - timedelta(minutes=1))
    )
    # Expiry only clears the in-memory turns. The marker stays so a late reply
    # can never be saved by accident.
    assert env.is_secret_thread("U_OWNER", "8.0") is True
    env.unmark_secret_thread("U_OWNER", "8.0")
    assert env.is_secret_thread("U_OWNER", "8.0") is False


def test_secret_stays_secret_when_dm_chats_disabled(env):
    runtime.settings.dm_chats = False
    env.mark_secret_thread("U_OWNER", "5.0")
    env.set_active_dm_chat("U_OWNER", "5.0")
    route = _route(_event("still private", "5.1", thread_ts="5.0"))
    assert route is not None
    assert route.secret is not None
    assert route.conversation == "dm:U_OWNER:5.0"
    # A main-DM message while secret mode is active also stays secret.
    dm_chats._last_message.clear()
    main = _route(_event("normal message", "6.0"))
    assert main is not None and main.secret is not None
    # Once the secret chat is ended, normal DMs use the legacy timeline.
    env.unmark_secret_thread("U_OWNER", "5.0")
    env.set_active_dm_chat("U_OWNER", None)
    dm_chats._last_message.clear()
    assert _route(_event("normal message", "7.0")) is None


def test_secret_store_ttl_prune(env):
    chat = dm_chats.secret_chat("U_OWNER", "7.0", create=True)
    assert chat is not None
    chat.last_used = 0.0
    dm_chats.prune_secret(now=10_000_000.0)
    assert dm_chats.secret_chat("U_OWNER", "7.0") is None


def test_secret_tools_are_read_only(db):
    toolbox = Toolbox(SimpleNamespace(secret_chat_tools="web"), db)
    names = {
        item["function"]["name"] for item in toolbox.definitions_for(ephemeral=True)
    }
    assert names
    assert "remember" not in names
    assert "post_message" not in names
    assert "send_dm" not in names
    assert "search_web" in names

    ctx = ToolContext(
        db=db,
        settings=SimpleNamespace(secret_chat_tools="web"),
        client=MagicMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_OWNER",
        channel_id="D1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
        ephemeral=True,
    )
    result = asyncio.run(toolbox.execute("remember", {"text": "x"}, ctx))
    assert "secret chat" in result

    none_toolbox = Toolbox(SimpleNamespace(secret_chat_tools="none"), db)
    assert none_toolbox.definitions_for(ephemeral=True) == []


def test_burn_only_deletes_bot_messages(monkeypatch):
    old_bot_id, old_bot_user = runtime.bot_id, runtime.bot_user_id
    runtime.bot_id = "B1"
    runtime.bot_user_id = "UBOT"
    client = MagicMock()
    client.conversations_replies = AsyncMock(
        return_value=SimpleNamespace(
            data={
                "messages": [
                    {"ts": "1.0", "bot_id": "B1"},
                    {"ts": "1.1", "user": "U_OWNER"},
                    {"ts": "1.2", "user": "UBOT"},
                ],
                "response_metadata": {},
            }
        )
    )
    client.chat_delete = AsyncMock()
    try:
        deleted = asyncio.run(dm_chats.burn_thread(client, "D1", "1.0"))
    finally:
        runtime.bot_id, runtime.bot_user_id = old_bot_id, old_bot_user
    assert deleted == 2
    deleted_ts = [call.kwargs["ts"] for call in client.chat_delete.await_args_list]
    assert deleted_ts == ["1.0", "1.2"]


def test_handle_dm_secret_reply_is_discreet(env, monkeypatch):
    from unittest.mock import patch

    from bot.features.agent import _handle_dm

    env.mark_secret_thread(
        "U_OWNER", "9.0", expires_at=iso(utcnow() + timedelta(minutes=10))
    )
    env.set_active_dm_chat("U_OWNER", "9.0")
    old_agent = runtime.agent
    runtime.agent = MagicMock()
    runtime.agent.reply = AsyncMock(return_value="the secret reply")
    client = AsyncMock()

    async def run():
        with patch("bot.features.agent.fetch_slack_images", AsyncMock(return_value=[])), \
             patch("bot.features.agent.transcribe_slack_audio", AsyncMock(return_value=[])), \
             patch("bot.features.agent.react", AsyncMock()), \
             patch("bot.features.agent.unreact", AsyncMock()), \
             patch(
                 "bot.features.agent.post",
                 AsyncMock(return_value={"ts": "9.2"}),
             ) as mock_post:
            event = _event("tell me something private", "9.1", thread_ts="9.0")
            await _handle_dm(event, client)
        call = runtime.agent.reply.call_args.kwargs
        assert call["secret"] is not None
        assert call["conversation"] == "dm:U_OWNER:9.0"
        posted = mock_post.call_args.kwargs
        assert posted["notification_text"] == "🔒 secret chat reply"
        assert posted["thread_ts"] == "9.0"
        assert env.history("dm:U_OWNER:9.0", 10) == []

    try:
        asyncio.run(run())
    finally:
        runtime.agent = old_agent


def test_agent_secret_turn_skips_sqlite(tmp_path):
    db = Database(str(tmp_path / "agent_secret.db"))
    db.init()
    settings = _settings(tmp_path)
    agent = Agent(
        settings,
        db,
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
    )
    agent._remember = MagicMock()

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "the secret reply"
    response.choices[0].message.tool_calls = []
    response.usage = None
    agent._chat = AsyncMock(return_value=(response, "primary"))

    chat = dm_chats.SecretChat("U_OWNER", "1.0")
    answer = asyncio.run(
        agent.reply(
            conversation="dm:U_OWNER:1.0",
            user_id="U_OWNER",
            text="tell me a secret",
            channel_id="D1",
            thread_ts="1.0",
            is_dm=True,
            trigger="dm",
            secret=chat,
        )
    )

    assert answer == "the secret reply"
    assert [turn["role"] for turn in chat.turns] == ["user", "assistant"]
    assert chat.turns[-1]["text"] == "the secret reply"
    assert db.history("dm:U_OWNER:1.0", 10) == []
    assert db.search_messages("secret") == []
    agent._remember.assert_not_called()

    call_args, kwargs = agent._chat.call_args
    assert kwargs["ephemeral"] is True
    assert any(
        "PRIVATE MODE" in str(message.get("content"))
        for message in call_args[0]
        if message.get("role") == "system"
    )
    db.close()

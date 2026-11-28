import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.ai.agent import Agent, QuotaExhausted
from bot.ai.budget import utc_day
from bot.ai.providers import FallbackProvider
from bot.db import Database

UPSTREAM_ERROR = (
    "Error code: 402 - {'error': {'message': 'Insufficient credits. "
    "Add more using https://openrouter.ai/settings/credits', "
    "'code': 402, 'metadata': {'limit_source': 'openrouter_credits'}}}"
)


def _settings(tmp_path):
    return SimpleNamespace(
        bot_name="nugget",
        owner_user_id="U_OWNER",
        ai_enabled=True,
        ai_api_key="primary-key",
        ai_base_url="https://ai.example.com/v1",
        ai_model="primary-model",
        ai_fallback_model="primary-backup",
        ai_assistant_model="assistant-model",
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
        ai_router_models=("fallback-model",),
        data_dir=tmp_path,
        repo_url="https://github.com/repo",
        is_admin=lambda user_id: user_id == "U_OWNER",
    )


def _response(text="fallback answer"):
    res = MagicMock()
    choice = MagicMock()
    choice.message.content = text
    choice.message.tool_calls = []
    res.choices = [choice]
    res.usage = None
    return res


def _agent(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init()
    settings = _settings(tmp_path)
    client = AsyncMock()
    agent = Agent(settings, db, client=client, user_client=None, bot_user_id="U_BOT")
    agent.llm = MagicMock()
    agent.llm.chat.completions.create = AsyncMock(side_effect=Exception(UPSTREAM_ERROR))
    fallback_client = MagicMock()
    fallback_client.chat.completions.create = AsyncMock(return_value=_response())
    provider = FallbackProvider(
        base_url="https://router.example/v1",
        api_key="fallback-key",
        models=("fallback-model",),
    )
    agent._fallback = lambda: (fallback_client, provider)
    return agent, db, fallback_client


def test_chat_switches_to_fallback_on_upstream_quota_error(tmp_path):
    agent, db, fallback_client = _agent(tmp_path)

    async def run():
        response, provider = await agent._chat(
            [{"role": "user", "content": "hello"}],
            tools=True,
            user_id="U_OWNER",
        )
        assert response.choices[0].message.content == "fallback answer"
        assert provider == "fallback"

    asyncio.run(run())

    _, kwargs = fallback_client.chat.completions.create.call_args
    assert kwargs["model"] == "fallback-model"
    assert "tools" not in kwargs
    # an upstream billing error is a short cooldown, not a day-long block
    assert db.kv_get(f"quota:hcai:{utc_day()}") is None
    assert db.kv_get("quota:hcai:cooldown")
    assert db.kv_get("quota:hcai:kind") == "upstream"
    agent.client.chat_postMessage.assert_awaited_once()


def test_chat_switches_to_fallback_on_daily_limit(tmp_path):
    agent, db, _ = _agent(tmp_path)
    agent.llm.chat.completions.create = AsyncMock(
        side_effect=Exception("429 Daily spending limit of $3 reached")
    )

    async def run():
        _, provider = await agent._chat(
            [{"role": "user", "content": "hello"}], tools=False
        )
        assert provider == "fallback"

    asyncio.run(run())
    assert db.kv_get(f"quota:hcai:{utc_day()}") == "empty"
    assert db.kv_get("quota:hcai:cooldown") is None


def test_upstream_cooldown_skips_primary_on_the_next_turn(tmp_path):
    agent, _, fallback_client = _agent(tmp_path)

    async def run():
        await agent._chat([{"role": "user", "content": "one"}], tools=False)
        await agent._chat([{"role": "user", "content": "two"}], tools=False)

    asyncio.run(run())
    agent.llm.chat.completions.create.assert_awaited_once()
    assert fallback_client.chat.completions.create.await_count == 2


def test_chat_raises_quota_error_when_fallback_also_fails(tmp_path):
    agent, _, fallback_client = _agent(tmp_path)
    fallback_client.chat.completions.create = AsyncMock(
        side_effect=Exception("fallback model unavailable")
    )

    with pytest.raises(QuotaExhausted) as exc:
        asyncio.run(
            agent._chat(
                [{"role": "user", "content": "hello"}],
                tools=False,
            )
        )
    assert "fallback model unavailable" in str(exc.value)
    assert exc.value.kind == "upstream"


def test_quota_error_without_fallback_asks_to_configure_one(tmp_path):
    agent, _, _ = _agent(tmp_path)
    agent._fallback = lambda: None

    with pytest.raises(QuotaExhausted) as exc:
        asyncio.run(
            agent._chat([{"role": "user", "content": "hello"}], tools=False)
        )
    assert exc.value.fallback is None
    message = agent._friendly_error(exc.value)
    assert "add a fallback endpoint" in message


def test_non_quota_primary_error_does_not_use_fallback(tmp_path):
    agent, _, fallback_client = _agent(tmp_path)
    agent.llm.chat.completions.create = AsyncMock(
        side_effect=Exception("model not found")
    )

    with pytest.raises(Exception) as exc:
        asyncio.run(
            agent._chat(
                [{"role": "user", "content": "hello"}],
                tools=False,
            )
        )
    assert "model not found" in str(exc.value)
    fallback_client.chat.completions.create.assert_not_awaited()


def test_quota_notice_is_sent_once_per_reason_per_day(tmp_path):
    agent, db, _ = _agent(tmp_path)

    async def run():
        await agent._chat([{"role": "user", "content": "one"}], tools=False)
        await agent._chat([{"role": "user", "content": "two"}], tools=False)

    asyncio.run(run())
    agent.client.chat_postMessage.assert_awaited_once()
    assert db.kv_get(f"quota:hcai:notified:{utc_day()}:upstream") == "1"


def test_fallback_provider_prefers_db_over_env(tmp_path):
    from bot.ai.providers import fallback_provider

    db = Database(tmp_path / "router.db")
    db.init()
    settings = _settings(tmp_path)
    provider = fallback_provider(settings, db)
    assert provider is not None
    assert provider.api_key == "fallback-key"
    assert provider.model == "fallback-model"

    db.kv_set("config:ai_router_api_key", "db-key")
    db.kv_set("config:ai_router_models", "db-model,db-backup")
    db.kv_set("config:ai_router_base_url", "https://db.example/v1")
    provider = fallback_provider(settings, db)
    assert provider is not None
    assert provider.api_key == "db-key"
    assert provider.models == ("db-model", "db-backup")
    assert provider.model == "db-model"
    assert provider.base_url == "https://db.example/v1"


def test_chat_tries_fallback_models_in_order(tmp_path):
    agent, _, fallback_client = _agent(tmp_path)
    fallback_client.chat.completions.create = AsyncMock(
        side_effect=[Exception("first unavailable"), _response("second answer")]
    )
    from bot.ai.providers import FallbackProvider

    provider = FallbackProvider(
        base_url="https://router.example/v1",
        api_key="fallback-key",
        models=("first/model", "second/model"),
    )
    agent._fallback = lambda: (fallback_client, provider)

    async def run():
        response, used = await agent._chat(
            [{"role": "user", "content": "hello"}], tools=False
        )
        assert response.choices[0].message.content == "second answer"
        assert used == "fallback"

    asyncio.run(run())
    calls = fallback_client.chat.completions.create.call_args_list
    assert [call.kwargs["model"] for call in calls] == ["first/model", "second/model"]

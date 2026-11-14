import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.ai.agent import Agent, QuotaExhausted
from bot.ai.budget import utc_day
from bot.ai.providers import FallbackProvider
from bot.db import Database


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
        ai_router_model="kgw/kilo-auto/free",
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
    agent.llm.chat.completions.create = AsyncMock(
        side_effect=Exception("402 payment required")
    )
    fallback_client = MagicMock()
    fallback_client.chat.completions.create = AsyncMock(return_value=_response())
    provider = FallbackProvider(
        base_url="https://router.example/v1",
        api_key="fallback-key",
        model="kgw/kilo-auto/free",
    )
    agent._fallback = lambda: (fallback_client, provider)
    return agent, db, fallback_client


def test_chat_switches_to_9router_on_quota_error(tmp_path):
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
    assert kwargs["model"] == "kgw/kilo-auto/free"
    assert "tools" not in kwargs
    assert db.kv_get(f"quota:hcai:{utc_day()}") == "empty"
    agent.client.chat_postMessage.assert_awaited_once()


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


def test_quota_notice_is_sent_once_per_day(tmp_path):
    agent, db, _ = _agent(tmp_path)

    async def run():
        await agent._chat([{"role": "user", "content": "one"}], tools=False)
        await agent._chat([{"role": "user", "content": "two"}], tools=False)

    asyncio.run(run())
    agent.client.chat_postMessage.assert_awaited_once()
    assert db.kv_get(f"quota:hcai:notified:{utc_day()}") == "1"

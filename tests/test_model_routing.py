from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from bot.ai.agent import Agent


def test_code_model_routing_uses_code_model_after_code_tools():
    settings = MagicMock()
    settings.ai_api_key = "key"
    settings.ai_base_url = "https://ai.example.com/v1"
    settings.ai_model = "bot-chat"
    settings.ai_fallback_model = None
    settings.ai_assistant_model = "boi-chat"
    settings.ai_assistant_fallback_model = None
    settings.ai_code_model = "code-model"
    settings.ai_code_fallback_model = "code-fallback"
    settings.ai_daily_budget_usd = 0
    settings.ai_user_daily_budget_usd = 0
    settings.ai_enabled = True
    db = MagicMock()
    db.kv_get.return_value = None

    agent = Agent(
        settings,
        db,
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
    )

    assert agent._models_for(False) == ["bot-chat"]
    assert agent._models_for(False, code=True) == [
        "code-model",
        "code-fallback",
        "bot-chat",
    ]
    assert agent._models_for(True) == ["boi-chat", "bot-chat"]
    assert agent._models_for(True, code=True) == [
        "code-model",
        "code-fallback",
        "boi-chat",
        "bot-chat",
    ]

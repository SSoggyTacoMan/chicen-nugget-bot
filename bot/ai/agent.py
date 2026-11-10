from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from bot.ai.tools import ToolContext, Toolbox, format_now
from bot.config import Settings
from bot.db import Database
from bot.slack_api import channel_info, user_info
from bot.utils.text import display_name
from bot.utils.tz import tzinfo

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "system.md"
MAX_TOOL_ROUNDS = 6
MAX_TOOL_RESULT = 7000

# How this turn started, in words the persona can act on. The model can't tell
# a mention from an ambient chiming-in otherwise, because mentions are stripped
# before they reach it.
TRIGGER_TEXT = {
    "dm": "this is a direct message to you.",
    "mention": "you were @mentioned in the latest message.",
    "thread_followup": "someone replied in a thread you are already part of.",
    "ambient": "nobody addressed you. you chose to chime in on the latest message because you thought you could help.",
}


class Agent:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        client,
        user_client=None,
        bot_user_id: str,
    ) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        self.user_client = user_client
        self.bot_user_id = bot_user_id
        self.llm = AsyncOpenAI(
            api_key=settings.ai_api_key,
            base_url=settings.ai_base_url,
            timeout=60.0,
            max_retries=1,
        )
        self.toolbox = Toolbox(settings)
        self.system_template = PROMPT_PATH.read_text(encoding="utf-8")

    async def reply(
        self,
        *,
        conversation: str,
        user_id: str,
        text: str,
        channel_id: str | None,
        thread_ts: str | None,
        is_dm: bool,
        trigger: str = "mention",
        jev: str = "",
    ) -> str:
        """Run one agent turn (with tools) and persist both sides of it."""
        if not self.settings.ai_enabled:
            return "my ai brain isn't configured yet. add AI_API_KEY and restart me."

        history = self.db.history(conversation, self.settings.ai_history_limit)
        role = "owner" if user_id == self.settings.owner_user_id else "guest"
        location = await self._location(channel_id, is_dm)
        system = (
            self.system_template.replace("{bot_name}", self.settings.bot_name)
            .replace("{now}", format_now(self.settings))
            .replace("{user_id}", user_id)
            .replace("{is_owner}", "yes" if role == "owner" else "no")
            .replace("{is_admin}", "yes" if self.settings.is_admin(user_id) else "no")
            .replace("{owner_id}", self.settings.owner_user_id)
            .replace("{location}", location)
            .replace("{trigger}", TRIGGER_TEXT.get(trigger, TRIGGER_TEXT["mention"]))
            .replace("{jev}", jev or "no extra classification")
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for item in history:
            if item["role"] in {"user", "assistant"}:
                messages.append({"role": item["role"], "content": item["text"]})
        messages.append({"role": "user", "content": text})
        self.db.add_message(conversation, "user", user_id, text)

        ctx = ToolContext(
            db=self.db,
            settings=self.settings,
            client=self.client,
            user_client=self.user_client,
            bot_user_id=self.bot_user_id,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_dm=is_dm,
            is_owner=user_id == self.settings.owner_user_id,
            is_admin=self.settings.is_admin(user_id),
        )

        answer = ""
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = await self._chat(messages, tools=True)
                message = response.choices[0].message
                tool_calls = message.tool_calls or []
                if tool_calls:
                    payload: dict[str, Any] = {
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.function.name,
                                    "arguments": call.function.arguments or "{}",
                                },
                            }
                            for call in tool_calls
                        ],
                    }
                    messages.append(payload)
                    for call in tool_calls:
                        try:
                            args = json.loads(call.function.arguments or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        result = await self.toolbox.execute(call.function.name, args, ctx)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": result[:MAX_TOOL_RESULT],
                            }
                        )
                    continue

                answer = (message.content or "").strip()
                break
            else:
                answer = "i got stuck in a tool loop. try asking again, maybe a little smaller."
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent completion failed")
            answer = self._friendly_error(exc)

        if answer:
            self.db.add_message(conversation, "assistant", self.bot_user_id, answer)
        return answer or "hmm, i blanked. try again?"

    async def complete(
        self, *, system: str, text: str, max_tokens: int | None = None, temperature: float = 0.5
    ) -> str:
        """One-shot completion with no tools or persisted history."""
        if not self.settings.ai_enabled:
            return "my ai brain isn't configured yet."
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ]
        try:
            response = await self._chat(
                messages,
                tools=False,
                temperature=temperature,
                max_tokens=max_tokens or self.settings.ai_max_tokens,
            )
            return (response.choices[0].message.content or "").strip() or "i blanked. try again?"
        except Exception as exc:  # noqa: BLE001
            logger.exception("one-shot completion failed")
            return self._friendly_error(exc)

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: bool,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ):
        models = [self.settings.ai_model]
        fallback = self.settings.ai_fallback_model
        if fallback and fallback != self.settings.ai_model:
            models.append(fallback)

        kwargs: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or self.settings.ai_max_tokens,
        }
        if tools:
            kwargs["tools"] = self.toolbox.definitions
            kwargs["tool_choice"] = "auto"

        last_error: Exception | None = None
        for index, model in enumerate(models):
            try:
                response = await self.llm.chat.completions.create(model=model, **kwargs)
                usage = getattr(response, "usage", None)
                if usage:
                    logger.info(
                        "ai usage model=%s prompt=%s completion=%s total=%s",
                        model,
                        getattr(usage, "prompt_tokens", "?"),
                        getattr(usage, "completion_tokens", "?"),
                        getattr(usage, "total_tokens", "?"),
                    )
                if index:
                    logger.info("ai answered with fallback model %s", model)
                message = response.choices[0].message
                if not (message.content or "").strip() and not (message.tool_calls or []):
                    raise RuntimeError("empty response")
                return response
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if index + 1 < len(models):
                    logger.warning(
                        "model %s failed (%s); trying fallback %s",
                        model,
                        str(exc)[:160],
                        models[index + 1],
                    )
                    continue
                raise
        raise last_error or RuntimeError("no ai model configured")

    async def _location(self, channel_id: str | None, is_dm: bool) -> str:
        if is_dm:
            return "a direct message"
        if not channel_id:
            return "a slack channel"
        info = await channel_info(self.client, channel_id)
        if info:
            kind = "private channel" if info.get("is_private") else "channel"
            return f"the {kind} #{info.get('name')} (<#{channel_id}>)"
        return f"channel <#{channel_id}>"

    def _friendly_error(self, exc: Exception) -> str:
        text = str(exc)
        lower = text.lower()
        if "402" in text or "payment required" in lower or "spending limit" in lower or "budget" in lower:
            return "my hack club ai daily budget is used up. it resets at 00:00 UTC."
        if "api key" in lower or "401" in text or "unauthorized" in lower:
            return "my ai key got rejected. check AI_API_KEY / AI_BASE_URL in my .env."
        if "model" in lower and ("not found" in lower or "does not exist" in lower):
            return f"i couldn't find the model `{self.settings.ai_model}`. check AI_MODEL."
        if "tool" in lower:
            return (
                "that model doesn't seem to support tool calling. try a tool-capable model "
                "(openai/gpt-oss-20b, qwen/qwen3-32b, etc.) in AI_MODEL."
            )
        if "rate limit" in lower or "429" in text:
            return "i'm being rate limited by the ai provider. give it a minute."
        if "timeout" in lower or "timed out" in lower:
            return "the ai took too long to answer. try a shorter question or give it another go."
        if "502" in text or "503" in text or "server error" in lower or "bad gateway" in lower:
            return "the ai provider is having a moment (upstream error). try again in a bit."
        if "context" in lower and ("length" in lower or "long" in lower):
            return "that conversation got too long for the model. try `/forget` and ask again."
        if "empty response" in lower:
            return "the model returned nothing. try again, or ask a smaller question."
        return f"my ai brain hiccuped: {text[:300]}"


async def owner_label(client, user_id: str) -> str:
    user = await user_info(client, user_id)
    return display_name(user)


__all__ = ["Agent", "owner_label", "tzinfo"]

from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from slack_sdk.web.async_client import AsyncWebClient

from bot.ai.budget import Budget, BudgetExceeded, estimate_tokens
from bot.ai.tools import ToolContext, Toolbox, format_now, get_user_memory_prompt
from bot.config import Settings
from bot.db import Database
from bot.runtime import runtime
from bot.slack_api import channel_info, user_info
from bot.utils.text import display_name, format_mrkdwn
from bot.utils.tz import tzinfo

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "system.md"
PROMPT_DIGEST_KEY = "persona_digest"
MAX_TOOL_ROUNDS = 6
MAX_TOOL_RESULT = 7000


def sync_prompt_digest(db: Database, template: str) -> int:
    """Clear old chat turns when the persona changes.

    The model learns style from the assistant messages in its history, so after
    a prompt rewrite the old voice keeps leaking through. Resetting on a new
    prompt hash starts every conversation on the new voice. Notes are untouched.
    """
    digest = hashlib.sha256(template.encode("utf-8")).hexdigest()
    previous = db.kv_get(PROMPT_DIGEST_KEY)
    if previous == digest:
        return 0
    cleared = db.clear_all_history()
    db.kv_set(PROMPT_DIGEST_KEY, digest)
    logger.info(
        "persona changed (%s -> %s); cleared %s stored turns",
        (previous or "none")[:8],
        digest[:8],
        cleared,
    )
    return cleared

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
        user_id: str = "",
        budget: Budget | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        self.user_client = user_client
        self.bot_user_id = bot_user_id
        self.user_id = user_id
        self.budget = budget or Budget(settings, db)
        self.llm = AsyncOpenAI(
            api_key=settings.ai_api_key,
            base_url=settings.ai_base_url,
            timeout=60.0,
            max_retries=1,
        )
        self.toolbox = Toolbox(settings)
        self.system_template = PROMPT_PATH.read_text(encoding="utf-8")
        sync_prompt_digest(self.db, self.system_template)

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
        images: list[str] | None = None,
        client: AsyncWebClient | None = None,
    ) -> str:
        """Run one agent turn (with tools) and persist both sides of it."""
        if not self.settings.ai_enabled:
            return "my ai brain isn't configured yet. add AI_API_KEY and restart me."

        active_client = client or self.client
        is_user_identity = bool(self.user_client and active_client == self.user_client)
        active_identity = (
            "Chicen Nugget Boi (personal assistant user account)"
            if is_user_identity
            else "Chicen Nugget Bot (workspace bot app)"
        )
        assistant_id = self.user_id or getattr(runtime, "user_id", "") or "unknown"
        history = self.db.history(conversation, self.settings.ai_history_limit)
        role = "owner" if user_id == self.settings.owner_user_id else "guest"
        location = await self._location(channel_id, is_dm, client=active_client)
        memory = (
            get_user_memory_prompt(self.db, self.settings, user_id)
            if (role == "owner" or self.settings.is_admin(user_id))
            else "none"
        )
        has_google = bool(
            self.settings.google_access_token
            or self.settings.google_refresh_token
            or (self.db and self.db.kv_get("config:google_access_token"))
            or (self.db and self.db.kv_get("config:google_refresh_token"))
        )
        has_mac = bool(
            self.settings.mac_bridge_url
            or (self.db and self.db.kv_get("config:mac_bridge_url"))
            or (sys.platform == "darwin" and not self.settings.mac_bridge_url)
        )
        gh_user = (
            (self.db.kv_get("config:github_username") if self.db else None)
            or self.settings.github_username
            or "SSoggyTacoMan"
        )
        integrations = (
            f"- GitHub & Hackatime: connected (default user: {gh_user})\n"
            f"- Google Workspace (Calendar, Gmail, Drive): {'connected' if has_google else 'not configured'}\n"
            f"- Mac companion link (Volume, Apps, Clipboard, Shell): {'connected' if has_mac else 'not configured'}"
        )
        system = (
            self.system_template.replace("{bot_name}", self.settings.bot_name)
            .replace("{bot_user_id}", self.bot_user_id or "unknown")
            .replace("{assistant_user_id}", assistant_id)
            .replace("{active_identity}", active_identity)
            .replace("{now}", format_now(self.settings))
            .replace("{user_id}", user_id)
            .replace("{is_owner}", "yes" if role == "owner" else "no")
            .replace("{is_admin}", "yes" if self.settings.is_admin(user_id) else "no")
            .replace("{owner_id}", self.settings.owner_user_id)
            .replace("{location}", location)
            .replace("{trigger}", TRIGGER_TEXT.get(trigger, TRIGGER_TEXT["mention"]))
            .replace("{jev}", jev or "no extra classification")
            .replace("{memory}", memory)
            .replace("{integrations}", integrations)
            .replace("{ai_model}", self.settings.ai_model)
            .replace("{ai_fallback_model}", self.settings.ai_fallback_model or "none")
            .replace("{imagine_model}", self.settings.imagine_model or "none")
            .replace("{gate_model}", self.settings.ai_gate_model or "none")
            .replace("{repo_url}", self.settings.repo_url)
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(fit_history(history, text, self.settings.ai_max_context_chars))
        if images:
            user_content: list[dict[str, Any]] = [
                {"type": "text", "text": text[: self.settings.ai_max_context_chars] or "Look at this image."}
            ]
            for img in images:
                user_content.append({"type": "image_url", "image_url": {"url": img}})
            messages.append({"role": "user", "content": user_content})
            stored_text = text if not images else f"{text} [image attached]".strip()
            self.db.add_message(conversation, "user", user_id, stored_text)
        else:
            messages.append({"role": "user", "content": text[: self.settings.ai_max_context_chars]})
            self.db.add_message(conversation, "user", user_id, text)

        ctx = ToolContext(
            db=self.db,
            settings=self.settings,
            client=active_client,
            user_client=self.user_client,
            bot_user_id=self.bot_user_id,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_dm=is_dm,
            is_owner=user_id == self.settings.owner_user_id,
            is_admin=self.settings.is_admin(user_id),
            bot_client=self.client,
        )

        answer = ""
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = await self._chat(messages, tools=True, user_id=user_id)
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
        except BudgetExceeded as exc:
            logger.warning("ai budget exhausted: %s", exc)
            answer = _budget_text(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent completion failed")
            answer = self._friendly_error(exc)

        answer = format_mrkdwn(answer)

        if answer and answer.strip() not in {"[NO_REPLY]", "NO_REPLY"}:
            self.db.add_message(conversation, "assistant", self.bot_user_id, answer)
            return answer
        if trigger == "mention" and not answer:
            return "hmm, i blanked. try again?"
        return ""

    async def complete(
        self,
        *,
        system: str,
        text: str,
        max_tokens: int | None = None,
        temperature: float = 0.5,
        user_id: str | None = None,
    ) -> str:
        """One-shot completion with no tools or persisted history."""
        if not self.settings.ai_enabled:
            return "my ai brain isn't configured yet."
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": text[: self.settings.ai_max_context_chars]},
        ]
        try:
            response = await self._chat(
                messages,
                tools=False,
                temperature=temperature,
                max_tokens=max_tokens or self.settings.ai_max_tokens,
                user_id=user_id,
            )
            raw = (response.choices[0].message.content or "").strip()
            return format_mrkdwn(raw) or "i blanked. try again?"
        except BudgetExceeded as exc:
            logger.warning("ai budget exhausted: %s", exc)
            return _budget_text(exc)
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
        user_id: str | None = None,
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
            if self.budget is not None:
                status = self.budget.status()
                if status.exhausted:
                    raise BudgetExceeded(status)
                if user_id and self.budget.user_exhausted(user_id):
                    raise BudgetExceeded(self.budget.user_status(user_id), scope="user")
            try:
                response = await self.llm.chat.completions.create(model=model, **kwargs)
                message = response.choices[0].message
                if index:
                    logger.info("ai answered with fallback model %s", model)
                self._record_usage(model, messages, message, response, user_id=user_id)
                if not (message.content or "").strip() and not (message.tool_calls or []):
                    raise RuntimeError("empty response")
                return response
            except BudgetExceeded:
                raise
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

    def _record_usage(self, model, messages, message, response, user_id: str | None = None) -> None:
        if self.budget is None:
            return
        usage = getattr(response, "usage", None)
        if usage is not None and getattr(usage, "prompt_tokens", None) is not None:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        else:
            prompt_tokens = estimate_tokens(
                "".join(str(item.get("content") or "") for item in messages)
            )
            completion_tokens = estimate_tokens((message.content or "") if message else "")
        self.budget.record(
            kind="chat",
            model=model,
            user_id=user_id or "",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def _location(self, channel_id: str | None, is_dm: bool, client: AsyncWebClient | None = None) -> str:
        if is_dm:
            return "a direct message"
        if not channel_id:
            return "a slack channel"
        info = await channel_info(client or self.client, channel_id)
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


def _budget_text(exc: BudgetExceeded) -> str:
    status = exc.status
    if exc.scope == "user":
        return (
            f"you've hit today's ai limit (${status.spent:.2f} of ${status.limit:.2f}). "
            "it resets at 00:00 UTC."
        )
    return (
        f"i've used up my ai budget for today (${status.spent:.2f} of ${status.limit:.2f}). "
        "it resets at 00:00 UTC."
    )


def fit_history(
    history: list[dict[str, Any]], text: str, max_chars: int
) -> list[dict[str, Any]]:
    """Keep the newest turns that fit, so a long thread can't balloon input cost."""
    room = max(0, max_chars - len(text))
    kept: list[dict[str, Any]] = []
    for item in reversed(history):
        if item["role"] not in {"user", "assistant"}:
            continue
        content = (item["text"] or "").strip()
        if not content:
            continue
        uid = item.get("user_id")
        if uid and item["role"] == "user" and not content.startswith(f"<@{uid}>") and not content.startswith("<@"):
            content = f"<@{uid}>: {content}"
        if len(content) > room:
            break
        kept.append({"role": item["role"], "content": content})
        room -= len(content)
    kept.reverse()
    return kept


async def owner_label(client, user_id: str) -> str:
    user = await user_info(client, user_id)
    return display_name(user)


__all__ = ["Agent", "fit_history", "owner_label", "sync_prompt_digest", "tzinfo"]

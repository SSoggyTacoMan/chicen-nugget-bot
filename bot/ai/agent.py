from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from slack_sdk.web.async_client import AsyncWebClient

from bot import integrations
from bot.ai import images as image_settings
from bot.ai import tts
from bot.ai.budget import Budget, BudgetExceeded, estimate_tokens, utc_day
from bot.ai.memory import MemoryIndex
from bot.ai.providers import FallbackProvider, fallback_provider
from bot.ai.tools import Toolbox, ToolContext, format_now, get_user_memory_prompt
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


def is_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "402",
            "payment required",
            "insufficient_quota",
            "quota",
            "spending limit",
            "daily limit",
            "daily budget",
        )
    )


PRIVATE_DETAIL_RE = re.compile(
    r"\b(?:sandbox|shell|tokens?|host|vps|\.env|sqlite|system prompt|"
    r"api key|openai|anthropic|deepseek|qwen|ssh|repo_url|github token|"
    r"credentials?|oauth|missing scope|bot token|user token|env file)\b"
    r"|/nugget setup\b"
    r"|\bsetup\s+(?:github|email|slack|google|apis|welcome|fallback|voice|images)\b",
    re.IGNORECASE,
)


class QuotaExhausted(RuntimeError):
    """The primary quota is empty and the fallback could not answer either."""

    def __init__(self, primary: str, fallback: str | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        detail = f"primary: {primary}"
        if fallback:
            detail += f"; fallback: {fallback}"
        super().__init__(detail)
CODE_TOOLS = {
    "sandbox",
    "github_write",
    "api_request",
    "browser",
    "run_code",
    "inspect_code",
    "clone_repo",
}
CODE_HINT_RE = re.compile(
    r"\b(?:pr|pull\s+request|merge|commit|branch|repo(?:sitory)?|sandbox|"
    r"patch|diff|review|deploy|build|test(?:s|ing)?|debug|fix|refactor|"
    r"typescript|python|javascript|docker|github)\b",
    re.IGNORECASE,
)
SIMPLE_CHAT_RE = re.compile(
    r"\b(?:search|look\s+up|check|find|open|run|build|deploy|fix|review|"
    r"summar(?:y|ize|ise)|remind|schedule|email|calendar|github|pr|repo|"
    r"sandbox|file|image|chart|deck|hackatime|weather|docs?|link|upload|"
    r"download|browser|api)\b",
    re.IGNORECASE,
)


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
    "mpim_mention": "you were @mentioned in a group DM.",
    "named": "someone addressed you by name in a channel message without an @mention.",
    "reaction": "someone reacted to a message to ask you to act on it.",
    "shortcut": "someone used a message shortcut on this message.",
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
        self._fallback_client: AsyncOpenAI | None = None
        self._fallback_sig: tuple[str, str] | None = None
        self.toolbox = Toolbox(settings, db)
        self.memory = MemoryIndex(settings, self.llm)
        self._embedding_tasks: set[asyncio.Task] = set()
        self.system_template = PROMPT_PATH.read_text(encoding="utf-8")
        sync_prompt_digest(self.db, self.system_template)

    def _remember(
        self,
        *,
        message_id: int,
        conversation: str,
        role: str,
        user_id: str | None,
        text: str,
    ) -> None:
        if not self.memory.enabled or not message_id:
            return
        task = asyncio.create_task(
            self.memory.remember(
                self.db,
                message_id=message_id,
                conversation=conversation,
                role=role,
                user_id=user_id,
                text=text,
            )
        )
        self._embedding_tasks.add(task)
        task.add_done_callback(self._embedding_tasks.discard)

    async def reply(
        self,
        *,
        conversation: str,
        user_id: str,
        text: str,
        channel_id: str | None,
        thread_ts: str | None,
        is_dm: bool,
        is_mpim: bool = False,
        trigger: str = "mention",
        jev: str = "",
        images: list[str] | None = None,
        context: str = "",
        public_veto: bool = False,
        client: AsyncWebClient | None = None,
    ) -> str:
        """Run one agent turn (with tools) and persist both sides of it."""
        if not self.settings.ai_enabled:
            return "my ai brain isn't configured yet. add AI_API_KEY and restart me."
        if context.strip():
            text = (
                "[recent channel context, for reference]\n"
                f"{context.strip()}\n"
                "[end context]\n\n"
                f"[latest message]\n{text}"
            )

        active_client = client or self.client
        is_user_identity = bool(self.user_client and active_client == self.user_client)
        active_identity = (
            "Chicen Nugget Boi (personal assistant user account)"
            if is_user_identity
            else "Chicen Nugget Bot (workspace bot app)"
        )
        assistant_id = (self.user_id or getattr(runtime, "user_id", "")) or "unknown"
        active_identity_id = (
            assistant_id if is_user_identity else (self.bot_user_id or "unknown")
        )
        owner_client = getattr(runtime, "owner_client", None)
        owner_posting = bool(owner_client)
        assistant_identity = bool(self.user_client)
        bot_model, bot_fallback, assistant_model, assistant_fallback = self._model_settings()
        code_model, code_fallback = self._code_model_settings()
        tts_model = tts.voice_config(self.settings, self.db).model or "none"
        imagine_model = (
            image_settings.imagine_config(self.settings, self.db).model or "none"
        )
        fast_model = self._fast_model() or "none"
        history = self.db.history(conversation, self.settings.ai_history_limit)
        role = "owner" if user_id == self.settings.owner_user_id else "guest"
        location = await self._location(
            channel_id, is_dm, is_mpim=is_mpim, client=active_client
        )
        memory = (
            get_user_memory_prompt(self.db, self.settings, user_id)
            if (role == "owner" or self.settings.is_admin(user_id))
            else "none"
        )
        integrations_text = integrations.prompt_integration_lines(
            self.db,
            self.settings,
            owner_posting=owner_posting,
            assistant_identity=assistant_identity,
            bot_model=bot_model,
            assistant_model=assistant_model,
        )
        system = (
            self.system_template.replace("{bot_name}", self.settings.bot_name)
            .replace("{bot_user_id}", self.bot_user_id or "unknown")
            .replace("{assistant_user_id}", assistant_id)
            .replace("{active_identity_user_id}", active_identity_id)
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
            .replace("{integrations}", integrations_text)
            .replace("{ai_model}", bot_model)
            .replace("{ai_fallback_model}", bot_fallback or "none")
            .replace("{ai_assistant_model}", assistant_model)
            .replace("{ai_assistant_fast_model}", fast_model)
            .replace("{ai_assistant_fallback_model}", assistant_fallback or "none")
            .replace(
                "{ai_code_model}",
                code_model or "not set (tool-heavy turns use the chat model)",
            )
            .replace("{ai_code_fallback_model}", code_fallback or "none")
            .replace("{imagine_model}", imagine_model)
            .replace("{tts_model}", tts_model)
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
            message_id = self.db.add_message(conversation, "user", user_id, stored_text)
            self._remember(
                message_id=message_id,
                conversation=conversation,
                role="user",
                user_id=user_id,
                text=stored_text,
            )
        else:
            messages.append({"role": "user", "content": text[: self.settings.ai_max_context_chars]})
            message_id = self.db.add_message(conversation, "user", user_id, text)
            self._remember(
                message_id=message_id,
                conversation=conversation,
                role="user",
                user_id=user_id,
                text=text,
            )

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
            owner_client=owner_client,
            assistant_user_id=active_identity_id,
        )

        answer = ""
        code_mode = bool(CODE_HINT_RE.search(text or ""))
        fast_chat = bool(
            is_user_identity
            and not code_mode
            and self._simple_chat(text, images=images)
        )
        used_fallback = False
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response, provider = await self._chat(
                    messages,
                    tools=True,
                    user_id=user_id,
                    is_user_identity=is_user_identity,
                    code=code_mode,
                    fast=fast_chat,
                )
                used_fallback = used_fallback or provider == "fallback"
                message = response.choices[0].message
                tool_calls = message.tool_calls or []
                if tool_calls:
                    fast_chat = False
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
                    if any(call.function.name in CODE_TOOLS for call in tool_calls):
                        code_mode = True
                    continue

                answer = (message.content or "").strip()
                break
            else:
                answer = "i got stuck in a tool loop. try asking again, maybe a little smaller."
        except BudgetExceeded as exc:
            logger.warning("ai budget exhausted: %s", exc)
            answer = _budget_text(exc)
        except Exception as exc:
            logger.exception("agent completion failed")
            answer = self._friendly_error(
                exc, model=self._models_for(is_user_identity, code=code_mode)[0]
            )

        answer = format_mrkdwn(answer)
        if public_veto and answer and PRIVATE_DETAIL_RE.search(answer):
            logger.info("public veto: suppressed answer with internal details")
            return ""
        if used_fallback and answer.strip() not in {"", "[NO_REPLY]", "NO_REPLY"}:
            answer = (
                f"{answer}\n\n_(hack club ai quota empty · 9router free fallback · "
                "resets 00:00 UTC)_"
            )

        if answer and answer.strip() not in {"[NO_REPLY]", "NO_REPLY"}:
            message_id = self.db.add_message(
                conversation, "assistant", self.bot_user_id, answer
            )
            self._remember(
                message_id=message_id,
                conversation=conversation,
                role="assistant",
                user_id=None,
                text=answer,
            )
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
            response, provider = await self._chat(
                messages,
                tools=False,
                temperature=temperature,
                max_tokens=max_tokens or self.settings.ai_max_tokens,
                user_id=user_id,
            )
            raw = (response.choices[0].message.content or "").strip()
            if provider == "fallback" and raw:
                raw = (
                    f"{raw}\n\n_(hack club ai quota empty · 9router free fallback · "
                    "resets 00:00 UTC)_"
                )
            return format_mrkdwn(raw) or "i blanked. try again?"
        except BudgetExceeded as exc:
            logger.warning("ai budget exhausted: %s", exc)
            return _budget_text(exc)
        except Exception as exc:
            logger.exception("one-shot completion failed")
            return self._friendly_error(exc)

    def _model_settings(self) -> tuple[str, str | None, str, str | None]:
        """Models can be overridden at runtime with /nugget config; env is the fallback."""

        def runtime(key: str, default: str | None) -> str | None:
            value = self.db.kv_get(f"config:{key}") if self.db else None
            return value if value else default

        return (
            runtime("ai_model", self.settings.ai_model) or self.settings.ai_model,
            runtime("ai_fallback_model", self.settings.ai_fallback_model),
            runtime("ai_assistant_model", self.settings.ai_assistant_model)
            or self.settings.ai_assistant_model,
            runtime("ai_assistant_fallback_model", self.settings.ai_assistant_fallback_model),
        )

    def _fast_model(self) -> str | None:
        value = self.db.kv_get("config:ai_assistant_fast_model") if self.db else None
        if not value:
            value = getattr(self.settings, "ai_assistant_fast_model", None)
        return value if isinstance(value, str) and value.strip() else None

    def _fast_fallback_model(self) -> str | None:
        value = (
            self.db.kv_get("config:ai_assistant_fast_fallback_model")
            if self.db
            else None
        )
        if not value:
            value = getattr(self.settings, "ai_assistant_fast_fallback_model", None)
        return value if isinstance(value, str) and value.strip() else None

    def _simple_chat(self, text: str, *, images: list[str] | None = None) -> bool:
        clean = (text or "").strip()
        if not clean or images or "[Voice message]" in clean:
            return False
        if len(clean) > 180 or clean.count("\n") > 2:
            return False
        if CODE_HINT_RE.search(clean) or SIMPLE_CHAT_RE.search(clean):
            return False
        if "http://" in clean or "https://" in clean or "`" in clean:
            return False
        return True

    def _code_model_settings(self) -> tuple[str | None, str | None]:
        def text(value: object) -> str | None:
            return value if isinstance(value, str) and value.strip() else None

        def runtime(key: str, default: object) -> str | None:
            value = self.db.kv_get(f"config:{key}") if self.db else None
            return text(value) or text(default)

        return (
            runtime("ai_code_model", self.settings.ai_code_model),
            runtime("ai_code_fallback_model", self.settings.ai_code_fallback_model),
        )

    def _models_for(
        self, is_user_identity: bool, *, code: bool = False, fast: bool = False
    ) -> list[str]:
        """Bot, Boi and tool-heavy turns can use different models."""
        bot_model, bot_fallback, assistant_model, assistant_fallback = self._model_settings()
        code_model, code_fallback = self._code_model_settings()
        fast_model = self._fast_model()
        fast_fallback = self._fast_fallback_model()
        if code and code_model:
            if is_user_identity:
                candidates = [
                    code_model,
                    code_fallback,
                    assistant_model,
                    assistant_fallback,
                    bot_model,
                    bot_fallback,
                ]
            else:
                candidates = [code_model, code_fallback, bot_model, bot_fallback]
        elif is_user_identity and fast and fast_model:
            candidates = [
                fast_model,
                fast_fallback,
                assistant_model,
                assistant_fallback,
                bot_model,
                bot_fallback,
            ]
        elif is_user_identity:
            candidates = [assistant_model, assistant_fallback, bot_model, bot_fallback]
        else:
            candidates = [bot_model, bot_fallback]
        models: list[str] = []
        for model in candidates:
            if model and model not in models:
                models.append(model)
        return models or [bot_model]

    def _fallback(self) -> tuple[AsyncOpenAI, FallbackProvider] | None:
        provider = fallback_provider(self.settings, self.db)
        if provider is None:
            return None
        signature = (provider.base_url, provider.api_key)
        if self._fallback_client is None or self._fallback_sig != signature:
            self._fallback_client = AsyncOpenAI(
                api_key=provider.api_key,
                base_url=provider.base_url,
                timeout=60.0,
                max_retries=1,
            )
            self._fallback_sig = signature
        return self._fallback_client, provider

    def _quota_empty(self) -> bool:
        return bool(
            self.db and self.db.kv_get(f"quota:hcai:{utc_day()}")
        )

    def _mark_quota_empty(self) -> None:
        if self.db:
            self.db.kv_set(f"quota:hcai:{utc_day()}", "empty")

    async def _notify_quota_once(self, provider: FallbackProvider) -> None:
        if not self.db:
            return
        key = f"quota:hcai:notified:{utc_day()}"
        if self.db.kv_get(key):
            return
        self.db.kv_set(key, "1")
        models = ", ".join(f"`{model}`" for model in provider.models)
        text = (
            "hack club ai quota is empty for today. "
            f"i'm on the 9router fallback ({models}), so replies may be slower "
            "and tool use is off. it resets at 00:00 UTC. voice, images and memory "
            "search need HCAI quota."
        )
        try:
            await self.client.chat_postMessage(
                channel=self.settings.owner_user_id, text=text
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not send quota notice: %s", exc)

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: bool,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        user_id: str | None = None,
        is_user_identity: bool = False,
        code: bool = False,
        fast: bool = False,
    ) -> tuple[Any, str]:
        models = self._models_for(is_user_identity, code=code, fast=fast)

        kwargs: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or self.settings.ai_max_tokens,
        }
        if tools:
            kwargs["tools"] = self.toolbox.definitions
            kwargs["tool_choice"] = "auto"

        fallback = self._fallback()
        quota_error: Exception | None = None
        providers: list[tuple[str, AsyncOpenAI, list[str]]] = []
        if not self._quota_empty():
            providers.append(("primary", self.llm, models))
        else:
            quota_error = RuntimeError("hack club ai quota is empty")
        if fallback is not None:
            providers.append(("fallback", fallback[0], list(fallback[1].models)))
        if not providers:
            raise QuotaExhausted(
                "hack club ai quota is empty", "no fallback key configured"
            )

        last_error: Exception | None = None
        for provider_name, client, provider_models in providers:
            if provider_name == "fallback" and quota_error is None:
                continue
            provider_kwargs = dict(kwargs)
            provider_messages = messages
            if provider_name == "fallback":
                provider_kwargs.pop("tools", None)
                provider_kwargs.pop("tool_choice", None)
                provider_messages = messages + [
                    {
                        "role": "system",
                        "content": (
                            "The primary ai quota is empty. You are the free fallback "
                            "model and have no tools. Answer briefly from the context "
                            "you already have. Do not claim to use tools."
                        ),
                    }
                ]
                provider_kwargs["messages"] = provider_messages

            for index, model in enumerate(provider_models):
                if self.budget is not None:
                    status = self.budget.status()
                    if status.exhausted:
                        raise BudgetExceeded(status)
                    if user_id and self.budget.user_exhausted(user_id):
                        raise BudgetExceeded(
                            self.budget.user_status(user_id), scope="user"
                        )
                try:
                    response = await client.chat.completions.create(
                        model=model, **provider_kwargs
                    )
                    message = response.choices[0].message
                    if index:
                        logger.info("ai answered with fallback model %s", model)
                    if provider_name == "fallback":
                        logger.warning(
                            "ai answered with %s fallback %s", provider_name, model
                        )
                    self._record_usage(
                        model,
                        provider_messages,
                        message,
                        response,
                        user_id=user_id,
                    )
                    if not (message.content or "").strip() and not (
                        message.tool_calls or []
                    ):
                        raise RuntimeError("empty response")
                    if provider_name == "fallback" and fallback is not None:
                        self._mark_quota_empty()
                        await self._notify_quota_once(fallback[1])
                    return response, provider_name
                except BudgetExceeded:
                    raise
                except Exception as exc:  # noqa: BLE001 - provider errors are retried
                    last_error = exc
                    if provider_name == "primary" and is_quota_error(exc):
                        quota_error = exc
                        self._mark_quota_empty()
                        logger.warning(
                            "primary ai quota exhausted: %s", str(exc)[:160]
                        )
                        break
                    if index + 1 < len(provider_models):
                        logger.warning(
                            "model %s failed (%s); trying fallback %s",
                            model,
                            str(exc)[:160],
                            provider_models[index + 1],
                        )
                        continue
                    break

        if quota_error is not None:
            raise QuotaExhausted(
                str(quota_error)[:160],
                str(last_error)[:160] if last_error else None,
            )
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

    async def _location(
        self,
        channel_id: str | None,
        is_dm: bool,
        *,
        is_mpim: bool = False,
        client: AsyncWebClient | None = None,
    ) -> str:
        if is_dm:
            return "a direct message"
        if is_mpim:
            return "a group DM"
        if not channel_id:
            return "a slack channel"
        info = await channel_info(client or self.client, channel_id)
        if info:
            kind = "private channel" if info.get("is_private") else "channel"
            return f"the {kind} #{info.get('name')} (<#{channel_id}>)"
        return f"channel <#{channel_id}>"

    def _friendly_error(self, exc: Exception, model: str | None = None) -> str:
        text = str(exc)
        lower = text.lower()
        if isinstance(exc, QuotaExhausted):
            if exc.fallback:
                return (
                    "hack club ai quota is empty for today, and the 9router fallback "
                    "also failed. voice, images and memory search need hacai quota. "
                    "it resets at 00:00 UTC."
                )
            return (
                "hack club ai quota is empty for today. add a 9router fallback key "
                "with `/nugget setup fallback`, or wait until 00:00 UTC. voice, "
                "images and memory search need hacai quota."
            )
        if "402" in text or "payment required" in lower or "spending limit" in lower or "budget" in lower:
            return "my hack club ai daily budget is used up. it resets at 00:00 UTC."
        if "api key" in lower or "401" in text or "unauthorized" in lower:
            return "my ai key got rejected. check AI_API_KEY / AI_BASE_URL in my .env."
        if "model" in lower and ("not found" in lower or "does not exist" in lower):
            return f"i couldn't find the model `{model or self.settings.ai_model}`. check AI_MODEL."
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

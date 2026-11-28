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
from bot.ai.budget import Budget, BudgetExceeded, estimate_tokens
from bot.ai.gate import Gate
from bot.ai.memory import MemoryIndex
from bot.ai.providers import FallbackProvider, fallback_provider
from bot.ai.quota import (
    KIND_DAILY,
    KIND_UNKNOWN,
    KIND_UPSTREAM,
    classify_quota_error,
    clear_cooldown,
    mark_unavailable,
    notice_key,
    primary_status,
)
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
MAX_IDENTICAL_TOOL_CALLS = 2
MAX_TOOL_RESULT = 7000

# Appended when a turn runs out of tool rounds (or repeats itself). Without it
# the model used to send "i got stuck in a tool loop" to the user instead of
# answering from the tool results it already had.
FORCE_ANSWER_NUDGE = (
    "you are out of tool calls for this turn. answer now in one or two short lines "
    "using what the tools already returned. if a tool returned nothing, say that "
    "plainly. never mention tool names, never write '(calls ...)', and never offer "
    "to run it again."
)

# Some models write "(calls search_slack ...)" as prose instead of making the
# tool call. Feeding that back makes the next round call the real tool.
PSEUDO_TOOL_NUDGE = (
    "you wrote about calling a tool, waiting on one, or rerunning it instead of "
    "using it. a tool call blocks until it returns, so there is no pending result. "
    "do not narrate tools and do not show their names. make the real tool call now, "
    "or answer in one line with what you already know (including 'nothing matched')."
)

_PSEUDO_TOOL_RE = re.compile(
    r"(?:^|[\s(\[])"
    r"(?:calls?|calling|invokes?|invoking|runs?|running|uses?|using|reruns?|rerunning)"
    r"\s+[`'\"]?(?P<name>[a-z][a-z0-9_]{2,})",
    re.IGNORECASE,
)

# A tool call blocks until it returns. Anything "still loading" or queued for a
# rerun is the model stalling, not a real state.
_STALLED_STATUS_RE = re.compile(
    r"\bi(?:'ll| will)\s+(?:rerun|re-run|try again|check again)\b"
    r"|\bi\s+(?:don'?t|do not)\s+have\s+(?:a|the)\s+result\b"
    r"|\b(?:still|yet)\s+(?:waiting|loading|running|searching)\b"
    r"|\bsearch(?:ing)?\s+(?:is|still)\s+(?:running|loading|pending)\b",
    re.IGNORECASE,
)


def looks_like_pseudo_tool_call(text: str, tool_names: frozenset[str] | set[str]) -> bool:
    """True when the model described or stalled on a tool instead of using it."""
    if not text:
        return False
    if _STALLED_STATUS_RE.search(text):
        return True
    if not tool_names:
        return False
    names = {name.lower() for name in tool_names}
    return any(
        match.group("name").lower() in names
        for match in _PSEUDO_TOOL_RE.finditer(text)
    )


def tool_call_signature(name: str, arguments: str | None) -> str:
    """Stable identity for a tool call, so an identical repeat is detectable."""
    try:
        parsed = json.loads(arguments or "{}")
        args = json.dumps(parsed, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        args = (arguments or "").strip()
    return f"{name}:{args}"


def is_quota_error(exc: Exception) -> bool:
    """Backward-compatible wrapper for the quota classifier."""

    return bool(classify_quota_error(exc))


PRIVATE_DETAIL_RE = re.compile(
    r"\b(?:sandbox|shell|tokens?|host|vps|\.env|sqlite|system prompt|"
    r"api key|openai|anthropic|deepseek|qwen|ssh|repo_url|github token|"
    r"credentials?|oauth|missing scope|bot token|user token|env file)\b"
    r"|/nugget setup\b"
    r"|\bsetup\s+(?:github|email|slack|google|apis|welcome|fallback|voice|images)\b",
    re.IGNORECASE,
)


class QuotaExhausted(RuntimeError):
    """Hack Club AI is unavailable and the fallback could not answer either."""

    def __init__(
        self,
        primary: str,
        fallback: str | None = None,
        *,
        kind: str = KIND_UNKNOWN,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.kind = kind or KIND_UNKNOWN
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
    r"summar(?:y|ize|ise)|tldr|recap|catch\s*up|remind|schedule|email|"
    r"calendar|github|pr|repo|sandbox|file|image|chart|deck|hackatime|"
    r"weather|docs?|link|upload|download|browser|api|emoji|export)\b",
    re.IGNORECASE,
)
# Small talk can skip tools. Anything that points at workspace data ("is 42253
# in #confessions yet") must not, or the fast model has no way to look it up and
# starts narrating a tool call instead.
SMALL_TALK_RE = re.compile(
    r"^(?:hi|hey|hello|yo|sup|gm|gn|good\s+(?:morning|night)|what'?s\s+up|"
    r"wassup|how\s+are\s+you|how'?s\s+it\s+going|who\s+are\s+you|"
    r"what\s+are\s+you|thanks|thank\s+you|ty|lol|lmao|nice|cool|ok|okay|"
    r"sure|yes|no|nah|yeah|yep|nope|same|fair|true|real|bet|word)\b",
    re.IGNORECASE,
)
DATA_HINT_RE = re.compile(
    r"#\w|\b(?:thread|channel|slack|confession|message|posted|search|find|"
    r"look\s*up|anyone|someone|did|does|has|have|yet|still|latest|recent|"
    r"number|id|tldr|recap|summary)\b|\d{3,}",
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
        safety_review: bool = False,
        client: AsyncWebClient | None = None,
        secret: Any | None = None,
        message_ts: str | None = None,
    ) -> str:
        """Run one agent turn (with tools) and persist both sides of it."""
        if not self.settings.ai_enabled:
            return "my ai brain isn't configured yet. add AI_API_KEY and restart me."
        trigger_text = text
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
        history = (
            secret.history(self.settings.ai_history_limit)
            if secret is not None
            else self.db.history(conversation, self.settings.ai_history_limit)
        )
        role = "owner" if user_id == self.settings.owner_user_id else "guest"
        location = await self._location(
            channel_id, is_dm, is_mpim=is_mpim, client=active_client
        )
        memory = (
            "none"
            if secret is not None
            else (
                get_user_memory_prompt(self.db, self.settings, user_id)
                if (role == "owner" or self.settings.is_admin(user_id))
                else "none"
            )
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
            .replace(
                "{is_moderator}",
                "yes" if self.settings.is_moderator(user_id) else "no",
            )
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
        if secret is not None:
            from bot.features.dm_chats import SECRET_SYSTEM

            messages.append({"role": "system", "content": SECRET_SYSTEM})
        messages.extend(fit_history(history, text, self.settings.ai_max_context_chars))
        if images:
            user_content: list[dict[str, Any]] = [
                {"type": "text", "text": text[: self.settings.ai_max_context_chars] or "Look at this image."}
            ]
            for img in images:
                user_content.append({"type": "image_url", "image_url": {"url": img}})
            messages.append({"role": "user", "content": user_content})
            stored_text = text if not images else f"{text} [image attached]".strip()
            if secret is not None:
                secret.add("user", user_id, stored_text)
            else:
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
            if secret is not None:
                secret.add("user", user_id, text)
            else:
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
            ephemeral=secret is not None,
            trigger_text=trigger_text[:6000],
            context_text=context[:6000],
            message_ts=message_ts,
        )

        answer = ""
        code_mode = bool(CODE_HINT_RE.search(text or ""))
        fast_chat = bool(
            is_user_identity
            and not code_mode
            and self._simple_chat(text, images=images)
        )
        extra_tools: frozenset[str] = frozenset()
        try:
            if self.settings.thread_summary_allowed(channel_id) is True:
                extra_tools = frozenset({"summarize_thread"})
        except Exception:  # noqa: BLE001 - a broken policy must fail closed
            extra_tools = frozenset()
        tool_names = self._visible_tool_names(
            is_owner=ctx.is_owner or ctx.is_admin,
            ephemeral=secret is not None,
            extra_tools=extra_tools,
        )
        used_fallback = False
        fallback_kind = ""
        seen_calls: dict[str, int] = {}
        rounds = 0
        stalled = False
        nudged = False
        try:
            while rounds < MAX_TOOL_ROUNDS:
                rounds += 1
                response, provider = await self._chat(
                    messages,
                    tools=not fast_chat,
                    user_id=user_id,
                    is_user_identity=is_user_identity,
                    code=code_mode,
                    fast=fast_chat,
                    is_owner=ctx.is_owner or ctx.is_admin,
                    ephemeral=secret is not None,
                    extra_tools=extra_tools,
                )
                if provider == "fallback" and not fallback_kind:
                    fallback_kind = self._current_fallback_kind()
                used_fallback = used_fallback or provider == "fallback"
                message = response.choices[0].message
                tool_calls = message.tool_calls or []
                content = (message.content or "").strip()

                if not tool_calls:
                    if looks_like_pseudo_tool_call(content, tool_names):
                        if nudged:
                            # It keeps narrating instead of acting. Drop the bad
                            # text and force a real answer below.
                            logger.info("model kept writing pseudo tool calls")
                            stalled = True
                            break
                        nudged = True
                        # It wanted a tool. The fast path has tools off, which is
                        # what makes models narrate calls in the first place.
                        fast_chat = False
                        messages.append({"role": "assistant", "content": content})
                        messages.append({"role": "user", "content": PSEUDO_TOOL_NUDGE})
                        continue
                    answer = content
                    break

                fast_chat = False
                signatures = [
                    tool_call_signature(call.function.name, call.function.arguments)
                    for call in tool_calls
                ]
                if any(
                    seen_calls.get(sig, 0) >= MAX_IDENTICAL_TOOL_CALLS
                    for sig in signatures
                ):
                    logger.info("tool loop: identical call repeated, forcing an answer")
                    stalled = True
                    break
                for sig in signatures:
                    seen_calls[sig] = seen_calls.get(sig, 0) + 1

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

            if stalled or not answer:
                answer = await self._final_answer(
                    messages,
                    user_id=user_id,
                    is_user_identity=is_user_identity,
                    code=code_mode,
                    fast=fast_chat,
                    is_owner=ctx.is_owner or ctx.is_admin,
                    ephemeral=secret is not None,
                    extra_tools=extra_tools,
                )
            if answer and looks_like_pseudo_tool_call(answer, tool_names):
                logger.info("dropped an answer that narrated a tool call")
                answer = ""
            if not answer and trigger not in {"ambient", "thread_followup"}:
                answer = "couldn't get a clean answer out of that. mind rephrasing?"
        except BudgetExceeded as exc:
            logger.warning("ai budget exhausted: %s", exc)
            answer = _budget_text(exc)
        except Exception as exc:
            logger.exception("agent completion failed")
            answer = self._friendly_error(
                exc, model=self._models_for(is_user_identity, code=code_mode)[0]
            )

        answer = format_mrkdwn(answer)
        if safety_review and answer and answer.strip() not in {"[NO_REPLY]", "NO_REPLY"}:
            gate = getattr(runtime, "gate", None)
            if gate is None:
                gate = Gate(self.settings, budget=self.budget)
            if not await gate.review_public(answer, user_id=user_id):
                logger.info("conduct veto: suppressed an unsafe public reply")
                return ""
        if public_veto and answer and PRIVATE_DETAIL_RE.search(answer):
            logger.info("public veto: suppressed answer with internal details")
            return ""
        if used_fallback and answer.strip() not in {"", "[NO_REPLY]", "NO_REPLY"}:
            answer = f"{answer}\n\n{self._fallback_footer(fallback_kind)}"

        if answer and answer.strip() not in {"[NO_REPLY]", "NO_REPLY"}:
            if secret is not None:
                secret.add("assistant", None, answer)
            else:
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
        if not answer and trigger in {
            "mention",
            "dm",
            "mpim_mention",
            "named",
            "reaction",
            "shortcut",
        }:
            return "hmm, i blanked. try again?"
        return ""

    def _visible_tool_names(
        self,
        *,
        is_owner: bool,
        ephemeral: bool,
        extra_tools: frozenset[str],
    ) -> frozenset[str]:
        """Names this caller can actually call, for spotting narrated calls."""
        try:
            definitions = self.toolbox.definitions_for(
                is_owner=is_owner,
                ephemeral=ephemeral,
                extra_tools=extra_tools,
            )
        except Exception:  # detection is best-effort
            logger.debug("could not list tool names", exc_info=True)
            return frozenset()
        return frozenset(
            name
            for item in definitions
            if (name := str((item.get("function") or {}).get("name") or ""))
        )

    async def _final_answer(
        self,
        messages: list[dict[str, Any]],
        *,
        user_id: str,
        is_user_identity: bool,
        code: bool,
        fast: bool,
        is_owner: bool,
        ephemeral: bool,
        extra_tools: frozenset[str],
    ) -> str:
        """Ask for a plain answer with tools off, so a loop can't swallow the turn."""
        final_messages = list(messages)
        final_messages.append({"role": "system", "content": FORCE_ANSWER_NUDGE})
        try:
            response, _ = await self._chat(
                final_messages,
                tools=False,
                user_id=user_id,
                is_user_identity=is_user_identity,
                code=code,
                fast=fast,
                is_owner=is_owner,
                ephemeral=ephemeral,
                extra_tools=extra_tools,
            )
        except (BudgetExceeded, QuotaExhausted):
            raise
        except Exception:  # the caller has a friendly fallback
            logger.warning("forced final answer failed", exc_info=True)
            return ""
        return (response.choices[0].message.content or "").strip()

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
                raw = f"{raw}\n\n{self._fallback_footer(self._current_fallback_kind())}"
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
        if "http://" in clean or "https://" in clean or "`" in clean:
            return False
        if DATA_HINT_RE.search(clean) and not SMALL_TALK_RE.search(clean):
            return False
        if CODE_HINT_RE.search(clean) or SIMPLE_CHAT_RE.search(clean):
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

    def _current_fallback_kind(self) -> str:
        _, kind = primary_status(self.db)
        return kind or KIND_UNKNOWN

    def _fallback_footer(self, kind: str) -> str:
        if kind == KIND_DAILY:
            return "_(hack club ai quota empty · fallback ai · resets 00:00 UTC)_"
        if kind == KIND_UPSTREAM:
            return "_(hack club ai upstream outage · fallback ai · auto-retrying)_"
        return "_(hack club ai unavailable · fallback ai)_"

    async def _notify_quota_once(
        self, provider: FallbackProvider, kind: str
    ) -> None:
        if not self.db:
            return
        key = notice_key(kind)
        if self.db.kv_get(key):
            return
        self.db.kv_set(key, "1")
        models = ", ".join(f"`{model}`" for model in provider.models)
        if kind == KIND_DAILY:
            text = (
                "hack club ai's daily budget is used up for today. "
                f"i'm on the fallback ({models}), so replies may be slower and "
                "tool use is off. it resets at 00:00 UTC. voice, images and memory "
                "search still need HCAI quota."
            )
        elif kind == KIND_UPSTREAM:
            text = (
                "hack club ai is having an upstream credits/billing problem — "
                "that's not your daily $3 quota. "
                f"i'm on the fallback ({models}) and will retry hacai automatically. "
                "voice, images and memory search still need HCAI quota."
            )
        else:
            text = (
                "hack club ai is unavailable right now. "
                f"i'm on the fallback ({models}) and will retry hacai automatically. "
                "voice, images and memory search still need HCAI quota."
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
        is_owner: bool = True,
        ephemeral: bool = False,
        extra_tools: frozenset[str] = frozenset(),
    ) -> tuple[Any, str]:
        models = self._models_for(is_user_identity, code=code, fast=fast)

        kwargs: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or self.settings.ai_max_tokens,
        }
        if tools:
            kwargs["tools"] = self.toolbox.definitions_for(
                is_owner=is_owner, ephemeral=ephemeral, extra_tools=extra_tools
            )
            kwargs["tool_choice"] = "auto"

        # Check the budget once per call, not once per candidate model.
        if self.budget is not None:
            status = self.budget.status()
            if status.exhausted:
                raise BudgetExceeded(status)
            if user_id and self.budget.user_exhausted(user_id):
                raise BudgetExceeded(self.budget.user_status(user_id), scope="user")

        fallback = self._fallback()
        blocked, block_kind = primary_status(self.db)
        quota_error: Exception | None = None
        quota_kind = block_kind
        providers: list[tuple[str, AsyncOpenAI, list[str]]] = []
        if not blocked:
            providers.append(("primary", self.llm, models))
        else:
            quota_error = RuntimeError(
                f"hack club ai unavailable ({block_kind or KIND_UNKNOWN})"
            )
        if fallback is not None:
            providers.append(("fallback", fallback[0], list(fallback[1].models)))
        if not providers:
            raise QuotaExhausted(
                "hack club ai is unavailable",
                kind=quota_kind or KIND_UNKNOWN,
            )

        last_error: Exception | None = None
        fallback_error: Exception | None = None
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
                            "The primary ai is unavailable. You are the fallback "
                            "model and have no tools. Answer briefly from the context "
                            "you already have. Do not claim to use tools."
                        ),
                    }
                ]
                provider_kwargs["messages"] = provider_messages

            for index, model in enumerate(provider_models):
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
                    if provider_name == "primary":
                        clear_cooldown(self.db)
                        return response, provider_name
                    if provider_name == "fallback" and fallback is not None:
                        await self._notify_quota_once(fallback[1], quota_kind)
                    return response, provider_name
                except BudgetExceeded:
                    raise
                except Exception as exc:  # noqa: BLE001 - provider errors are retried
                    last_error = exc
                    if provider_name == "fallback":
                        fallback_error = exc
                    kind = classify_quota_error(exc)
                    if provider_name == "primary" and kind:
                        quota_error = exc
                        quota_kind = kind
                        mark_unavailable(self.db, kind, str(exc))
                        logger.warning(
                            "primary ai unavailable (%s): %s",
                            kind,
                            str(exc)[:160],
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
                str(fallback_error)[:160] if fallback_error else None,
                kind=quota_kind or KIND_UNKNOWN,
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
            kind = getattr(exc, "kind", KIND_UNKNOWN)
            if kind == KIND_DAILY:
                if exc.fallback:
                    return (
                        "hack club ai quota is empty for today, and the fallback "
                        "also failed. voice, images and memory search need hacai "
                        "quota. it resets at 00:00 UTC."
                    )
                return (
                    "hack club ai quota is empty for today. add a fallback endpoint "
                    "with `/nugget setup fallback`, or wait until 00:00 UTC. voice, "
                    "images and memory search need hacai quota."
                )
            if kind == KIND_UPSTREAM:
                if exc.fallback:
                    return (
                        "hack club ai is down because its upstream credits are empty "
                        "— that's on hacai's side, not your daily quota — and the "
                        "fallback also failed. voice, images and memory search need "
                        "hacai quota. i'll retry hacai automatically."
                    )
                return (
                    "hack club ai is down because its upstream credits are empty "
                    "(not your daily quota). add a fallback endpoint with "
                    "`/nugget setup fallback`; i'll retry hacai automatically. "
                    "voice, images and memory search need hacai quota."
                )
            if exc.fallback:
                return (
                    "hack club ai is unavailable right now, and the fallback also "
                    "failed. i'll retry hacai automatically. voice, images and "
                    "memory search need hacai quota."
                )
            return (
                "hack club ai is unavailable right now. add a fallback endpoint with "
                "`/nugget setup fallback`; i'll retry hacai automatically. voice, "
                "images and memory search need hacai quota."
            )
        if (
            "daily spending limit" in lower
            or "daily budget" in lower
            or "spending limit" in lower
        ):
            return "my hack club ai daily budget is used up. it resets at 00:00 UTC."
        if "402" in text or "payment required" in lower:
            return (
                "hack club ai is having a billing/credit problem upstream. "
                "voice, images and memory search need hacai quota. try again in a bit."
            )
        if "budget" in lower:
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


__all__ = [
    "Agent",
    "fit_history",
    "looks_like_pseudo_tool_call",
    "owner_label",
    "sync_prompt_digest",
    "tool_call_signature",
    "tzinfo",
]

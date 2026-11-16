"""Thread follow-up rules shared by the bot app and boi.

Both identities answer an @mention right away, but a thread they already joined
should keep flowing without a fresh ping on every message. The two gateways are
fed by different Slack transports, so the decision lives here once instead of
drifting apart in two places.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from bot.slack_api import share_text

logger = logging.getLogger(__name__)

# "why / how / what / huh" style questions and "i don't get it" nudges.
FOLLOWUP_RE = re.compile(
    r"\b(?:you|u|why|how|what|explain|huh|really|serious|confused)\b|don'?t get",
    re.IGNORECASE,
)
# Things that ask for work rather than just narrating.
REQUEST_RE = re.compile(
    r"\b(?:can|could|would|will)\s+you\b|"
    r"\b(?:help|check|look at|fix|review|explain|thoughts|opinion|advice|debug)\b",
    re.IGNORECASE,
)

_SKIP_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "bot_message",
}

# "boi" is the user account identity; nobody means the bot app when they say it.
BOI_RE = re.compile(r"\bboi\b", re.IGNORECASE)

NameOf = Callable[[str], Awaitable[str]]


async def conversation_lines(
    client,
    channel: str,
    *,
    thread_ts: str | None = None,
    current_ts: str | None = None,
    agent_user_id: str = "",
    limit: int = 20,
    keep: int = 8,
    name_of: NameOf | None = None,
) -> tuple[list[str], int, bool]:
    """Read a thread (or recent channel history) for the model's context.

    Returns the oldest-to-newest lines, how many humans spoke, and whether the
    calling identity spoke last. `name_of` resolves display names; without it
    people are written as `<@U123>` mentions, which the model reads fine.
    """
    if not client or not channel:
        return [], 0, False
    call = client.conversations_replies if thread_ts else client.conversations_history
    kwargs: dict[str, Any] = {"channel": channel, "limit": max(1, limit)}
    if thread_ts:
        kwargs["ts"] = thread_ts
    try:
        response = await call(**kwargs)
    except Exception as exc:  # noqa: BLE001 - context is best effort
        logger.debug("could not read lines for %s: %s", channel, exc)
        return [], 0, False
    payload = response.get("messages") if hasattr(response, "get") else None
    if inspect.isawaitable(payload):
        payload = await payload
    messages = (
        [message for message in payload if isinstance(message, dict)]
        if isinstance(payload, list)
        else []
    )
    # Replies arrive oldest-first, channel history newest-first.
    ordered = messages if thread_ts else list(reversed(messages))

    if name_of is not None:
        users = {
            message.get("user")
            for message in ordered
            if message.get("user") and message.get("user") != agent_user_id
        }
        if users:
            await asyncio.gather(*(name_of(uid) for uid in users))

    lines: list[str] = []
    humans: set[str] = set()
    agent_last = False
    for message in ordered:
        if current_ts and message.get("ts") == current_ts:
            continue
        if message.get("subtype") in _SKIP_SUBTYPES:
            continue
        text = " ".join(
            f"{message.get('text') or ''} {share_text(message)}".split()
        )
        if not text:
            continue
        user_id = message.get("user") or ""
        if user_id and user_id == agent_user_id:
            lines.append(f"you: {text[:200]}")
        elif user_id and name_of is not None:
            lines.append(f"{await name_of(user_id)}: {text[:200]}")
        elif user_id:
            lines.append(f"<@{user_id}>: {text[:200]}")
        else:
            lines.append(text[:200])
        if user_id and user_id != agent_user_id:
            humans.add(user_id)
        agent_last = bool(user_id) and user_id == agent_user_id
    return lines[-keep:], len(humans), agent_last


def is_direct_followup(*, text: str, bot_last: bool) -> bool:
    """True when this reads as an answer to something we just said."""
    return bool(bot_last and ("?" in text or REQUEST_RE.search(text) or FOLLOWUP_RE.search(text)))


def looks_like_followup(
    *,
    text: str,
    bot_last: bool,
    humans: int = 0,
    has_images: bool = False,
    named: bool = False,
) -> bool:
    """Would an unmentioned thread message count as addressed to us?

    A question or request right after we spoke always counts. Otherwise the
    message has to want something instead of just narrating, and a thread with
    several humans talking to each other stays private.
    """
    if named:
        return True
    question = "?" in text
    if is_direct_followup(text=text, bot_last=bot_last):
        return True
    if humans >= 2 and not question and not has_images:
        return False
    if not has_images and len(text.split()) < 3 and not question:
        return False
    # Past this point it has to want something rather than narrate.
    return bool(has_images or question or REQUEST_RE.search(text))


__all__ = [
    "BOI_RE",
    "FOLLOWUP_RE",
    "REQUEST_RE",
    "conversation_lines",
    "is_direct_followup",
    "looks_like_followup",
]

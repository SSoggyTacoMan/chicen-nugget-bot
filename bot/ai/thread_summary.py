"""Read and summarise long Slack threads.

Slack's ``conversations.replies`` hands back one page at a time, and a thread
with a thousand replies is far past what a single prompt can hold. This module
walks every page, then either returns a small transcript as-is or compresses it
slice by slice and reduces those notes into one TLDR.

The tool, the message shortcut and the 🧵 reaction all funnel through here, so
"tldr" means the same thing no matter how it was asked.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from slack_sdk.errors import SlackApiError

from bot.runtime import runtime
from bot.slack_api import share_text
from bot.utils.tz import tzinfo

logger = logging.getLogger(__name__)

PAGE_SIZE = 200
MAX_PAGES = 60
MAX_MESSAGE_CHARS = 500
SMALL_THREAD_CHARS = 6000
MAX_CHUNKS = 18
MAP_TOKENS = 450
REDUCE_TOKENS = 900
MAP_CONCURRENCY = 3
MAX_TOOL_TRANSCRIPT = 6000
THIN_MESSAGES = 400

_SKIP_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "bot_message",
}

_MAP_SYSTEM = (
    "you are compressing one slice of a long Slack thread. "
    "keep decisions, who agreed to what, links, numbers, and open questions. "
    "drop greetings, reactions, jokes and repetition. write terse notes, one per line, "
    "not prose. no preamble, no headings."
)

_REDUCE_SYSTEM = (
    "you are writing a TLDR of a long Slack thread for someone who was not in it. "
    "format: one sentence gist, then 3-6 bullets of what actually happened or was decided, "
    "then 'open' for unresolved questions, then 'links' when there are any. "
    "no chronological recap, no filler, no preamble. under 220 words. slack mrkdwn."
)


@dataclass
class ThreadDigest:
    channel: str
    ts: str
    messages: int
    participants: int
    first_ts: str | None
    last_ts: str | None
    truncated: bool
    transcript: str = ""
    summary: str = ""
    permalink: str | None = None


def _ts_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clock(value: Any) -> str | None:
    try:
        moment = datetime.fromtimestamp(float(value), tzinfo())
    except (TypeError, ValueError, OSError):
        return None
    return moment.strftime("%d %b %H:%M")


async def _replies_page(client, channel: str, ts: str, cursor: str | None) -> Mapping[str, Any]:
    kwargs: dict[str, Any] = {"channel": channel, "ts": ts, "limit": PAGE_SIZE}
    if cursor:
        kwargs["cursor"] = cursor
    for attempt in range(2):
        try:
            response = await client.conversations_replies(**kwargs)
            break
        except SlackApiError as exc:
            error = (getattr(exc, "response", None) or {}).get("error") or ""
            if error != "ratelimited" or attempt:
                raise
            retry_after = 1.0
            headers = getattr(getattr(exc, "response", None), "headers", None) or {}
            try:
                retry_after = min(10.0, float(headers.get("retry-after") or 1))
            except (TypeError, ValueError):
                retry_after = 1.0
            logger.info("thread read rate limited in %s, retrying in %ss", channel, retry_after)
            await asyncio.sleep(retry_after)
    return response if hasattr(response, "get") else {}


async def fetch_thread(
    client,
    channel: str,
    ts: str,
    *,
    max_messages: int = 2000,
) -> tuple[list[Mapping[str, Any]], bool]:
    """Every reply in a thread, oldest first.

    Returns the messages and whether we stopped early. Slack caps a page at 200
    replies and signals more with ``has_more`` plus a cursor; ignoring that is
    how a 1,000-reply thread silently turned into "the first 100 messages".
    """
    if not client or not channel or not ts:
        raise ValueError("no slack client, channel or thread timestamp")
    collected: list[Mapping[str, Any]] = []
    cursor: str | None = None
    truncated = False
    for _ in range(MAX_PAGES):
        response = await _replies_page(client, channel, ts, cursor)
        batch = response.get("messages") or []
        collected.extend(m for m in batch if isinstance(m, Mapping))
        if len(collected) > max_messages:
            # Keep the newest replies: the end of a long thread is where the
            # decisions and the "so what happened" usually are.
            collected = collected[-max_messages:]
            truncated = True
        cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
        if not response.get("has_more") or not cursor:
            break
    else:
        truncated = True

    seen: set[str] = set()
    ordered: list[Mapping[str, Any]] = []
    for message in collected:
        key = str(message.get("ts") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        ordered.append(message)
    ordered.sort(key=lambda message: _ts_float(message.get("ts")))
    return ordered, truncated


def _clean(message: Mapping[str, Any], *, chars: int) -> str:
    text = " ".join(f"{message.get('text') or ''} {share_text(message)}".split())
    if len(text) > chars:
        text = text[: chars - 1].rstrip() + "…"
    return text


def render_lines(
    messages: list[Mapping[str, Any]],
    *,
    chars: int = MAX_MESSAGE_CHARS,
) -> tuple[list[str], int, str | None, str | None]:
    """One readable line per message, with its local date and time."""
    lines: list[str] = []
    participants: set[str] = set()
    first_ts = last_ts = None
    for message in messages:
        if message.get("subtype") in _SKIP_SUBTYPES:
            continue
        text = _clean(message, chars=chars)
        if not text:
            continue
        stamp = _clock(message.get("ts"))
        if stamp:
            when = stamp
            if not first_ts:
                first_ts = str(message.get("ts"))
            last_ts = str(message.get("ts"))
        else:
            when = "?"
        user = message.get("user") or message.get("bot_id") or ""
        if not user:
            author = "someone"
        elif message.get("user"):
            author = f"<@{user}>"
            participants.add(str(user))
        else:
            author = "a bot"
        lines.append(f"{author} ({when}): {text}")
    return lines, len(participants), first_ts, last_ts


def _chunks(lines: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _thin_lines(lines: list[str], limit: int) -> tuple[list[str], bool]:
    """Keep the start and the end when a thread is bigger than the budget."""
    if sum(len(line) + 1 for line in lines) <= limit:
        return lines, False
    budget = limit - 200
    head_budget = int(budget * 0.55)
    tail_budget = budget - head_budget
    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head):]):
        if used + len(line) + 1 > tail_budget:
            break
        tail.append(line)
        used += len(line) + 1
    tail.reverse()
    omitted = len(lines) - len(head) - len(tail)
    marker = f"… [{omitted} messages omitted from the middle to fit …]"
    return head + [marker] + tail, True


def _span(first_ts: str | None, last_ts: str | None) -> str:
    start, end = _ts_float(first_ts), _ts_float(last_ts)
    if not start or not end or end < start:
        return ""
    seconds = int(end - start)
    if seconds < 90:
        return "under a minute"
    if seconds < 3600:
        return f"{seconds // 60}m"
    days, rest = divmod(seconds, 86_400)
    hours = rest // 3600
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    return " ".join(parts)


async def thread_permalink(client, channel: str, ts: str) -> str | None:
    if not client or not channel or not ts:
        return None
    try:
        response = await client.chat_getPermalink(channel=channel, message_ts=ts)
        link = response.get("permalink") if hasattr(response, "get") else None
        return str(link) if link else None
    except Exception as exc:  # noqa: BLE001 - a link is nice, not required
        logger.debug("permalink failed for %s/%s: %s", channel, ts, exc)
        return None


async def _summarise_lines(
    channel: str,
    lines: list[str],
    *,
    focus: str,
    user_id: str | None,
    chunk_chars: int,
) -> str:
    agent = runtime.agent
    if agent is None:
        return ""
    chunks = _chunks(lines, max(2000, chunk_chars))
    focus_line = f"focus: {focus.strip()[:300]}\n" if focus.strip() else ""
    notes: list[str] = []
    if len(chunks) == 1:
        notes = chunks
    else:
        semaphore = asyncio.Semaphore(MAP_CONCURRENCY)

        async def one(index: int, chunk: str) -> str:
            async with semaphore:
                return await agent.complete(
                    system=_MAP_SYSTEM,
                    text=(
                        f"thread <#{channel}> · slice {index + 1}/{len(chunks)}\n"
                        f"{focus_line}\n{chunk}"
                    ),
                    max_tokens=MAP_TOKENS,
                    user_id=user_id,
                )

        results = await asyncio.gather(
            *(one(index, chunk) for index, chunk in enumerate(chunks)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning("thread slice summary failed: %s", result)
                continue
            text = (result or "").strip()
            if not text or text.startswith("my ai brain"):
                continue
            if "ai budget" in text or "today's ai limit" in text:
                continue
            notes.append(text)
        if not notes:
            return ""

    body = "\n\n---\n\n".join(notes)
    summary = await agent.complete(
        system=_REDUCE_SYSTEM,
        text=(
            f"thread <#{channel}> · {len(lines)} readable messages"
            f"{' · ' + focus_line.strip() if focus_line else ''}\n\n{body}"
        ),
        max_tokens=REDUCE_TOKENS,
        user_id=user_id,
    )
    return summary.strip()


async def summarize_thread(
    client,
    channel: str,
    ts: str,
    *,
    focus: str = "",
    max_messages: int = 2000,
    user_id: str | None = None,
    include_permalink: bool = True,
) -> ThreadDigest:
    """Fetch a whole thread and either return its transcript or a TLDR."""
    messages, truncated = await fetch_thread(
        client, channel, ts, max_messages=max_messages
    )
    chars = 200 if len(messages) > THIN_MESSAGES else MAX_MESSAGE_CHARS
    lines, participants, first_ts, last_ts = render_lines(messages, chars=chars)
    link = await thread_permalink(client, channel, ts) if include_permalink else None
    digest = ThreadDigest(
        channel=channel,
        ts=ts,
        messages=len(lines),
        participants=participants,
        first_ts=first_ts,
        last_ts=last_ts,
        truncated=truncated,
        permalink=link,
    )
    if not lines:
        return digest

    transcript = "\n".join(lines)
    budget = max(4000, int(getattr(runtime.settings, "thread_summary_chunk_chars", 14000) or 14000))
    transcript, thinned = _thin_lines(transcript.split("\n"), budget * MAX_CHUNKS)
    digest.truncated = digest.truncated or thinned
    digest.transcript = "\n".join(transcript)

    if len(digest.transcript) <= SMALL_THREAD_CHARS:
        return digest

    if not getattr(runtime.settings, "ai_enabled", False):
        return digest

    summary = await _summarise_lines(
        channel,
        transcript,
        focus=focus,
        user_id=user_id,
        chunk_chars=budget,
    )
    digest.summary = summary[:8000]
    return digest


def format_digest(digest: ThreadDigest) -> str:
    """The text the model sees for a thread read."""
    meta = [f"thread in <#{digest.channel}>"]
    if digest.messages:
        meta.append(f"{digest.messages:,} messages")
    if digest.participants:
        meta.append(f"{digest.participants} people")
    span = _span(digest.first_ts, digest.last_ts)
    if span:
        meta.append(span)
    header = " · ".join(meta)
    if digest.truncated:
        header += " · truncated"
    if digest.permalink:
        header += f"\npermalink: {digest.permalink}"
    body = digest.summary or digest.transcript
    if len(body) > MAX_TOOL_TRANSCRIPT and not digest.summary:
        body = body[:MAX_TOOL_TRANSCRIPT] + "\n… transcript truncated"
    return f"{header}\n\n{body}" if body else header


__all__ = [
    "ThreadDigest",
    "fetch_thread",
    "format_digest",
    "render_lines",
    "summarize_thread",
    "thread_permalink",
]

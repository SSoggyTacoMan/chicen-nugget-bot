"""Read and summarise long Slack threads.

Slack's ``conversations.replies`` hands back one page at a time, and a thread
with a thousand replies is far past what a single prompt can hold. This module
walks every page, then either returns a small transcript as-is or compresses it
slice by slice and reduces those notes into one TLDR.

A specific question ("who has the domain renewal code?") runs through the same
pipeline with two additions: a keyword pass over every reply before anything is
thinned, and question-aware slice prompts. The TLDR and the answer come back
together.

The tool, the message shortcut and the 🧵 reaction all funnel through here, so
"tldr" means the same thing no matter how it was asked.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
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
HIT_CHARS = 600
MAX_HITS = 5
NOTE_CHARS = 900
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

_MAP_SYSTEM_QUESTION = (
    "you are scanning one slice of a long Slack thread for one specific question. "
    "if this slice contains anything that answers it, quote the exact words and say who "
    "said it and when. also note decisions, links and numbers nearby. "
    "if nothing in the slice answers it, reply with exactly: NOTHING. "
    "no preamble, no prose."
)

_REDUCE_SYSTEM = (
    "you are writing a TLDR of a long Slack thread for someone who was not in it. "
    "format: one sentence gist, then 3-6 bullets of what actually happened or was decided, "
    "then 'open' for unresolved questions, then 'links' when there are any. "
    "no chronological recap, no filler, no preamble. under 220 words. slack mrkdwn."
)

_REDUCE_SYSTEM_QUESTION = (
    "you are answering one question about a long Slack thread, for someone who was not in it. "
    "start with the direct answer and quote the exact line with the author and time. "
    "if the notes and direct hits do not answer it, say you could not find it and say what "
    "the thread does cover instead. then add at most two bullets of context. "
    "no filler, no preamble. under 150 words. slack mrkdwn."
)

_QUESTION_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "an",
    "and",
    "any",
    "anybody",
    "anyone",
    "anything",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "done",
    "down",
    "ever",
    "find",
    "for",
    "from",
    "get",
    "gets",
    "got",
    "had",
    "has",
    "have",
    "he",
    "her",
    "here",
    "hers",
    "him",
    "his",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "know",
    "knows",
    "like",
    "look",
    "looking",
    "may",
    "me",
    "mention",
    "mentioned",
    "mentions",
    "might",
    "mine",
    "must",
    "my",
    "no",
    "not",
    "of",
    "off",
    "ok",
    "okay",
    "on",
    "or",
    "our",
    "out",
    "over",
    "please",
    "said",
    "say",
    "says",
    "shall",
    "she",
    "should",
    "so",
    "some",
    "somebody",
    "someone",
    "something",
    "still",
    "tell",
    "than",
    "thank",
    "thanks",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "to",
    "too",
    "under",
    "up",
    "us",
    "very",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "without",
    "would",
    "yes",
    "you",
    "your",
    "yours",
}

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'_-]{2,}")
_ARCHIVE_RE = re.compile(r"^(.*/archives/[^/]+/)p[0-9]+")


@dataclass(frozen=True)
class ThreadRecord:
    ts: str
    user: str
    author: str
    when: str
    text: str


@dataclass
class AnswerHit:
    ts: str
    user: str
    author: str
    when: str
    text: str
    score: int
    link: str | None = None
    via: str = "keyword"


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
    question: str = ""
    hits: list[AnswerHit] | None = None


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
    When the cap is hit, the newest replies are the ones kept.
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


def _clip(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[: chars - 1].rstrip() + "…"


def build_records(
    messages: list[Mapping[str, Any]],
    *,
    chars: int = MAX_MESSAGE_CHARS,
) -> list[ThreadRecord]:
    """Readable records for the messages the model should see."""
    records: list[ThreadRecord] = []
    for message in messages:
        if message.get("subtype") in _SKIP_SUBTYPES:
            continue
        text = _clean(message, chars=chars)
        if not text:
            continue
        stamp = _clock(message.get("ts")) or "?"
        user = str(message.get("user") or "")
        if user:
            author = f"<@{user}>"
        elif message.get("bot_id"):
            author = "a bot"
        else:
            author = "someone"
        records.append(
            ThreadRecord(
                ts=str(message.get("ts") or ""),
                user=user,
                author=author,
                when=stamp,
                text=text,
            )
        )
    return records


def render_lines(
    messages: list[Mapping[str, Any]],
    *,
    chars: int = MAX_MESSAGE_CHARS,
) -> tuple[list[str], int, str | None, str | None]:
    """One readable line per message, with its local date and time."""
    records = build_records(messages, chars=chars)
    lines = [f"{record.author} ({record.when}): {record.text}" for record in records]
    participants = len({record.user for record in records if record.user})
    first_ts = records[0].ts if records else None
    last_ts = records[-1].ts if records else None
    return lines, participants, first_ts, last_ts


def question_keywords(question: str) -> list[str]:
    text = (question or "").lower().replace("\u2019", "'")
    text = re.sub(r"n't\b", "", text)
    text = re.sub(r"'s\b", "", text)
    text = re.sub(r"'re\b|'ve\b|'ll\b|'d\b|'m\b", "", text)
    keywords: list[str] = []
    for word in _WORD_RE.findall(text):
        if word in _QUESTION_STOPWORDS or word in keywords:
            continue
        keywords.append(word)
    return keywords[:10]


def find_hits(
    records: list[ThreadRecord],
    question: str,
    *,
    permalink: str | None = None,
    channel: str = "",
    root_ts: str = "",
) -> list[AnswerHit]:
    """Keyword pass over every reply, before the transcript is thinned.

    This is the deterministic half of the answer: if one reply says "the code
    is 4417", that line is kept verbatim even when the middle of the thread is
    dropped to fit the prompt. Rare words count for more, so "renewal" outranks
    every reply that merely says "code".
    """
    keywords = question_keywords(question)
    if not keywords or not records:
        return []
    bodies = [record.text.lower() for record in records]
    frequency = {
        word: sum(1 for body in bodies if word in body) for word in keywords
    }
    scored: list[tuple[float, ThreadRecord]] = []
    for record, body in zip(records, bodies):
        matched = [word for word in keywords if word in body]
        if not matched:
            continue
        score = sum(1.0 / max(1, frequency[word]) for word in matched) * 1000
        scored.append((score + len(matched), record))
    scored.sort(key=lambda item: item[0], reverse=True)
    hits: list[AnswerHit] = []
    seen: set[str] = set()
    for score, record in scored:
        key = record.text[:120]
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            AnswerHit(
                ts=record.ts,
                user=record.user,
                author=record.author,
                when=record.when,
                text=record.text,
                score=int(score),
                link=_hit_link(permalink, record.ts, channel, root_ts),
            )
        )
        if len(hits) >= MAX_HITS:
            break
    return hits


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = left_norm = right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if not left_norm or not right_norm:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


async def semantic_hits(
    records: list[ThreadRecord],
    question: str,
    *,
    permalink: str | None = None,
    channel: str = "",
    root_ts: str = "",
    top_k: int = 3,
    min_score: float = 0.30,
) -> list[AnswerHit]:
    """Embedding pass, for when the answer shares no words with the question.

    "who handles billing?" vs "priya owns the stripe invoices" has no lexical
    overlap, but the vectors are close. One batched embedding pass over the
    replies (capped by THREAD_SUMMARY_MAX_MESSAGES) and one for the question.
    Any failure falls back to the keyword pass and the question-aware slices.
    """
    agent = runtime.agent
    memory = getattr(agent, "memory", None) if agent is not None else None
    if memory is None or getattr(memory, "enabled", False) is not True:
        return []
    question = (question or "").strip()
    if not question or not records:
        return []
    try:
        question_vector, vectors = await asyncio.gather(
            memory.embed(question),
            memory.embed_many([record.text for record in records]),
        )
    except Exception:
        logger.debug("semantic thread retrieval failed", exc_info=True)
        return []
    if not question_vector:
        return []
    scored: list[tuple[float, ThreadRecord]] = []
    for record, vector in zip(records, vectors):
        if not vector:
            continue
        score = _cosine(question_vector, vector)
        if score >= min_score:
            scored.append((score, record))
    scored.sort(key=lambda item: item[0], reverse=True)
    hits: list[AnswerHit] = []
    for score, record in scored[:top_k]:
        hits.append(
            AnswerHit(
                ts=record.ts,
                user=record.user,
                author=record.author,
                when=record.when,
                text=record.text,
                score=int(score * 1000),
                link=_hit_link(permalink, record.ts, channel, root_ts),
                via="semantic",
            )
        )
    return hits


def merge_hits(
    lexical: list[AnswerHit],
    semantic: list[AnswerHit],
    *,
    limit: int = MAX_HITS,
) -> list[AnswerHit]:
    """Exact matches first, then meaning matches, one hit per reply."""
    merged: list[AnswerHit] = []
    seen: set[str] = set()
    for hit in [*lexical, *semantic]:
        key = hit.ts or hit.text[:80]
        if key in seen:
            continue
        seen.add(key)
        merged.append(hit)
        if len(merged) >= limit:
            break
    return merged


def _hit_link(
    permalink: str | None,
    ts: str,
    channel: str,
    root_ts: str,
) -> str | None:
    """A reply permalink, derived from the root one Slack already gave us.

    Slack permalinks are ``/archives/<channel>/p<ts without the dot>``, so a
    thread read costs one ``chat.getPermalink`` call instead of one per hit.
    """
    if not permalink or not ts:
        return None
    match = _ARCHIVE_RE.match(permalink)
    if not match:
        return None
    link = f"{match.group(1)}p{ts.replace('.', '')}"
    if root_ts and channel:
        link += f"?thread_ts={root_ts}&cid={channel}"
    return link


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


def _hit_block(hits: list[AnswerHit]) -> str:
    lines = []
    for hit in hits:
        line = f"- {hit.author} ({hit.when}): {_clip(hit.text, 400)}"
        if hit.link:
            line += f" <{hit.link}|link>"
        lines.append(line)
    return "\n".join(lines)


async def _summarise_lines(
    channel: str,
    lines: list[str],
    *,
    focus: str,
    question: str,
    hits: list[AnswerHit],
    user_id: str | None,
    chunk_chars: int,
) -> str:
    agent = runtime.agent
    if agent is None:
        return ""
    chunks = _chunks(lines, max(2000, chunk_chars))
    focus_line = f"focus: {focus.strip()[:300]}\n" if focus.strip() else ""
    question = question.strip()[:500]
    headers: list[str] = []
    if focus_line:
        headers.append(focus_line.strip())
    if question:
        headers.append(f"question: {question}")
    header = "\n".join(headers)
    notes: list[str] = []
    if len(chunks) == 1:
        notes = chunks
    else:
        semaphore = asyncio.Semaphore(MAP_CONCURRENCY)

        async def one(index: int, chunk: str) -> str:
            async with semaphore:
                parts = [
                    f"thread <#{channel}> · slice {index + 1}/{len(chunks)}",
                ]
                if header:
                    parts.append(header)
                parts.append(chunk)
                return await agent.complete(
                    system=_MAP_SYSTEM_QUESTION if question else _MAP_SYSTEM,
                    text="\n".join(parts),
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
            if question and text.rstrip(" .").upper() == "NOTHING":
                continue
            notes.append(_clip(text, NOTE_CHARS))
        if not notes and not hits:
            return ""

    body = "\n\n---\n\n".join(notes)
    if question:
        prompt = [
            f"thread <#{channel}> · {len(lines)} readable messages",
            f"question: {question}",
            "",
            "direct hits:",
            _hit_block(hits) or "(no keyword hits)",
            "",
            "notes:",
            body or "(no slice answered this question)",
        ]
        system = _REDUCE_SYSTEM_QUESTION
    else:
        suffix = f" · {focus_line.strip()}" if focus_line else ""
        prompt = [
            f"thread <#{channel}> · {len(lines)} readable messages{suffix}",
            "",
            body,
        ]
        system = _REDUCE_SYSTEM
    summary = await agent.complete(
        system=system,
        text="\n".join(prompt),
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
    question: str = "",
    max_messages: int = 2000,
    user_id: str | None = None,
    include_permalink: bool = True,
) -> ThreadDigest:
    """Fetch a whole thread and either return its transcript or a TLDR."""
    messages, truncated = await fetch_thread(
        client, channel, ts, max_messages=max_messages
    )
    line_chars = 200 if len(messages) > THIN_MESSAGES else MAX_MESSAGE_CHARS
    records = build_records(messages, chars=HIT_CHARS)
    lines = [
        f"{record.author} ({record.when}): {_clip(record.text, line_chars)}"
        for record in records
    ]
    participants = len({record.user for record in records if record.user})
    first_ts = records[0].ts if records else None
    last_ts = records[-1].ts if records else None
    link = await thread_permalink(client, channel, ts) if include_permalink else None
    question = (question or "").strip()[:500]
    lexical = (
        find_hits(records, question, permalink=link, channel=channel, root_ts=ts)
        if question
        else []
    )
    digest = ThreadDigest(
        channel=channel,
        ts=ts,
        messages=len(lines),
        participants=participants,
        first_ts=first_ts,
        last_ts=last_ts,
        truncated=truncated,
        permalink=link,
        question=question,
        hits=lexical,
    )
    if not lines:
        return digest

    transcript = "\n".join(lines)
    budget = max(4000, int(getattr(runtime.settings, "thread_summary_chunk_chars", 14000) or 14000))
    transcript, thinned = _thin_lines(transcript.split("\n"), budget * MAX_CHUNKS)
    digest.truncated = digest.truncated or thinned
    digest.transcript = "\n".join(transcript)

    if (
        question
        and len(digest.transcript) > SMALL_THREAD_CHARS
        and getattr(runtime.settings, "ai_enabled", False)
    ):
        # Only worth embedding when the transcript is too big to hand over
        # whole. Small threads already reach the model intact.
        semantic = await semantic_hits(
            records,
            question,
            permalink=link,
            channel=channel,
            root_ts=ts,
        )
        digest.hits = merge_hits(lexical, semantic)

    if len(digest.transcript) <= SMALL_THREAD_CHARS:
        return digest

    if not getattr(runtime.settings, "ai_enabled", False):
        return digest

    summary = await _summarise_lines(
        channel,
        transcript,
        focus=focus,
        question=question,
        hits=digest.hits or [],
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
    parts = [header]
    if digest.permalink:
        parts.append(f"permalink: {digest.permalink}")
    if digest.question:
        parts.append(f"question: {digest.question}")
    if digest.hits:
        parts.append("direct hits:\n" + _hit_block(digest.hits))
    body = digest.summary or digest.transcript
    if len(body) > MAX_TOOL_TRANSCRIPT and not digest.summary:
        body = body[:MAX_TOOL_TRANSCRIPT] + "\n… transcript truncated"
    if body:
        parts.append(body)
    return "\n\n".join(parts)


__all__ = [
    "AnswerHit",
    "ThreadDigest",
    "ThreadRecord",
    "build_records",
    "fetch_thread",
    "find_hits",
    "format_digest",
    "merge_hits",
    "question_keywords",
    "render_lines",
    "semantic_hits",
    "summarize_thread",
    "thread_permalink",
]

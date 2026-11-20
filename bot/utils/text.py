from __future__ import annotations

import re

MAX_SLACK_TEXT = 3900


def chunk_text(text: str, limit: int = MAX_SLACK_TEXT) -> list[str]:
    """Split a reply into Slack-sized messages without cutting words if possible."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def chat_chunks(
    text: str,
    *,
    max_fragments: int = 4,
    limit: int = MAX_SLACK_TEXT,
) -> list[str]:
    """Split a reply into human-sized chat fragments.

    Paragraphs become separate messages, but code fences are kept together.
    Long paragraphs still go through chunk_text, so nothing is dropped.
    """
    text = (text or "").strip()
    if not text:
        return []
    max_fragments = max(1, int(max_fragments))
    paragraphs: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        if not stripped and not in_fence:
            if current:
                paragraphs.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        paragraphs.append("\n".join(current).strip())
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return []
    if len(paragraphs) > max_fragments:
        head = paragraphs[: max_fragments - 1]
        tail = "\n\n".join(paragraphs[max_fragments - 1 :])
        paragraphs = head + [tail]
    chunks: list[str] = []
    for paragraph in paragraphs:
        chunks.extend(chunk_text(paragraph, limit))
    return chunks


def strip_mentions(text: str) -> str:
    return re.sub(r"<@[^>]+>", "", text or "").strip()


def incoming_text(
    text: str,
    *,
    bot_user_id: str = "",
    assistant_user_id: str = "",
    bot_name: str = "nugget",
    assistant_name: str = "boi",
) -> str:
    """Keep mentions readable instead of deleting them.

    Slack sends mentions as ``<@U123>``. Stripping every one of them turns
    "i have @boi and @bot" into "i have and", which makes the model think the
    message was cut off. Replace our own identities with names, leave other
    mentions intact so the model can resolve them with lookup_user.
    """
    cleaned = text or ""
    if bot_user_id:
        cleaned = re.sub(
            rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>", f"@{bot_name}", cleaned
        )
    if assistant_user_id:
        cleaned = re.sub(
            rf"<@{re.escape(assistant_user_id)}(?:\|[^>]+)?>",
            f"@{assistant_name}",
            cleaned,
        )
    return cleaned.strip()


_SILENCE_RE = re.compile(
    r"(?:^|[.!?,]\s*|\b(?:please|pls|plz)\s+)"
    r"(?:do\s+not|don't|dont)\s+(?:respond|reply|answer)\b"
    r"|\bno\s+response\b"
    r"|\b(?:shut\s+up|stop\s+talking|quit\s+talking|be\s+quiet|can\s+you\s+not)\b"
    r"|\bnobody\s+asked\s+(?:you|u)\b"
    r"|\b(?:stop|quit)\s+(?:replying|responding)\b"
    r"|\b(?:don't|dont)\s+(?:talk|reply)\s+to\s+me\b"
    r"|\bwhy\s+(?:are|r)\s+(?:you|u)\s+(?:replying|responding|talking)\b",
    re.IGNORECASE,
)

# The code of conduct says "if someone asks you to leave them alone, you must
# leave them alone". These phrasings are standing requests, not one-turn ones,
# so the caller records a durable opt-out instead of just skipping this reply.
_STANDING_SILENCE_RE = re.compile(
    r"\bleave\s+me\s+(?:alone|be)\b"
    r"|\b(?:stop|quit)\s+(?:messaging|dm(?:ing)?|pming|contacting|bothering)\s+me\b"
    r"|\b(?:stop|quit)\s+(?:replying|responding)\s+to\s+me\b"
    r"|\b(?:don'?t|do\s+not)\s+(?:ever\s+)?(?:talk|speak|message|dm|reply|respond)\s+to\s+me\b"
    r"|\b(?:i\s+don'?t|i\s+do\s+not)\s+want\s+(?:you\s+)?to\s+(?:talk|reply|respond|message)\b",
    re.IGNORECASE,
)

_COME_BACK_RE = re.compile(
    r"\b(?:unmute|un-mute)\b"
    r"|\byou\s+can\s+talk\s+to\s+me\s+again\b"
    r"|(?:^|[.!?,]\s*|\b(?:ok|okay)\s+)come\s+back\b",
    re.IGNORECASE,
)


def asks_for_silence(text: str) -> bool:
    """True when someone explicitly asks the bot not to reply."""
    return bool(_SILENCE_RE.search(text or ""))


def asks_to_be_left_alone(text: str) -> bool:
    """True for a standing "leave me alone", which should persist."""
    return bool(_STANDING_SILENCE_RE.search(text or ""))


def asks_to_come_back(text: str) -> bool:
    """True when an opted-out person explicitly invites the bot back."""
    return bool(_COME_BACK_RE.search(text or ""))


def is_ignored_message(text: str) -> bool:
    """Manual opt-out: messages starting with ``# `` are never answered."""
    return (text or "").lstrip().startswith("# ")


def chunk_options(settings: object) -> tuple[int, float]:
    """Read chunk settings safely, including mocked settings in tests."""
    max_fragments = getattr(settings, "chat_chunk_max", 4)
    if not isinstance(max_fragments, int) or max_fragments < 1:
        max_fragments = 4
    delay = getattr(settings, "chat_chunk_delay", 0.0)
    if not isinstance(delay, (int, float)):
        delay = 0.0
    return min(max_fragments, 8), max(0.0, float(delay))


def has_owner_signature(text: str) -> bool:
    """True for messages we posted through the owner's profile.

    Those messages mention the assistant in the footer; without this guard the
    bot and Boi would treat their own attribution as a fresh mention and reply
    to the owner's message.
    """
    return "sent by <@" in (text or "").lower()


def user_mention(user_id: str) -> str:
    return f"<@{user_id}>"


def channel_mention(channel_id: str) -> str:
    return f"<#{channel_id}>"


def display_name(user: dict | None) -> str:
    if not user:
        return "someone"
    profile = user.get("profile") or {}
    return (
        profile.get("display_name")
        or user.get("real_name")
        or user.get("name")
        or user.get("id")
        or "someone"
    )


def extract_id(value: str, prefix: str) -> str | None:
    """Pull `U123` / `C123` out of a raw id or `<@U123|name>` / `<#C123|name>`."""
    value = (value or "").strip()
    if not value:
        return None
    p = prefix.upper()
    if p in {"@", "U", "W"}:
        m = re.match(r"^<@?([UW][A-Z0-9]+)(?:\|[^>]+)?>$", value, re.IGNORECASE)
        if m:
            return m.group(1).upper()
        m = re.match(r"^@?([UW][A-Z0-9]{8,14})$", value, re.IGNORECASE)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1).upper()
        return None
    if p in {"#", "C", "G"}:
        m = re.match(r"^<#?([CG][A-Z0-9]+)(?:\|[^>]+)?>$", value, re.IGNORECASE)
        if m:
            return m.group(1).upper()
        m = re.match(r"^#?([CG][A-Z0-9]{8,14})$", value, re.IGNORECASE)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1).upper()
        return None
    match = re.match(rf"^<[{prefix}]([A-Z0-9]+)(?:\|[^>]+)?>$", value, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    match = re.match(rf"^([{prefix}][A-Z0-9]+)$", value, re.IGNORECASE)
    return match.group(1).upper() if match else None


def escape_mrkdwn(text: str) -> str:
    """Escape user-provided text so it can't inject Slack mentions/links."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def disarm_broadcasts(text: str) -> str:
    """Disarm Slack broadcast mentions (<!channel>, <!everyone>, <!here>, <!subteam^...>) to prevent unwanted pings."""
    return re.sub(r"<!(channel|everyone|here|subteam\^[A-Z0-9]+)(?:\|[^>]*)?>", r"@\1", text or "")


def format_mrkdwn(text: str) -> str:
    """Normalize common Markdown constructs (bold, links, headings, dashes) to Slack mrkdwn.

    Protects fenced code blocks and inline code spans from modification, strips
    unsupported code block language tags, and auto-closes dangling code fences.
    """
    if not text:
        return ""
    if text.count("```") % 2 == 1:
        text = text + "\n```"
    pattern = r"(```[\s\S]*?```|`[^`\n]*`)"
    tokens = re.split(pattern, text)
    out: list[str] = []
    for i, token in enumerate(tokens):
        if i % 2 == 1:
            if token.startswith("```"):
                token = re.sub(r"^```[a-zA-Z0-9_\-\+\.]*[ \t]*\n", "```\n", token)
            out.append(token)
            continue
        t = token
        if t.count("`") % 2 == 1:
            t = t + "`"
        t = t.replace(" — ", " - ").replace("—", " - ").replace("–", "-")
        t = re.sub(r"\*\*\*(.+?)\*\*\*", r"*_\1_*", t)
        t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", t)
        t = re.sub(r"~~(.+?)~~", r"~\1~", t)
        t = re.sub(r"\[([^\]\n]+)\]\((https?://[^\)\s]+)\)", r"<\2|\1>", t)
        t = re.sub(r"(?m)^#{1,6}[ \t]+([^\n]+)$", r"*\1*", t)
        out.append(t)
    return "".join(out)



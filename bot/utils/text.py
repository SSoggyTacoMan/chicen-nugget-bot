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


def strip_mentions(text: str) -> str:
    return re.sub(r"<@[^>]+>", "", text or "").strip()


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

    Protects fenced code blocks and inline code spans from modification.
    """
    if not text:
        return ""
    pattern = r"(```[\s\S]*?```|`[^`\n]*`)"
    tokens = re.split(pattern, text)
    out: list[str] = []
    for i, token in enumerate(tokens):
        if i % 2 == 1:
            out.append(token)
            continue
        t = token
        t = t.replace(" — ", " - ").replace("—", " - ").replace("–", "-")
        t = re.sub(r"\*\*\*(.+?)\*\*\*", r"*_\1_*", t)
        t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", t)
        t = re.sub(r"~~(.+?)~~", r"~\1~", t)
        t = re.sub(r"\[([^\]\n]+)\]\((https?://[^\)\s]+)\)", r"<\2|\1>", t)
        t = re.sub(r"(?m)^#{1,6}\s+([^\n]+)$", r"*\1*", t)
        out.append(t)
    return "".join(out)



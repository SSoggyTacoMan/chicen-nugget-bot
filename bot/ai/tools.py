"""The tools the AI model can call.

Every tool returns a short human/model readable string. Owner-only tools are
checked here against the real Slack sender, not just the prompt.
"""

from __future__ import annotations

import asyncio
import email
import email.header
import html
import imaplib
import io
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx
from slack_sdk.web.async_client import AsyncWebClient

from bot import image_access
from bot.config import Settings
from bot.db import Database
from bot.features.nuggets import random_fact
from bot.features.stats import (
    collect_system_diagnostics,
    format_stats,
    format_system_diagnostics,
)
from bot.reminders import confirmation_text, create_reminder, dm_target, reminder_line
from bot.runtime import runtime
from bot.slack_api import (
    all_channels,
    all_users,
    channel_info,
    error_code,
    is_private_host,
    post,
    ssrf_filter,
    user_info,
)
from bot.utils import dice
from bot.utils.text import disarm_broadcasts, extract_id, format_mrkdwn
from bot.utils.timeparse import format_when, parse_when
from bot.utils.tz import tzinfo

logger = logging.getLogger(__name__)

MAX_TOOL_RESULT = 7000
WEATHER_CODES = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light rain showers",
    81: "rain showers",
    82: "violent rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "heavy thunderstorm with hail",
}


def _notes_dir(settings: Settings) -> Path:
    p = settings.data_dir / "notes"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sanitize_topic(topic: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "", topic.strip().lower())
    return cleaned or "general"


def _list_note_files(settings: Settings) -> list[Path]:
    d = _notes_dir(settings)
    return sorted(d.glob("*.md"))


def _notes_index(settings: Settings) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for f in _list_note_files(settings):
        desc = ""
        try:
            head = f.read_text(encoding="utf-8")[:1000]
            m = re.search(r"^description:\s*(.+)$", head, re.MULTILINE)
            if m:
                desc = m.group(1).strip()
            elif head:
                first = [
                    line.strip("# -")
                    for line in head.splitlines()
                    if line.strip() and not line.startswith("---")
                ]
                if first:
                    desc = first[0][:80]
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("failed reading note %s: %s", f.name, exc)
        out.append({"file": f.name, "topic": f.stem, "description": desc or "(no description)"})
    return out


@dataclass
class ToolContext:
    db: Database
    settings: Settings
    client: AsyncWebClient
    user_client: AsyncWebClient | None
    bot_user_id: str
    user_id: str
    channel_id: str | None
    thread_ts: str | None
    is_dm: bool
    is_owner: bool
    is_admin: bool
    bot_client: AsyncWebClient | None = None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        elif tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def _decode_mime(val: str | None) -> str:
    if not val:
        return ""
    try:
        parts = email.header.decode_header(val)
        out = []
        for b, enc in parts:
            if isinstance(b, bytes):
                out.append(b.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(str(b))
        return "".join(out)
    except Exception:
        return str(val)


def _extract_mime_body(msg: email.message.Message, max_len: int = 2000) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in cdisp:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
                    break
        if not body:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        raw_html = payload.decode(charset, errors="replace")
                        parser = _TextExtractor()
                        parser.feed(raw_html[:30000])
                        body = "".join(parser.parts)
                        break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return "\n".join(lines)[:max_len]


def _sync_imap_op(
    host: str,
    port: int,
    user: str,
    password: str,
    folder: str,
    action: str,
    query: str,
    email_id: str,
    limit: int,
) -> str:
    try:
        client = imaplib.IMAP4_SSL(host, port, timeout=15)
    except Exception as exc:
        return _error(f"failed to connect to imap server {host}:{port}: {exc}")

    try:
        typ, _ = client.login(user, password)
        if typ != "OK":
            return _error("imap login failed (check credentials or app password)")
        typ, _ = client.select(folder or "INBOX", readonly=True)
        if typ != "OK":
            return _error(f"could not open mailbox folder {folder or 'INBOX'}")

        if action == "read" or email_id:
            target_id = email_id or query
            if not target_id:
                return _error("email_id is required to read email")
            typ, data = client.fetch(target_id, "(RFC822)")
            if typ != "OK" or not data or not data[0] or not isinstance(data[0], tuple):
                return _error(f"email #{target_id} not found")
            msg = email.message_from_bytes(data[0][1])
            info = {
                "id": target_id,
                "from": _decode_mime(msg.get("From")),
                "to": _decode_mime(msg.get("To")),
                "subject": _decode_mime(msg.get("Subject")),
                "date": msg.get("Date"),
                "body": _extract_mime_body(msg, max_len=4000),
            }
            return _json(info)

        if action == "search" and query:
            safe_q = query.replace('"', "")
            criterion = f'(TEXT "{safe_q}")'
        elif action == "recent":
            criterion = "ALL"
        else:
            criterion = "UNSEEN"

        typ, data = client.search(None, criterion)
        if typ != "OK":
            return _error("imap search failed")

        msg_nums = data[0].split()
        if not msg_nums:
            desc = "unread" if criterion == "UNSEEN" else f"matching '{query}'" if query else "recent"
            return f"no {desc} emails found in {folder or 'INBOX'}."

        selected = msg_nums[-limit:]
        selected.reverse()

        emails = []
        for num in selected:
            typ, fetch_data = client.fetch(num, "(RFC822)")
            if typ == "OK" and fetch_data and isinstance(fetch_data[0], tuple):
                msg = email.message_from_bytes(fetch_data[0][1])
                emails.append(
                    {
                        "id": num.decode("ascii", errors="replace"),
                        "from": _decode_mime(msg.get("From")),
                        "subject": _decode_mime(msg.get("Subject")),
                        "date": msg.get("Date"),
                        "snippet": _extract_mime_body(msg, max_len=250),
                    }
                )

        return _json(emails)
    except Exception as exc:
        return _error(f"imap error: {exc}")
    finally:
        try:
            client.close()
            client.logout()
        except Exception:
            pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:MAX_TOOL_RESULT]


def _pick(values: list[Any] | None, index: int, default: Any = "?") -> Any:
    if not values or index >= len(values):
        return default
    return values[index]


def _error(message: str) -> str:
    return _json({"error": message})


def _need_owner(ctx: ToolContext) -> bool:
    return ctx.is_owner or ctx.is_admin


def _default_repo(settings: Settings) -> str:
    """`https://github.com/owner/repo` -> `owner/repo`, else empty."""
    match = re.search(r"github\.com/([^/\s]+/[^/\s#?]+)", settings.repo_url or "")
    return match.group(1).removesuffix(".git") if match else ""


def _numbers(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []
    out: list[float] = []
    for value in values:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def parse_slack_ref(ref: str) -> tuple[str | None, str | None]:
    m = re.search(r"archives/([CDG][0-9A-Z]+)/p(\d{10})(\d{1,6})", ref)
    if m:
        return m.group(1), f"{m.group(2)}.{m.group(3)}"
    m_cid = re.search(r"[?&]cid=([CDG][0-9A-Z]+)", ref)
    m_ts = re.search(r"[?&]thread_ts=(\d+\.\d+)", ref)
    if m_cid and m_ts:
        return m_cid.group(1), m_ts.group(1)
    m_raw = re.match(r"([CDG][0-9A-Z]+):(\d+\.\d+)", ref.strip())
    if m_raw:
        return m_raw.group(1), m_raw.group(2)
    m_c = re.search(r"<#([CDG][0-9A-Z]+)", ref)
    if m_c:
        return m_c.group(1), None
    m_b = re.match(r"([CDG][0-9A-Z]+)$", ref.strip())
    if m_b:
        return m_b.group(1), None
    m_arch = re.search(r"archives/([CDG][0-9A-Z]+)(?!/\w)", ref)
    if m_arch:
        return m_arch.group(1), None
    return None, None


def parse_slack_thread_ref(ref: str) -> tuple[str | None, str | None]:
    cid, ts = parse_slack_ref(ref)
    if not cid:
        return None, None
    m_thread = re.search(r"[?&]thread_ts=(\d+\.\d+)", ref)
    if m_thread:
        return cid, m_thread.group(1)
    return cid, ts


class Toolbox:
    """Registry of model-callable tools."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------ definitions
    @property
    def definitions(self) -> list[dict[str, Any]]:
        def tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            }

        tools = [
            tool(
                "get_weather",
                "Current weather and a short daily forecast for a place. Uses open-meteo, no key needed.",
                {
                    "location": {"type": "string", "description": "City or place name. Omit to use the configured default."},
                    "days": {"type": "integer", "description": "Forecast days, 1-7. Default 3."},
                },
                [],
            ),
            tool(
                "roll_dice",
                "Roll dice in standard notation: d20, 2d6+3, 4d8-1, coin.",
                {"notation": {"type": "string", "description": "Dice notation."}},
                ["notation"],
            ),
            tool(
                "remind",
                "Set a one-off or recurring reminder. Only the owner/admin can target a channel.",
                {
                    "when": {
                        "type": "string",
                        "description": "When, e.g. '10m', 'tomorrow 09:00', 'daily 08:30', 'weekly mon 18:00', 'in 2h'.",
                    },
                    "text": {"type": "string", "description": "What to remind about."},
                    "target": {
                        "type": "string",
                        "description": "'dm' (default), 'here', a channel id like C123, or #channel-name.",
                    },
                },
                ["when", "text"],
            ),
            tool(
                "list_reminders",
                "List active reminders. Owner/admin only.",
                {"all": {"type": "boolean", "description": "List every user's reminders instead of just yours."}},
                [],
            ),
            tool(
                "cancel_reminder",
                "Cancel an active reminder by id. Owner/admin, or the person who created it.",
                {"id": {"type": "integer", "description": "Reminder id."}},
                ["id"],
            ),
            tool(
                "search_slack",
                "Search Slack messages as the bot's user token. Needs SLACK_USER_TOKEN with search:read.",
                {
                    "query": {"type": "string", "description": "Slack search query, supports in:#channel, from:@user, etc."},
                    "count": {"type": "integer", "description": "Max results, 1-20."},
                },
                ["query"],
            ),
            tool(
                "lookup_user",
                "Find a Slack user by id, name, display name or email. Returns real ids for mentions.",
                {"query": {"type": "string", "description": "User id, name, display name or email."}},
                ["query"],
            ),
            tool(
                "lookup_channel",
                "Find a Slack channel by id or name. Returns the real channel id for mentions.",
                {"query": {"type": "string", "description": "Channel id, #name, or name."}},
                ["query"],
            ),
            tool(
                "get_channel_history",
                "Read recent messages from a Slack channel to catch up or answer questions. Owner/admin only.",
                {
                    "channel": {"type": "string", "description": "Channel id, #name, or 'here'."},
                    "limit": {"type": "integer", "description": "How many messages, 1-100. Default 30."},
                },
                ["channel"],
            ),
            tool(
                "send_dm",
                "Send a direct message to a Slack user as the bot app or user assistant. Owner/admin only.",
                {
                    "user": {"type": "string", "description": "User id, @name, display name, or email."},
                    "text": {"type": "string", "description": "Message text (Slack mrkdwn)."},
                    "as_user": {
                        "type": "boolean",
                        "description": "Send as personal user assistant (Boi) instead of bot app. Default false.",
                    },
                },
                ["user", "text"],
            ),
            tool(
                "nugget_fact",
                "Return a random chicken nugget fact. Use for fun nugget trivia.",
                {},
                [],
            ),
            tool(
                "remember",
                "Save a durable note or rule in markdown memory and database. Owner/admin only.",
                {
                    "text": {"type": "string", "description": "The fact, rule, or note to store."},
                    "topic": {
                        "type": "string",
                        "description": "Optional category or file name (e.g. 'rules', 'projects', 'people'). Defaults to 'general'.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional 1-line summary of what this topic note covers (used for the memory index).",
                    },
                },
                ["text"],
            ),
            tool(
                "recall",
                "Search your markdown memory notes and conversation history. Owner/admin only.",
                {
                    "query": {
                        "type": "string",
                        "description": "What to search for across notes and history. Omit to list the notes index and recent notes.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Optional specific topic/note file to read in full (e.g. 'rules', 'general').",
                    },
                },
                [],
            ),
            tool(
                "post_message",
                "Post a message to a Slack channel as the bot app or user assistant. Owner/admin only.",
                {
                    "channel": {"type": "string", "description": "Channel id, #name, or 'here'."},
                    "text": {"type": "string", "description": "Message text (Slack mrkdwn)."},
                    "as_user": {
                        "type": "boolean",
                        "description": "Send as personal user assistant (Boi) instead of bot app. Default false.",
                    },
                },
                ["channel", "text"],
            ),
            tool(
                "slack_status",
                "View, set, or clear Slack custom status text, emoji, and expiration for the user assistant. Owner/admin only.",
                {
                    "action": {
                        "type": "string",
                        "enum": ["get", "set", "clear"],
                        "description": "Action: 'get' (view current status), 'set' (update status), or 'clear' (remove status).",
                    },
                    "text": {"type": "string", "description": "Status text to set."},
                    "emoji": {
                        "type": "string",
                        "description": "Status emoji code (e.g. ':speech_balloon:', ':calendar:', ':coffee:').",
                    },
                    "minutes": {
                        "type": "integer",
                        "description": "Optional expiration duration in minutes from now.",
                    },
                },
                ["action"],
            ),
            tool(
                "slack_dnd",
                "Check DND (Do Not Disturb) snooze status, or snooze/end notification snooze for the user assistant. Owner/admin only.",
                {
                    "action": {
                        "type": "string",
                        "enum": ["info", "snooze", "end"],
                        "description": "Action: 'info' (check status), 'snooze' (snooze notifications), or 'end' (turn off snooze).",
                    },
                    "minutes": {
                        "type": "integer",
                        "description": "Number of minutes to snooze notifications (required for action='snooze').",
                    },
                },
                ["action"],
            ),
            tool(
                "pending_access_requests",
                "List pending tier 2 access requests with ids and reasons. Owner/admin only.",
                {},
                [],
            ),
            tool(
                "get_stats",
                "Workspace stats: joins, leaves, access requests, polls, reminders. Owner/admin only.",
                {},
                [],
            ),
            tool(
                "fetch_url",
                "Read a public web page or API as plain text or JSON. Blocks private addresses. Owner/admin only.",
                {"url": {"type": "string", "description": "Public http(s) URL."}},
                ["url"],
            ),
            tool(
                "search_web",
                "Search the live web for current information, documentation, news, or answers. Owner/admin only.",
                {"query": {"type": "string", "description": "Search query."}},
                ["query"],
            ),
            tool(
                "generate_image",
                "Generate an AI image from a text prompt and post it to the channel or DM. Owner/admin only.",
                {
                    "prompt": {"type": "string", "description": "Visual prompt describing the image to generate."},
                    "ratio": {
                        "type": "string",
                        "description": "Aspect ratio: '1:1', '16:9', '9:16', '4:3', '3:4'. Default '1:1'.",
                    },
                },
                ["prompt"],
            ),
            tool(
                "make_chart",
                "Render a chart from data as a PNG and upload it to the conversation. Owner/admin only.",
                {
                    "type": {
                        "type": "string",
                        "enum": ["bar", "line", "pie", "scatter", "hist"],
                        "description": "Chart type. Default 'bar'.",
                    },
                    "title": {"type": "string", "description": "Chart title."},
                    "x_label": {"type": "string", "description": "X axis label."},
                    "y_label": {"type": "string", "description": "Y axis label."},
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Category labels for the first series.",
                    },
                    "values": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Numbers for the first series, in the same order as labels.",
                    },
                    "series": {
                        "type": "array",
                        "description": "Optional extra series: [{'name': 'series name', 'values': [1, 2, 3]}].",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "values": {"type": "array", "items": {"type": "number"}},
                            },
                            "required": ["name", "values"],
                        },
                    },
                },
                [],
            ),
            tool(
                "make_deck",
                "Build a PowerPoint (.pptx) deck from a title and slides, then upload it to the conversation. Owner/admin only.",
                {
                    "title": {"type": "string", "description": "Deck title."},
                    "subtitle": {"type": "string", "description": "Optional subtitle for the title slide."},
                    "slides": {
                        "type": "array",
                        "description": "Slides in order: [{'title': '...', 'bullets': ['...'], 'notes': 'optional speaker notes'}].",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "bullets": {"type": "array", "items": {"type": "string"}},
                                "notes": {"type": "string"},
                            },
                            "required": ["title"],
                        },
                    },
                },
                ["title", "slides"],
            ),
            tool(
                "flaron",
                "Query the public Hack Club Slack directory (flaron.halceon.dev). Actions: user, channel, user_search, channel_search, channel_by_name, channel_managers, channel_members, app, emoji, command, promote.",
                {
                    "action": {
                        "type": "string",
                        "enum": [
                            "user",
                            "channel",
                            "user_search",
                            "channel_search",
                            "channel_by_name",
                            "channel_managers",
                            "channel_members",
                            "app",
                            "emoji",
                            "command",
                            "promote",
                        ],
                        "description": "Which lookup to perform.",
                    },
                    "id": {"type": "string", "description": "Slack id for user/channel/app/etc."},
                    "name": {"type": "string", "description": "Name for channel_by_name, emoji, or command."},
                    "query": {"type": "string", "description": "Search term for user_search or channel_search."},
                },
                ["action"],
            ),
            tool(
                "fetch_slack_message",
                "Fetch the actual text and author of a specific Slack message. Pass a Slack permalink/URL or channel:timestamp reference. Owner/admin only.",
                {"ref": {"type": "string", "description": "Slack permalink URL or channel:timestamp."}},
                ["ref"],
            ),
            tool(
                "summarize_thread",
                "Read messages in a Slack thread to provide a summary or context. Pass a permalink to the thread or omit to use current thread. Owner/admin only.",
                {"ref": {"type": "string", "description": "Slack permalink URL, or omit for current thread."}},
                [],
            ),
            tool(
                "ocr_document",
                "Extract text and markdown from an image or PDF document URL using Hack Club AI Mistral OCR. Owner/admin only.",
                {"url": {"type": "string", "description": "Public URL of an image (PNG, JPG, WebP) or PDF."}},
                ["url"],
            ),
            tool(
                "public_holidays",
                "Look up upcoming public holidays for a country via Nager.Date. Default country is US.",
                {
                    "country": {"type": "string", "description": "2-letter country code, e.g. US, NL, CA, GB. Default US."},
                    "year": {"type": "integer", "description": "Optional 4-digit calendar year."},
                },
                [],
            ),
            tool(
                "check_emails",
                "Check recent or unread emails via IMAP, search your inbox, or read a specific email. Owner/admin only.",
                {
                    "action": {
                        "type": "string",
                        "enum": ["unread", "recent", "search", "read"],
                        "description": "What to do: 'unread' (default), 'recent', 'search', or 'read'.",
                    },
                    "query": {"type": "string", "description": "Search keyword or sender for action='search'."},
                    "email_id": {"type": "string", "description": "Email ID/number to read full content for action='read'."},
                    "folder": {"type": "string", "description": "Mailbox folder, default 'INBOX'."},
                    "limit": {"type": "integer", "description": "How many emails to return, 1-20. Default 5."},
                },
                [],
            ),
            tool(
                "hackatime_stats",
                "Look up Hack Club Hackatime coding time, streak, and top languages for a user.",
                {
                    "username": {
                        "type": "string",
                        "description": "Hackatime/GitHub username. Defaults to GITHUB_USERNAME when set.",
                    },
                },
                [],
            ),
            tool(
                "github_info",
                "Look up GitHub repositories, issues, pull requests, commits, releases, or user profiles.",
                {
                    "action": {
                        "type": "string",
                        "enum": ["user", "repo", "commits", "release", "issues", "prs", "issue"],
                        "description": "Action: 'repo' (default), 'issues', 'prs', 'issue', 'commits', 'release', or 'user'.",
                    },
                    "target": {
                        "type": "string",
                        "description": "Username (for 'user'), 'owner/repo' (for 'repo', 'commits', 'release', 'issues', 'prs'), or 'owner/repo#123' (for 'issue'). Defaults to REPO_URL when set.",
                    },
                    "state": {
                        "type": "string",
                        "enum": ["open", "closed", "all"],
                        "description": "Filter state for 'issues' or 'prs': 'open' (default), 'closed', or 'all'.",
                    },
                    "number": {
                        "type": "integer",
                        "description": "Specific issue or pull request number (for action='issue').",
                    },
                },
                ["action"],
            ),
            tool(
                "run_code",
                "Execute a Python code snippet in a sandboxed subprocess and return stdout/stderr. Owner/admin only.",
                {"code": {"type": "string", "description": "Python 3 code to execute."}},
                ["code"],
            ),
            tool(
                "hacker_news",
                "Fetch top stories, discussions, and links from Hacker News (top, new, best, ask, show). Owner/admin only.",
                {
                    "category": {
                        "type": "string",
                        "enum": ["top", "new", "best", "ask", "show"],
                        "description": "Category: 'top' (default), 'new', 'best', 'ask', or 'show'.",
                    },
                    "limit": {"type": "integer", "description": "Number of stories, 1-10. Default 5."},
                },
                [],
            ),
            tool(
                "http_status",
                "Inspect a website, host or API: check response status, latency, headers, and redirect chains. Owner/admin only.",
                {"url": {"type": "string", "description": "URL or domain name to test."}},
                ["url"],
            ),
            tool(
                "dog_fact",
                "Get a random real dog fact for the doggy taco fellas.",
                {},
                [],
            ),
            tool(
                "taco_recipe",
                "Generate a creative taco recipe or delicious taco inspiration for the doggy taco fellas.",
                {"theme": {"type": "string", "description": "Optional theme: 'spicy', 'breakfast', 'crispy', 'street', 'birria', 'vegan'."}},
                [],
            ),
            tool(
                "urban_dict",
                "Look up modern slang, terms, or internet memes on Urban Dictionary with definitions and examples.",
                {"term": {"type": "string", "description": "The word or phrase to look up."}},
                ["term"],
            ),
            tool(
                "bot_config",
                "View or change bot runtime settings (recaps, daily_digest, personal_updates, welcomer, tier2, tier2_channel, admin_channel, ambient_ai, thread_followups, weather_location). Owner/admin only.",
                {
                    "action": {
                        "type": "string",
                        "enum": ["get", "set", "list", "reset"],
                        "description": "Action: 'get', 'set', 'list', or 'reset'.",
                    },
                    "key": {
                        "type": "string",
                        "description": "Setting name: 'recaps', 'daily_digest', 'personal_updates', 'welcomer', 'tier2', 'tier2_channel', 'admin_channel', 'ambient_ai', 'thread_followups', 'weather_location'.",
                    },
                    "value": {
                        "type": "string",
                        "description": "New value to set (e.g. 'off', 'on', 'false', 'true', 'dm', or channel ID).",
                    },
                },
                ["action"],
            ),
            tool(
                "image_access",
                "List, add or remove people allowed to use image generation. Admins always have access. Owner/admin only.",
                {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "remove"],
                        "description": "Action: 'list', 'add', or 'remove'.",
                    },
                    "user": {
                        "type": "string",
                        "description": "Slack user id or @mention for add/remove.",
                    },
                },
                ["action"],
            ),
            tool(
                "system_diagnostics",
                "Inspect server and host diagnostics: host OS, uptime, CPU load, memory usage, disk space, Python runtime, and SQLite database size. Owner/admin only.",
                {},
                [],
            ),
            tool(
                "manage_tasks",
                "Manage a persistent personal to-do checklist in SQLite: add, list, complete, uncomplete, delete, or clear tasks.",
                {
                    "action": {
                        "type": "string",
                        "enum": ["add", "list", "complete", "uncomplete", "delete", "clear_completed"],
                        "description": "Action: 'add', 'list', 'complete', 'uncomplete', 'delete', or 'clear_completed'.",
                    },
                    "title": {"type": "string", "description": "Task description for action='add'."},
                    "task_id": {"type": "integer", "description": "Task ID number for complete, uncomplete, or delete."},
                    "show_all": {"type": "boolean", "description": "Whether to list completed tasks as well as pending. Default false."},
                },
                ["action"],
            ),
            tool(
                "inspect_code",
                "Inspect local codebase files in the bot repository: read file contents, list directories, or search code. Owner/admin only.",
                {
                    "action": {
                        "type": "string",
                        "enum": ["read", "list", "search"],
                        "description": "Action: 'read' (default), 'list', or 'search'.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative file or directory path in the repo (e.g. 'bot/ai/tools.py', 'prompts', or empty for root).",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Search pattern (text or regex) for action='search'.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (1-based, default 1) for action='read'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read for action='read' (default 150).",
                    },
                },
                ["action"],
            ),
            tool(
                "clone_repo",
                "Shallow clone a remote Git repository into an isolated sandbox, inspect its files (list, read, or search), and auto-delete immediately after. Owner/admin only.",
                {
                    "repo_url": {
                        "type": "string",
                        "description": "Git URL (https://...) or GitHub 'owner/repo' shorthand.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "search"],
                        "description": "Action: 'list' (default), 'read', or 'search'.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative file or directory path within the cloned repository.",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Search pattern (text or regex) for action='search'.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (1-based, default 1) for action='read'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read for action='read' (default 150).",
                    },
                },
                ["repo_url"],
            ),
        ]
        return tools

    # ---------------------------------------------------------------- dispatch
    async def execute(self, name: str, args: Any, ctx: ToolContext) -> str:
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return _error(f"unknown tool: {name}")
            return await handler(args, ctx)
        except Exception as exc:
            logger.exception("tool %s failed", name)
            return _error(f"tool failed: {exc}")

    # ------------------------------------------------------------------ tools
    async def _tool_get_weather(self, args: dict[str, Any], ctx: ToolContext) -> str:
        loc_setting = ctx.db.kv_get("config:weather_location") if ctx.db else None
        location = (args.get("location") or loc_setting or self.settings.weather_default_location or "").strip()
        if not location:
            return _error("no location given and WEATHER_DEFAULT_LOCATION is not set")
        days = max(1, min(7, int(args.get("days") or 3)))

        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "chicen-nugget-bot"}) as http:
            geo = await http.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1, "language": "en", "format": "json"},
            )
            geo.raise_for_status()
            results = (geo.json() or {}).get("results") or []
            if not results:
                return _error(f"could not find a place called {location!r}")
            place = results[0]
            forecast = await http.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": "temperature_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "auto",
                    "forecast_days": days,
                },
            )
            forecast.raise_for_status()
            data = forecast.json()

        current = data.get("current") or {}
        daily = data.get("daily") or {}
        label = ", ".join(
            part for part in (place.get("name"), place.get("admin1"), place.get("country")) if part
        )
        lines = [
            f"weather for {label} ({place.get('timezone', 'local time')})",
            f"now: {current.get('temperature_2m')}°C (feels {current.get('apparent_temperature')}°C), "
            f"{WEATHER_CODES.get(current.get('weather_code'), 'unknown')}, "
            f"wind {current.get('wind_speed_10m')} km/h, precip {current.get('precipitation')} mm",
            "",
        ]
        for i, day in enumerate(daily.get("time") or []):
            code = _pick(daily.get("weather_code"), i)
            lines.append(
                f"{day}: {WEATHER_CODES.get(code, 'unknown')}, "
                f"{_pick(daily.get('temperature_2m_min'), i)}–"
                f"{_pick(daily.get('temperature_2m_max'), i)}°C, "
                f"rain {_pick(daily.get('precipitation_probability_max'), i)}%"
            )
        return "\n".join(lines)[:MAX_TOOL_RESULT]

    async def _tool_roll_dice(self, args: dict[str, Any], ctx: ToolContext) -> str:
        result = dice.roll(str(args.get("notation") or ""))
        if not result:
            return _error("invalid dice notation. try d20, 2d6+3, 4d8-1 or coin.")
        return f"{result.notation}: {result.breakdown} = *{result.total}* (rolled {len(result.rolls)}d{result.sides})"

    async def _tool_remind(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can create reminders")
        tz = tzinfo()
        parsed = parse_when(str(args.get("when") or ""), tz=tz)
        if not parsed:
            return _error("could not parse the time. try 10m, tomorrow 09:00, daily 08:30 or weekly mon 18:00.")
        text = str(args.get("text") or "").strip()
        if not text:
            return _error("the reminder needs some text")

        target_arg = str(args.get("target") or "dm").strip()
        if target_arg.lower() in {"dm", "me", ""}:
            target = dm_target(ctx.user_id)
        else:
            target_id = await self._resolve_channel(ctx, target_arg)
            if not target_id:
                return _error(f"could not resolve channel {target_arg!r}")
            target = target_id

        reminder = create_reminder(
            ctx.db,
            self.settings,
            user_id=ctx.user_id,
            target=target,
            parsed=parsed,
            text=text,
            tz=tz,
        )
        if not reminder:
            return _error(f"reminder limit reached ({self.settings.max_reminders} active)")
        return confirmation_text(reminder, tz, target)

    async def _tool_list_reminders(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can list all reminders")
        all_users_flag = bool(args.get("all"))
        rows = ctx.db.list_reminders(None if all_users_flag else ctx.user_id)
        if not rows:
            return "no active reminders."
        tz = tzinfo()
        return "\n".join(reminder_line(row, tz) for row in rows[:30])

    async def _tool_cancel_reminder(self, args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            reminder_id = int(args.get("id"))
        except (TypeError, ValueError):
            return _error("id must be a number")
        reminder = ctx.db.reminder(reminder_id)
        if not reminder:
            return _error(f"no reminder #{reminder_id}")
        if not (ctx.is_admin or reminder["user_id"] == ctx.user_id):
            return _error("you can only cancel your own reminders")
        if reminder["done"]:
            return _error(f"reminder #{reminder_id} is already done")
        ctx.db.cancel_reminder(reminder_id)
        return f"cancelled reminder #{reminder_id}."

    async def _tool_search_slack(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if ctx.user_client is None:
            return _error("SLACK_USER_TOKEN is not configured (needs search:read)")
        query = str(args.get("query") or "").strip()
        if not query:
            return _error("query is required")
        count = max(1, min(20, int(args.get("count") or 10)))
        try:
            response = await ctx.user_client.search_messages(
                query=query, count=count, sort="timestamp", sort_dir="desc"
            )
        except Exception as exc:  # noqa: BLE001
            return _error(f"slack search failed: {error_code(exc)}")
        matches = (((response.get("messages") or {}).get("matches")) or [])[:count]
        if not matches:
            return "no slack messages matched that search."
        cleaned = []
        for match in matches:
            cleaned.append(
                {
                    "text": (match.get("text") or "")[:800],
                    "user": match.get("user"),
                    "username": match.get("username"),
                    "channel": (match.get("channel") or {}).get("name"),
                    "channel_id": (match.get("channel") or {}).get("id"),
                    "permalink": match.get("permalink"),
                    "ts": match.get("ts"),
                }
            )
        return _json(cleaned)

    async def _tool_lookup_user(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return _error("query is required")
        q_lower = query.lower()
        if q_lower in {"me", "myself", "self"} and ctx.user_id:
            user = await user_info(ctx.client, ctx.user_id)
            return _json(self._user_summary(user)) if user else _error("no user with that id")
        if q_lower == "owner" and ctx.settings.owner_user_id:
            user = await user_info(ctx.client, ctx.settings.owner_user_id)
            return _json(self._user_summary(user)) if user else _error("no user with that id")
        user_id = extract_id(query, "@")
        if user_id:
            user = await user_info(ctx.client, user_id)
            return _json(self._user_summary(user)) if user else _error("no user with that id")
        if "@" in query and "." in query:
            try:
                response = await ctx.client.users_lookupByEmail(email=query)
                user = response.get("user")
                return _json(self._user_summary(user)) if user else _error("no user with that email")
            except Exception:  # noqa: BLE001 - fall through to a name search
                pass
        needle = query.lstrip("@").lower()
        matches = []
        for user in await all_users(ctx.client):
            profile = user.get("profile") or {}
            haystack = " ".join(
                str(value or "")
                for value in (
                    user.get("id"),
                    user.get("name"),
                    user.get("real_name"),
                    profile.get("display_name"),
                    profile.get("real_name"),
                    profile.get("email"),
                )
            ).lower()
            if needle in haystack:
                matches.append(self._user_summary(user))
        if not matches:
            return "no users matched that."
        return _json(matches[:8])

    def _user_summary(self, user: dict[str, Any] | None) -> dict[str, Any]:
        if not user:
            return {}
        profile = user.get("profile") or {}
        return {
            "id": user.get("id"),
            "name": user.get("name"),
            "real_name": user.get("real_name"),
            "display_name": profile.get("display_name"),
            "title": profile.get("title"),
            "email": profile.get("email"),
            "is_bot": user.get("is_bot"),
            "tz": user.get("tz"),
            "deleted": user.get("deleted"),
        }

    async def _tool_lookup_channel(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return _error("query is required")
        channel_id = extract_id(query, "#")
        if channel_id:
            info = await channel_info(ctx.client, channel_id)
            return _json(self._channel_summary(info)) if info else _error("no channel with that id")
        needle = query.lstrip("#").lower()
        matches = [
            self._channel_summary(channel)
            for channel in await all_channels(ctx.client)
            if needle in str(channel.get("name", "")).lower() or needle == str(channel.get("id", "")).lower()
        ]
        if not matches:
            return "no channels matched that."
        return _json(matches[:8])

    def _channel_summary(self, channel: dict[str, Any] | None) -> dict[str, Any]:
        if not channel:
            return {}
        return {
            "id": channel.get("id"),
            "name": channel.get("name"),
            "is_private": channel.get("is_private"),
            "is_archived": channel.get("is_archived"),
            "topic": ((channel.get("topic") or {}).get("value")),
            "purpose": ((channel.get("purpose") or {}).get("value")),
        }

    def _get_target_client(self, ctx: ToolContext, as_user: bool) -> tuple[AsyncWebClient | None, str | None]:
        if as_user:
            if not ctx.user_client:
                return None, "user client is not configured (needs SLACK_USER_TOKEN)"
            return ctx.user_client, None
        return ctx.bot_client or ctx.client, None

    async def _tool_get_channel_history(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can read channel history")
        channel_value = str(args.get("channel") or "here").strip()
        channel_id = await self._resolve_channel(ctx, channel_value)
        if not channel_id:
            return _error(f"could not resolve channel {channel_value!r}")
        try:
            limit = max(1, min(100, int(args.get("limit") or 30)))
        except (TypeError, ValueError):
            limit = 30
        client = ctx.client
        if channel_id.startswith("D") and ctx.user_client and client is not ctx.user_client:
            client = ctx.user_client
        try:
            response = await client.conversations_history(channel=channel_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            alt = ctx.user_client if client is not ctx.user_client else (ctx.bot_client or ctx.client)
            if alt and alt is not client:
                try:
                    response = await alt.conversations_history(channel=channel_id, limit=limit)
                except Exception as exc2:  # noqa: BLE001
                    return _error(f"could not read channel: {error_code(exc2)}")
            else:
                return _error(f"could not read channel: {error_code(exc)}")
        tz = tzinfo()
        messages = []
        for message in reversed(response.get("messages") or []):
            if message.get("subtype") in {
                "channel_join",
                "channel_leave",
                "channel_topic",
                "channel_purpose",
            }:
                continue
            text = (message.get("text") or "").strip()
            if not text:
                continue
            try:
                when = datetime.fromtimestamp(float(message.get("ts")), tz).strftime("%a %H:%M")
            except (TypeError, ValueError, OSError):
                when = "?"
            messages.append(
                {
                    "when": when,
                    "user": message.get("user") or message.get("bot_id"),
                    "text": text[:800],
                    "ts": message.get("ts"),
                    "thread_ts": message.get("thread_ts"),
                }
            )
        if not messages:
            return "no recent messages in that channel."
        return _json({"channel": channel_id, "messages": messages})

    async def _tool_send_dm(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can send direct messages")
        query = str(args.get("user") or "").strip()
        text = str(args.get("text") or "").strip()
        as_user = bool(args.get("as_user", False))
        if not query:
            return _error("which user?")
        if not text:
            return _error("message text is required")
        user = await self._find_user(ctx, query)
        if not user:
            return _error(f"could not find a user matching {query!r}")
        user_id = user.get("id")
        target_client, err = self._get_target_client(ctx, as_user)
        if err or not target_client:
            return _error(err or "slack client not available")
        target = user_id
        try:
            opened = await target_client.conversations_open(users=user_id)
            ch = opened.get("channel") if hasattr(opened, "get") else None
            dm_id = ch.get("id") if hasattr(ch, "get") else None
            if isinstance(dm_id, str) and dm_id.startswith("D"):
                target = dm_id
        except Exception as exc:  # noqa: BLE001 - fall back to posting with user_id directly
            logger.debug("conversations_open failed for %s: %s", user_id, error_code(exc))
        if not await post(target_client, target, disarm_broadcasts(format_mrkdwn(text))):
            return _error("slack rejected the DM")
        sender = "Boi (user account)" if as_user else "Bot app"
        return f"sent a DM to <@{user_id}> as {sender}."

    async def _tool_nugget_fact(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return random_fact()

    async def _find_user(self, ctx: ToolContext, query: str) -> dict[str, Any] | None:
        q_lower = query.strip().lower()
        if q_lower in {"me", "myself", "self"} and ctx.user_id:
            return await user_info(ctx.client, ctx.user_id)
        if q_lower == "owner" and ctx.settings.owner_user_id:
            return await user_info(ctx.client, ctx.settings.owner_user_id)
        user_id = extract_id(query, "@")
        if user_id:
            return await user_info(ctx.client, user_id)
        if "@" in query and "." in query:
            try:
                response = await ctx.client.users_lookupByEmail(email=query)
                return response.get("user")
            except Exception:  # noqa: BLE001 - fall through to a name search
                pass
        needle = query.lstrip("@").lower()
        for user in await all_users(ctx.client):
            profile = user.get("profile") or {}
            haystack = " ".join(
                str(value or "")
                for value in (
                    user.get("id"),
                    user.get("name"),
                    user.get("real_name"),
                    profile.get("display_name"),
                    profile.get("real_name"),
                    profile.get("email"),
                )
            ).lower()
            if needle in haystack:
                return user
        return None

    async def _tool_remember(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can store notes")
        text = str(args.get("text") or "").strip()
        if not text:
            return _error("nothing to remember")
        topic = _sanitize_topic(str(args.get("topic") or "general"))
        desc = str(args.get("description") or "").strip()

        note_id = ctx.db.add_note(ctx.user_id, f"[{topic}] {text}" if topic != "general" else text)

        notes_dir = _notes_dir(ctx.settings)
        note_file = notes_dir / f"{topic}.md"
        bullet = f"- {text}\n"

        if not note_file.exists():
            file_desc = desc or f"notes about {topic}"
            content = f"---\ndescription: {file_desc}\n---\n\n## {topic.title()}\n{bullet}"
            note_file.write_text(content, encoding="utf-8")
        else:
            existing = note_file.read_text(encoding="utf-8")
            if desc and existing.startswith("---"):
                existing = re.sub(
                    r"^description:\s*.*$",
                    f"description: {desc}",
                    existing,
                    count=1,
                    flags=re.MULTILINE,
                )
            note_file.write_text(existing.rstrip() + f"\n{bullet}", encoding="utf-8")

        return f"saved note #{note_id} to notes/{topic}.md."

    async def _tool_recall(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can search memory")
        topic_arg = str(args.get("topic") or "").strip()
        query = str(args.get("query") or "").strip()

        if topic_arg:
            topic = _sanitize_topic(topic_arg)
            note_file = _notes_dir(ctx.settings) / f"{topic}.md"
            if note_file.exists():
                content = note_file.read_text(encoding="utf-8")[:MAX_TOOL_RESULT]
                return f"content of notes/{topic}.md:\n{content}"
            return f"no note file found for topic '{topic}'."

        markdown_hits: list[str] = []
        if query:
            q_lower = query.lower()
            for f in _list_note_files(ctx.settings):
                try:
                    for line in f.read_text(encoding="utf-8").splitlines():
                        if q_lower in line.lower():
                            clean_line = line.strip()
                            if clean_line:
                                markdown_hits.append(f"notes/{f.name}: {clean_line}")
                except (OSError, UnicodeDecodeError) as exc:
                    logger.debug("failed searching note %s: %s", f.name, exc)
            notes = ctx.db.search_notes(query, 10)
            turns = ctx.db.search_messages(query, 10)
        else:
            notes = ctx.db.recent_notes(10)
            turns = []

        index = _notes_index(ctx.settings)
        payload = {
            "notes_files": [f"- notes/{item['file']}: {item['description']}" for item in index],
            "markdown_matches": markdown_hits[:15],
            "notes": [{"id": row["id"], "text": row["text"], "at": row["created_at"]} for row in notes],
            "conversations": [
                {
                    "conversation": row["conversation"],
                    "role": row["role"],
                    "text": row["text"][:600],
                    "at": row["created_at"],
                }
                for row in turns
            ],
        }
        if (
            not payload["notes_files"]
            and not payload["markdown_matches"]
            and not payload["notes"]
            and not payload["conversations"]
        ):
            return "nothing found in memory."
        return _json(payload)

    async def _tool_post_message(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can post messages")
        channel_value = str(args.get("channel") or "here").strip()
        text = str(args.get("text") or "").strip()
        as_user = bool(args.get("as_user", False))
        if not text:
            return _error("message text is required")
        channel_id = await self._resolve_channel(ctx, channel_value)
        if not channel_id:
            return _error(f"could not resolve channel {channel_value!r}")
        target_client, err = self._get_target_client(ctx, as_user)
        if err or not target_client:
            return _error(err or "slack client not available")
        result = await post(target_client, channel_id, disarm_broadcasts(format_mrkdwn(text)))
        if not result:
            return _error("slack rejected the message")
        sender = "Boi (user account)" if as_user else "Bot app"
        return f"posted to <#{channel_id}> (ts {result.get('ts')}) as {sender}."

    async def _tool_slack_status(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can manage Slack status")
        if ctx.user_client is None:
            return _error("user client is not configured (needs SLACK_USER_TOKEN)")
        action = str(args.get("action") or "get").lower().strip()
        if action == "get":
            try:
                resp = await ctx.user_client.users_profile_get()
                profile = resp.get("profile") or {}
                return _json(
                    {
                        "status_text": profile.get("status_text") or "",
                        "status_emoji": profile.get("status_emoji") or "",
                        "status_expiration": profile.get("status_expiration") or 0,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                return _error(f"failed to get status: {exc}")
        if action == "set":
            text = str(args.get("text") or "").strip()
            emoji = str(args.get("emoji") or "").strip()
            if emoji and not (emoji.startswith(":") and emoji.endswith(":")):
                emoji = f":{emoji.strip(':')}:"
            exp = 0
            minutes = args.get("minutes")
            if minutes is not None:
                try:
                    mins = int(minutes)
                    if mins > 0:
                        exp = int(time.time()) + mins * 60
                except (TypeError, ValueError):
                    pass
            profile = {
                "status_text": text[:100],
                "status_emoji": emoji,
                "status_expiration": exp,
            }
            try:
                await ctx.user_client.users_profile_set(profile=profile)
                return f"updated Slack status: {emoji} {text}".strip()
            except Exception as exc:  # noqa: BLE001
                return _error(f"failed to set status: {exc}")
        if action == "clear":
            try:
                await ctx.user_client.users_profile_set(
                    profile={"status_text": "", "status_emoji": "", "status_expiration": 0}
                )
                return "cleared Slack status."
            except Exception as exc:  # noqa: BLE001
                return _error(f"failed to clear status: {exc}")
        return _error(f"unknown slack_status action: {action}")

    async def _tool_slack_dnd(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can manage DND")
        if ctx.user_client is None:
            return _error("user client is not configured (needs SLACK_USER_TOKEN)")
        action = str(args.get("action") or "info").lower().strip()
        if action == "info":
            try:
                resp = await ctx.user_client.dnd_info()
                return _json(
                    {
                        "dnd_enabled": resp.get("dnd_enabled"),
                        "snooze_enabled": resp.get("snooze_enabled"),
                        "snooze_endtime": resp.get("snooze_endtime"),
                        "snooze_remaining": resp.get("snooze_remaining"),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                return _error(f"failed to get dnd info: {exc}")
        if action == "snooze":
            try:
                minutes = int(args.get("minutes") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0:
                return _error("minutes must be a positive integer")
            try:
                await ctx.user_client.dnd_setSnooze(num_minutes=minutes)
                return f"snoozed notifications for {minutes} minute(s)."
            except Exception as exc:  # noqa: BLE001
                return _error(f"failed to set snooze: {exc}")
        if action == "end":
            try:
                await ctx.user_client.dnd_endSnooze()
                return "ended DND snooze."
            except Exception as exc:  # noqa: BLE001
                return _error(f"failed to end snooze: {exc}")
        return _error(f"unknown slack_dnd action: {action}")

    async def _tool_pending_access_requests(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can see access requests")
        rows = ctx.db.pending_access()
        if not rows:
            return "no pending access requests."
        return _json(
            [
                {
                    "id": row["id"],
                    "user": row["user_id"],
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        )

    async def _tool_get_stats(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can see workspace stats")
        return format_stats(ctx.db.stats())

    async def _tool_fetch_url(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can fetch urls")
        url = str(args.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return _error("only public http(s) urls are allowed")
        if await self._is_private_host(parsed.hostname):
            return _error("that host resolves to a private address")

        text_result = None
        try:
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                event_hooks={"request": [ssrf_filter]},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            ) as http:
                response = await http.get(url)
            if response.status_code < 400:
                final_host = response.url.host
                if not (final_host and await self._is_private_host(final_host)):
                    content_type = response.headers.get("content-type", "")
                    if "json" in content_type:
                        text_result = response.text[:MAX_TOOL_RESULT]
                    elif "html" in content_type or "text" in content_type:
                        parser = _TextExtractor()
                        parser.feed(response.text[:500_000])
                        extracted = html.unescape("".join(parser.parts))
                        cleaned = "\n".join(line.strip() for line in extracted.splitlines() if line.strip())
                        if len(cleaned) > 100:
                            text_result = cleaned[:MAX_TOOL_RESULT]
        except Exception:
            logger.debug("direct fetch failed for %s, trying exa contents", url)

        if text_result:
            return text_result

        # Fallback to Hack Club AI Exa contents for JS-rendered or bot-blocked pages
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.post(
                    f"{self.settings.ai_base_url}/exa/contents",
                    headers={"Authorization": f"Bearer {self.settings.ai_api_key}"},
                    json={"urls": [url], "text": {"maxCharacters": MAX_TOOL_RESULT}},
                )
                if response.status_code == 200:
                    data = response.json()
                    for r in data.get("results") or []:
                        text = (r.get("text") or "").strip()
                        if text:
                            title = (r.get("title") or "").strip()
                            prefix = f"# {title}\n\n" if title else ""
                            return f"{prefix}{text}"[:MAX_TOOL_RESULT]
        except Exception as exc:
            logger.debug("exa contents failed for %s: %s", url, exc)

        return _error(f"could not fetch or extract content from {url}")

    async def _tool_search_web(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can search the web")
        query = str(args.get("query") or "").strip()
        if not query:
            return _error("query is required")

        # 1. Primary: Hack Club AI Exa answer proxy
        try:
            async with httpx.AsyncClient(timeout=25) as http:
                response = await http.post(
                    f"{self.settings.ai_base_url}/exa/answer",
                    headers={"Authorization": f"Bearer {self.settings.ai_api_key}"},
                    json={"query": query},
                )
                if response.status_code == 200:
                    data = response.json()
                    answer = (data.get("answer") or "").strip()
                    if answer:
                        citations = [
                            c.get("url")
                            for c in (data.get("citations") or [])
                            if isinstance(c, dict) and c.get("url")
                        ]
                        if citations:
                            sources = "\n".join(f"- {u}" for u in citations[:5])
                            return f"{answer}\n\nsources:\n{sources}"
                        return answer
        except Exception:
            logger.exception("exa search failed, trying fallback")

        # 2. Tavily if key is configured
        if self.settings.tavily_api_key:
            try:
                async with httpx.AsyncClient(timeout=25) as http:
                    response = await http.post(
                        "https://api.tavily.com/search",
                        json={
                            "api_key": self.settings.tavily_api_key,
                            "query": query,
                            "search_depth": "basic",
                            "max_results": 5,
                            "include_answer": True,
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                return _json(
                    {
                        "answer": data.get("answer"),
                        "results": [
                            {"title": item.get("title"), "url": item.get("url"), "content": item.get("content", "")[:700]}
                            for item in (data.get("results") or [])
                        ],
                    }
                )
            except Exception:
                logger.exception("tavily search failed, trying fallback")

        # 3. Sonar on Hack Club AI
        try:
            async with httpx.AsyncClient(timeout=25) as http:
                response = await http.post(
                    f"{self.settings.ai_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.ai_api_key}"},
                    json={
                        "model": "perplexity/sonar",
                        "messages": [
                            {
                                "role": "system",
                                "content": "You are a concise web search assistant. Provide accurate, up-to-date facts and sources.",
                            },
                            {"role": "user", "content": query},
                        ],
                        "max_tokens": 600,
                        "temperature": 0.2,
                    },
                )
                response.raise_for_status()
                data = response.json()
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                return _json({"query": query, "answer": content})
        except Exception as exc:  # noqa: BLE001
            return _error(f"web search failed: {exc}")

    async def _tool_generate_image(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not image_access.can_generate(ctx.settings, ctx.db, ctx.user_id):
            return _error("you are not on the image allowlist; ask an admin to add you")
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return _error("prompt is required")
        ratio = str(args.get("ratio") or "1:1").strip()
        if ratio not in {"1:1", "16:9", "9:16", "4:3", "3:4"}:
            ratio = "1:1"
        if not ctx.channel_id:
            return _error("no channel or dm context to post the image to")
        from bot.features.ai_commands import _generate_image
        try:
            image_bytes, mime = await _generate_image(prompt, ratio)
        except Exception as exc:  # noqa: BLE001
            return _error(f"image generation failed: {exc}")
        ext = "png" if "png" in mime else "jpg"
        try:
            await ctx.client.files_upload_v2(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                file=image_bytes,
                filename=f"nugget-{int(time.time())}.{ext}",
                initial_comment=f":art: generated: *{prompt}*",
            )
            return f"image successfully generated and uploaded to conversation for: {prompt}"
        except Exception as exc:  # noqa: BLE001
            return _error(f"failed to upload image: {exc}")

    async def _tool_make_chart(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can render charts")
        if not ctx.channel_id:
            return _error("no channel or dm context to post the chart to")
        kind = str(args.get("type") or "bar").strip().lower()
        if kind not in {"bar", "line", "pie", "scatter", "hist"}:
            return _error("type must be bar, line, pie, scatter or hist")
        title = str(args.get("title") or "").strip()[:160]
        labels = [str(item)[:60] for item in (args.get("labels") or [])]
        values = _numbers(args.get("values"))
        series = [
            (str(item.get("name") or f"series {i + 1}")[:60], _numbers(item.get("values")))
            for i, item in enumerate(args.get("series") or [])
            if isinstance(item, dict)
        ]
        if not series and values:
            series = [("", values)]
        if not series:
            return _error("values or series is required")
        if len(series) == 1 and labels and len(labels) != len(series[0][1]):
            return _error("labels and values must be the same length")

        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            fig, ax = plt.subplots(figsize=(8, 4.5), dpi=160)
            fig.patch.set_facecolor("#ffffff")
            ax.set_facecolor("#ffffff")
            ax.grid(True, axis="y", alpha=0.25, linewidth=0.8)
            ax.spines[["top", "right"]].set_visible(False)

            if kind == "pie":
                name, data = series[0]
                if len(labels) != len(data):
                    return _error("pie charts need one label per value")
                ax.pie(data, labels=labels, autopct="%1.0f%%", startangle=90)
                ax.axis("equal")
            elif kind == "hist":
                for name, data in series:
                    if data:
                        ax.hist(data, bins=min(20, max(4, len(data) // 2)), label=name or None, alpha=0.75)
            elif kind == "scatter":
                x = labels or [str(i + 1) for i in range(len(series[0][1]))]
                for name, data in series:
                    ax.scatter(x[: len(data)], data, label=name or None)
            else:
                x = labels or [str(i + 1) for i in range(max(len(s) for _, s in series))]
                for name, data in series:
                    if kind == "line":
                        ax.plot(x[: len(data)], data, marker="o", linewidth=2, label=name or None)
                    else:
                        ax.bar(x[: len(data)], data, label=name or None)
                if len(series) > 1:
                    ax.legend(frameon=False)
                ax.tick_params(axis="x", labelrotation=30)

            if kind != "pie":
                if title:
                    ax.set_title(title, loc="left", fontsize=13, fontweight="bold")
                if args.get("x_label"):
                    ax.set_xlabel(str(args["x_label"])[:80])
                if args.get("y_label"):
                    ax.set_ylabel(str(args["y_label"])[:80])
            elif title:
                ax.set_title(title, loc="left", fontsize=13, fontweight="bold")

            fig.tight_layout()
            buf = io.BytesIO()
            fig.savefig(buf, format="png")
            plt.close(fig)
        except Exception as exc:  # noqa: BLE001
            return _error(f"chart rendering failed: {exc}")

        try:
            await ctx.client.files_upload_v2(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                file=buf.getvalue(),
                filename=f"chart-{int(time.time())}.png",
                initial_comment=f":bar_chart: {title}" if title else ":bar_chart: chart",
            )
        except Exception as exc:  # noqa: BLE001
            return _error(f"failed to upload chart: {exc}")
        return f"chart uploaded ({kind})" + (f": {title}" if title else "")

    async def _tool_make_deck(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can build decks")
        if not ctx.channel_id:
            return _error("no channel or dm context to post the deck to")
        title = str(args.get("title") or "").strip()[:140]
        if not title:
            return _error("title is required")
        subtitle = str(args.get("subtitle") or "").strip()[:180]
        raw_slides = [item for item in (args.get("slides") or []) if isinstance(item, dict)]
        if not raw_slides:
            return _error("at least one slide is required")
        if len(raw_slides) > 30:
            return _error("that is more than 30 slides; split it into two decks")

        try:
            from pptx import Presentation
            from pptx.util import Pt

            prs = Presentation()
            cover = prs.slides.add_slide(prs.slide_layouts[0])
            cover.shapes.title.text = title
            if subtitle and len(cover.placeholders) > 1:
                cover.placeholders[1].text = subtitle

            for item in raw_slides:
                slide = prs.slides.add_slide(prs.slide_layouts[1])
                slide.shapes.title.text = str(item.get("title") or "untitled")[:140]
                bullets = [str(b)[:400] for b in (item.get("bullets") or [])][:12]
                body = slide.placeholders[1].text_frame
                body.clear()
                for i, bullet in enumerate(bullets):
                    paragraph = body.paragraphs[0] if i == 0 else body.add_paragraph()
                    paragraph.text = bullet
                    paragraph.level = 0
                    paragraph.font.size = Pt(20)
                notes = str(item.get("notes") or "").strip()
                if notes:
                    slide.notes_slide.notes_text_frame.text = notes[:4000]

            buf = io.BytesIO()
            prs.save(buf)
        except Exception as exc:  # noqa: BLE001
            return _error(f"deck build failed: {exc}")

        try:
            await ctx.client.files_upload_v2(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                file=buf.getvalue(),
                filename=f"deck-{int(time.time())}.pptx",
                initial_comment=f":bar_chart: {title} ({len(raw_slides) + 1} slides)",
            )
        except Exception as exc:  # noqa: BLE001
            return _error(f"failed to upload deck: {exc}")
        return f"deck uploaded: {title} ({len(raw_slides) + 1} slides)"

    async def _tool_flaron(self, args: dict[str, Any], ctx: ToolContext) -> str:
        action = str(args.get("action") or "").lower().strip()
        item_id = str(args.get("id") or "").strip()
        name = str(args.get("name") or "").strip()
        query = str(args.get("query") or "").strip()

        path_map = {
            "user": f"/user/{quote(item_id)}" if item_id else None,
            "channel": f"/channel/{quote(item_id)}" if item_id else None,
            "user_search": f"/users/search?{urlencode({'q': query})}" if query else None,
            "channel_search": f"/channels/search?{urlencode({'q': query})}" if query else None,
            "channel_by_name": f"/cname/{quote(name)}" if name else None,
            "channel_managers": f"/cman/{quote(item_id)}" if item_id else None,
            "channel_members": f"/ccount/{quote(item_id)}" if item_id else None,
            "app": f"/app/{quote(item_id)}" if item_id else None,
            "emoji": f"/emoji/{quote(name)}" if name else None,
            "command": f"/command/{quote(name)}" if name else None,
            "promote": f"/promote/{quote(item_id)}" if item_id else None,
        }
        path = path_map.get(action)
        if not path:
            return _error(f"invalid action {action!r} or missing required id/name/query parameter")

        url = f"https://flaron.halceon.dev{path}"
        try:
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "chicen-nugget-bot"}) as http:
                resp = await http.get(url)
                if resp.status_code == 404:
                    return f"no flaron data found for {action} ({item_id or name or query})"
                resp.raise_for_status()
                return _json(resp.json())
        except Exception as exc:  # noqa: BLE001
            return _error(f"flaron lookup failed: {exc}")

    async def _tool_fetch_slack_message(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can fetch slack messages")
        ref = str(args.get("ref") or "").strip()
        if not ref:
            return _error("ref is required")
        cid, ts = parse_slack_ref(ref)
        if not cid:
            return _error("could not parse a slack channel or link from reference")
        if not ts:
            return _error(f"found channel <#{cid}> but no specific message timestamp")
        client = ctx.client
        if cid.startswith("D") and ctx.user_client and client is not ctx.user_client:
            client = ctx.user_client
        try:
            resp = await client.conversations_history(channel=cid, latest=ts, inclusive=True, limit=1)
            msgs = (resp.get("messages") or []) if resp.get("ok") else []
            if not msgs:
                resp2 = await client.conversations_replies(channel=cid, ts=ts, limit=1)
                msgs = (resp2.get("messages") or []) if resp2.get("ok") else []
        except Exception:
            alt = ctx.user_client if client is not ctx.user_client else (ctx.bot_client or ctx.client)
            if alt and alt is not client:
                try:
                    resp = await alt.conversations_history(channel=cid, latest=ts, inclusive=True, limit=1)
                    msgs = (resp.get("messages") or []) if resp.get("ok") else []
                    if not msgs:
                        resp2 = await alt.conversations_replies(channel=cid, ts=ts, limit=1)
                        msgs = (resp2.get("messages") or []) if resp2.get("ok") else []
                except Exception as exc:  # noqa: BLE001
                    return _error(f"failed to fetch slack message: {exc}")
            else:
                return _error(f"failed to fetch slack message: {exc}")
        if not msgs:
            return f"no message found in <#{cid}> at {ts}"
        msg = msgs[0]
        user = msg.get("user") or msg.get("bot_id") or "unknown"
        text = (msg.get("text") or "").strip()
        return f"message in <#{cid}> from <@{user}> at {ts}:\n{text}"

    async def _tool_summarize_thread(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can summarize threads")
        ref = str(args.get("ref") or "").strip()
        if ref:
            cid, ts = parse_slack_thread_ref(ref)
        elif ctx.channel_id and ctx.thread_ts:
            cid, ts = ctx.channel_id, ctx.thread_ts
        else:
            return _error("no thread specified and not currently inside a thread")
        if not cid or not ts:
            return _error("could not parse channel and thread timestamp")
        client = ctx.client
        if cid.startswith("D") and ctx.user_client and client is not ctx.user_client:
            client = ctx.user_client
        try:
            resp = await client.conversations_replies(channel=cid, ts=ts, limit=100)
            msgs = (resp.get("messages") or []) if resp.get("ok") else []
        except Exception:
            alt = ctx.user_client if client is not ctx.user_client else (ctx.bot_client or ctx.client)
            if alt and alt is not client:
                try:
                    resp = await alt.conversations_replies(channel=cid, ts=ts, limit=100)
                    msgs = (resp.get("messages") or []) if resp.get("ok") else []
                except Exception as exc:  # noqa: BLE001
                    return _error(f"failed to read thread: {exc}")
            else:
                return _error(f"failed to read thread: {exc}")
        if not msgs:
            return "thread has no messages or could not be read"
        tz = tzinfo()
        lines = []
        for m in msgs:
            u = m.get("user") or m.get("bot_id") or "unknown"
            t = (m.get("text") or "").strip()
            try:
                when = datetime.fromtimestamp(float(m.get("ts")), tz).strftime("%H:%M")
            except Exception:
                when = "?"
            lines.append(f"<@{u}> ({when}): {t}")
        return f"thread in <#{cid}> ({len(msgs)} messages):\n" + "\n".join(lines)[:MAX_TOOL_RESULT]

    async def _tool_ocr_document(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can use OCR")
        url = str(args.get("url") or "").strip()
        if not url:
            return _error("url is required")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return _error("only public http(s) urls are allowed")
        if await self._is_private_host(parsed.hostname):
            return _error("that host resolves to a private address")
        doc_type = "document_url" if parsed.path.lower().endswith(".pdf") else "image_url"
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.post(
                    f"{self.settings.ai_base_url}/ocr",
                    headers={"Authorization": f"Bearer {self.settings.ai_api_key}"},
                    json={
                        "model": "mistral-ocr-latest",
                        "document": {"type": doc_type, doc_type: url},
                    },
                )
                if response.status_code != 200:
                    return _error(f"ocr failed with http {response.status_code}: {response.text[:200]}")
                data = response.json()
                pages = [
                    (p.get("markdown") or "").strip()
                    for p in (data.get("pages") or [])
                    if isinstance(p, dict)
                ]
                text = "\n\n--- page ---\n\n".join(p for p in pages if p)
                if not text:
                    return "ocr returned no readable text"
                return text[:MAX_TOOL_RESULT]
        except Exception as exc:  # noqa: BLE001
            return _error(f"ocr failed: {exc}")

    async def _tool_public_holidays(self, args: dict[str, Any], ctx: ToolContext) -> str:
        country = str(args.get("country") or "US").strip().upper()
        if not re.match(r"^[A-Z]{2}$", country):
            country = "US"
        year = args.get("year")
        if year:
            try:
                year_int = int(year)
                api_url = f"https://date.nager.at/api/v3/PublicHolidays/{year_int}/{country}"
            except (ValueError, TypeError):
                api_url = f"https://date.nager.at/api/v3/NextPublicHolidays/{country}"
        else:
            api_url = f"https://date.nager.at/api/v3/NextPublicHolidays/{country}"
        try:
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "chicen-nugget-bot"}) as http:
                resp = await http.get(api_url)
                if resp.status_code == 404:
                    return f"no holiday data found for country code {country}"
                resp.raise_for_status()
                data = resp.json()
            if not isinstance(data, list) or not data:
                return f"no public holidays found for {country}"
            lines = [f"public holidays for {country}:"]
            for h in data[:15]:
                date_str = h.get("date")
                name = h.get("name")
                local_name = h.get("localName")
                label = f"{name} ({local_name})" if local_name and local_name != name else name
                lines.append(f"- {date_str}: {label}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return _error(f"failed to fetch holidays: {exc}")

    async def _tool_check_emails(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can check emails")
        host = self.settings.imap_host
        user = self.settings.imap_user
        password = self.settings.imap_password
        port = self.settings.imap_port or 993
        if not host or not user or not password:
            return (
                "email credentials not configured. "
                "Set IMAP_HOST, IMAP_USER, and IMAP_PASSWORD in .env (for Gmail/Fastmail, use an App Password)."
            )
        action = str(args.get("action") or "unread").lower().strip()
        query = str(args.get("query") or "").strip()
        email_id = str(args.get("email_id") or "").strip()
        folder = str(args.get("folder") or "INBOX").strip()
        limit = max(1, min(20, int(args.get("limit") or 5)))
        return await asyncio.to_thread(
            _sync_imap_op, host, port, user, password, folder, action, query, email_id, limit
        )

    async def _tool_hackatime_stats(self, args: dict[str, Any], ctx: ToolContext) -> str:
        username = str(
            args.get("username") or ctx.settings.github_username or ""
        ).strip()
        if not username:
            return _error("username is required; set GITHUB_USERNAME for a default")
        url = f"https://hackatime.hackclub.com/api/v1/users/{quote(username)}/stats"
        try:
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "chicen-nugget-bot"}) as http:
                resp = await http.get(url)
                if resp.status_code == 404:
                    return f"no hackatime profile found for {username}"
                resp.raise_for_status()
                data = resp.json().get("data") or {}
            out = {
                "username": data.get("username") or username,
                "total_time": data.get("human_readable_total"),
                "daily_average": data.get("human_readable_daily_average"),
                "streak": data.get("streak"),
                "languages": [f"{l['name']}: {l.get('text', '')}" for l in (data.get("languages") or [])[:5]],
            }
            return _json(out)
        except Exception as exc:  # noqa: BLE001
            return _error(f"failed to fetch hackatime stats: {exc}")

    async def _tool_github_info(self, args: dict[str, Any], ctx: ToolContext) -> str:
        action = str(args.get("action") or "repo").lower().strip()
        target = str(args.get("target") or _default_repo(ctx.settings)).strip()
        if not target:
            if action == "user":
                return _error("username is required for action='user'")
            return _error("target is required; set REPO_URL for a default repository")
        state = str(args.get("state") or "open").lower().strip()
        if state not in {"open", "closed", "all"}:
            state = "open"
        headers = {"User-Agent": "chicen-nugget-bot", "Accept": "application/vnd.github+json"}
        token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(timeout=12, headers=headers, follow_redirects=True) as http:
                if action == "user":
                    resp = await http.get(f"https://api.github.com/users/{quote(target)}")
                    if resp.status_code == 404:
                        return f"github user '{target}' not found"
                    resp.raise_for_status()
                    u = resp.json()
                    return _json({
                        "login": u.get("login"),
                        "name": u.get("name"),
                        "bio": u.get("bio"),
                        "public_repos": u.get("public_repos"),
                        "followers": u.get("followers"),
                        "url": u.get("html_url"),
                    })
                if action == "commits":
                    resp = await http.get(f"https://api.github.com/repos/{target}/commits?per_page=5")
                    if resp.status_code == 404:
                        return f"github repo '{target}' not found"
                    resp.raise_for_status()
                    commits = [
                        {
                            "sha": c["sha"][:7],
                            "author": (c.get("commit", {}).get("author", {}) or {}).get("name"),
                            "message": (c.get("commit", {}).get("message") or "").splitlines()[0],
                        }
                        for c in (resp.json() or [])[:5]
                    ]
                    return _json(commits)
                if action == "release":
                    resp = await http.get(f"https://api.github.com/repos/{target}/releases/latest")
                    if resp.status_code == 404:
                        return f"no releases found for '{target}'"
                    resp.raise_for_status()
                    rel = resp.json()
                    return _json({
                        "tag": rel.get("tag_name"),
                        "name": rel.get("name"),
                        "published_at": rel.get("published_at"),
                        "body": (rel.get("body") or "")[:500],
                    })
                if action == "issues":
                    resp = await http.get(f"https://api.github.com/repos/{target}/issues?state={state}&per_page=10")
                    if resp.status_code == 404:
                        return f"github repo '{target}' not found"
                    resp.raise_for_status()
                    issues = [
                        {
                            "number": i["number"],
                            "title": i.get("title"),
                            "author": (i.get("user") or {}).get("login"),
                            "state": i.get("state"),
                            "comments": i.get("comments", 0),
                            "labels": [l.get("name") for l in i.get("labels", []) if isinstance(l, dict)],
                            "created_at": i.get("created_at"),
                            "url": i.get("html_url"),
                        }
                        for i in (resp.json() or [])
                        if isinstance(i, dict) and "pull_request" not in i
                    ][:5]
                    return _json(issues) if issues else f"no {state} issues found for '{target}'"
                if action == "prs":
                    resp = await http.get(f"https://api.github.com/repos/{target}/pulls?state={state}&per_page=5")
                    if resp.status_code == 404:
                        return f"github repo '{target}' not found"
                    resp.raise_for_status()
                    prs = [
                        {
                            "number": p["number"],
                            "title": p.get("title"),
                            "author": (p.get("user") or {}).get("login"),
                            "state": p.get("state"),
                            "draft": p.get("draft", False),
                            "branch": f"{(p.get('head') or {}).get('ref')} -> {(p.get('base') or {}).get('ref')}",
                            "created_at": p.get("created_at"),
                            "url": p.get("html_url"),
                        }
                        for p in (resp.json() or [])
                        if isinstance(p, dict)
                    ][:5]
                    return _json(prs) if prs else f"no {state} pull requests found for '{target}'"
                if action == "issue":
                    repo = target
                    number = args.get("number")
                    if "#" in target:
                        repo, _, num_str = target.partition("#")
                        try:
                            number = int(num_str)
                        except ValueError:
                            pass
                    if not number:
                        return _error("issue number is required for action='issue'")
                    resp = await http.get(f"https://api.github.com/repos/{repo}/issues/{number}")
                    if resp.status_code == 404:
                        return f"issue #{number} not found in '{repo}'"
                    resp.raise_for_status()
                    it = resp.json()
                    return _json({
                        "number": it.get("number"),
                        "title": it.get("title"),
                        "author": (it.get("user") or {}).get("login"),
                        "state": it.get("state"),
                        "labels": [l.get("name") for l in it.get("labels", []) if isinstance(l, dict)],
                        "comments": it.get("comments", 0),
                        "created_at": it.get("created_at"),
                        "body": (it.get("body") or "")[:500],
                        "url": it.get("html_url"),
                    })
                # default action == "repo"
                resp = await http.get(f"https://api.github.com/repos/{target}")
                if resp.status_code == 404:
                    return f"github repo '{target}' not found"
                resp.raise_for_status()
                r = resp.json()
                return _json({
                    "full_name": r.get("full_name"),
                    "description": r.get("description"),
                    "stars": r.get("stargazers_count"),
                    "forks": r.get("forks_count"),
                    "open_issues": r.get("open_issues_count"),
                    "language": r.get("language"),
                    "default_branch": r.get("default_branch"),
                    "updated_at": r.get("updated_at"),
                })
        except Exception as exc:  # noqa: BLE001
            return _error(f"github api error: {exc}")

    async def _tool_run_code(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can run code")
        if not ctx.is_dm:
            return _error("run_code is only permitted in direct messages")
        code = str(args.get("code") or "").strip()
        if not code:
            return _error("code is required")
        # ponytail: for multi-tenant code execution, upgrade to Sandbox SDK or gVisor container.
        safe_env = {
            k: v for k, v in os.environ.items()
            if not any(s in k.upper() for s in ("TOKEN", "KEY", "SECRET", "PASS", "AUTH", "SSH_"))
        }
        safe_env["PYTHONPATH"] = ""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-c",
                code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=safe_env,
                cwd=tempfile.gettempdir(),
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            stdout = stdout_bytes.decode(errors="replace").strip()
            stderr = stderr_bytes.decode(errors="replace").strip()
            if proc.returncode != 0:
                err_msg = stderr or stdout or f"exited with code {proc.returncode}"
                return f"execution failed (exit {proc.returncode}):\n{err_msg}"[:MAX_TOOL_RESULT]
            output = stdout or "(finished with no output)"
            return output[:MAX_TOOL_RESULT]
        except TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return _error("code execution timed out after 5.0 seconds")
        except Exception as exc:  # noqa: BLE001
            return _error(f"execution failed: {exc}")

    async def _tool_inspect_code(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can inspect local code")
        action = str(args.get("action") or "read").lower().strip()
        repo_root = Path(__file__).resolve().parents[2]
        rel_path = str(args.get("path") or "").strip().lstrip("/")
        target_path = (repo_root / rel_path).resolve() if rel_path else repo_root

        try:
            target_path.relative_to(repo_root)
        except ValueError:
            return _error("access denied: path escapes repository root")

        forbidden_names = {".env", "id_rsa", "id_ed25519"}
        forbidden_exts = {".pem", ".key", ".db", ".sqlite", ".sqlite3"}

        def is_forbidden(p: Path) -> bool:
            name = p.name.lower()
            if name in forbidden_names or name.startswith(".env"):
                return True
            if p.suffix.lower() in forbidden_exts:
                return True
            return False

        if action == "list":
            if not target_path.exists():
                return _error(f"path not found: {rel_path}")
            if target_path.is_file():
                if is_forbidden(target_path):
                    return _error("access denied: sensitive file")
                return f"file: {target_path.relative_to(repo_root)} ({target_path.stat().st_size} bytes)"
            entries = []
            try:
                for item in sorted(target_path.iterdir()):
                    if item.name.startswith(".") and item.name in {".git", ".pytest_cache", ".mypy_cache"}:
                        continue
                    if item.name in {"__pycache__", ".venv", "venv", "node_modules"}:
                        continue
                    if is_forbidden(item):
                        continue
                    suffix = "/" if item.is_dir() else ""
                    entries.append(f"{item.relative_to(repo_root)}{suffix}")
            except Exception as exc:
                return _error(f"failed to list directory: {exc}")
            return "\n".join(entries[:150])[:MAX_TOOL_RESULT] or "(empty directory)"

        if action == "read":
            if not target_path.exists() or not target_path.is_file():
                return _error(f"file not found: {rel_path}")
            if is_forbidden(target_path):
                return _error("access denied: sensitive file")
            try:
                offset = max(1, int(args.get("offset") or 1))
                limit = max(1, min(500, int(args.get("limit") or 150)))
                lines = target_path.read_text(encoding="utf-8", errors="replace").splitlines()
                slice_lines = lines[offset - 1 : offset - 1 + limit]
                formatted = [f"{i}: {line}" for i, line in enumerate(slice_lines, start=offset)]
                return "\n".join(formatted)[:MAX_TOOL_RESULT]
            except Exception as exc:
                return _error(f"failed to read file: {exc}")

        if action == "search":
            pattern = str(args.get("pattern") or "").strip()
            if not pattern:
                return _error("pattern is required for search")
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error:
                rx = re.compile(re.escape(pattern), re.IGNORECASE)
            matches = []
            try:
                for p in repo_root.rglob("*"):
                    if not p.is_file() or p.is_symlink() or is_forbidden(p):
                        continue
                    rel = p.relative_to(repo_root)
                    parts = set(rel.parts)
                    if any(bad in parts for bad in {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "data"}):
                        continue
                    if p.stat().st_size > 512_000:
                        continue
                    try:
                        content_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                        for idx, line in enumerate(content_lines, 1):
                            if rx.search(line):
                                matches.append(f"{rel}:{idx}: {line.strip()}")
                                if len(matches) >= 60:
                                    break
                    except Exception:
                        continue
                    if len(matches) >= 60:
                        break
            except Exception as exc:
                return _error(f"search failed: {exc}")
            if not matches:
                return f"no matches for pattern: {pattern}"
            return "\n".join(matches)[:MAX_TOOL_RESULT]

        return _error(f"unknown action: {action}")

    async def _tool_clone_repo(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can clone repositories")
        raw_url = str(args.get("repo_url") or "").strip()
        if not raw_url:
            return _error("repo_url is required")
        if raw_url.startswith("-") or any(ch in raw_url for ch in (";", "&", "|", "`", "$", "\n")):
            return _error("invalid repo_url")
        if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", raw_url):
            clone_url = f"https://github.com/{raw_url}.git"
        elif raw_url.startswith(("https://", "http://")):
            parsed = urlparse(raw_url)
            if not parsed.hostname or await self._is_private_host(parsed.hostname):
                return _error("invalid or private repository host")
            clone_url = raw_url
        else:
            return _error("only https:// or 'owner/repo' GitHub URLs are allowed")

        action = str(args.get("action") or "list").lower().strip()
        # ponytail: sandbox cloning uses temporary directory; upgrade to container sandbox for untrusted git hooks/LFS.
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = (Path(tmpdir) / "repo").resolve()
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "-c",
                    "core.symlinks=false",
                    "-c",
                    "protocol.file.allow=never",
                    "clone",
                    "--depth",
                    "1",
                    "--single-branch",
                    clone_url,
                    str(dest),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=25.0)
            except TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return _error("git clone timed out after 25s")
            except Exception as exc:
                return _error(f"failed to run git clone: {exc}")

            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip() or f"git clone exited with code {proc.returncode}"
                return _error(f"clone failed: {err}")

            rel_path = str(args.get("path") or "").strip().lstrip("/")
            target = (dest / rel_path).resolve() if rel_path else dest
            try:
                target.relative_to(dest.resolve())
            except ValueError:
                return _error("access denied: path escapes cloned repository")

            if action == "list":
                if not target.exists():
                    return _error(f"path not found: {rel_path}")
                if target.is_file():
                    return f"file: {target.relative_to(dest)} ({target.stat().st_size} bytes)"
                entries = []
                for item in sorted(target.iterdir()):
                    if item.name == ".git":
                        continue
                    suffix = "/" if item.is_dir() else ""
                    entries.append(f"{item.relative_to(dest)}{suffix}")
                return "\n".join(entries[:150])[:MAX_TOOL_RESULT] or "(empty directory)"

            if action == "read":
                if not target.exists() or not target.is_file():
                    return _error(f"file not found: {rel_path}")
                try:
                    offset = max(1, int(args.get("offset") or 1))
                    limit = max(1, min(500, int(args.get("limit") or 150)))
                    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
                    slice_lines = lines[offset - 1 : offset - 1 + limit]
                    formatted = [f"{i}: {line}" for i, line in enumerate(slice_lines, start=offset)]
                    return "\n".join(formatted)[:MAX_TOOL_RESULT]
                except Exception as exc:
                    return _error(f"failed to read file: {exc}")

            if action == "search":
                pattern = str(args.get("pattern") or "").strip()
                if not pattern:
                    return _error("pattern is required for search")
                try:
                    rx = re.compile(pattern, re.IGNORECASE)
                except re.error:
                    rx = re.compile(re.escape(pattern), re.IGNORECASE)
                matches = []
                for p in dest.rglob("*"):
                    if not p.is_file() or p.is_symlink() or ".git" in p.parts:
                        continue
                    if p.stat().st_size > 512_000:
                        continue
                    rel = p.relative_to(dest)
                    try:
                        content_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                        for idx, line in enumerate(content_lines, 1):
                            if rx.search(line):
                                matches.append(f"{rel}:{idx}: {line.strip()}")
                                if len(matches) >= 60:
                                    break
                    except Exception:
                        continue
                    if len(matches) >= 60:
                        break
                if not matches:
                    return f"no matches for pattern: {pattern}"
                return "\n".join(matches)[:MAX_TOOL_RESULT]

            return _error(f"unknown action: {action}")

    async def _tool_hacker_news(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can browse hacker news")
        category = str(args.get("category") or "top").lower().strip()
        if category not in {"top", "new", "best", "ask", "show"}:
            category = "top"
        limit = max(1, min(10, int(args.get("limit") or 5)))
        try:
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "chicen-nugget-bot"}) as http:
                resp = await http.get(f"https://hacker-news.firebaseio.com/v0/{category}stories.json")
                resp.raise_for_status()
                ids = (resp.json() or [])[:limit]
                tasks = [http.get(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json") for item_id in ids]
                item_resps = await asyncio.gather(*tasks, return_exceptions=True)
                stories = []
                for res in item_resps:
                    if isinstance(res, httpx.Response) and res.status_code == 200:
                        d = res.json()
                        if d and not d.get("deleted"):
                            stories.append({
                                "title": d.get("title"),
                                "score": d.get("score"),
                                "by": d.get("by"),
                                "url": d.get("url") or f"https://news.ycombinator.com/item?id={d.get('id')}",
                                "comments_url": f"https://news.ycombinator.com/item?id={d.get('id')}",
                                "descendants": d.get("descendants", 0),
                            })
                return _json(stories)
        except Exception as exc:  # noqa: BLE001
            return _error(f"failed to fetch hacker news: {exc}")

    async def _tool_http_status(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can inspect http status")
        raw = str(args.get("url") or "").strip()
        if not raw:
            return _error("url is required")
        if not raw.startswith(("http://", "https://")):
            raw = f"https://{raw}"
        parsed = urlparse(raw)
        if not parsed.hostname:
            return _error("invalid url or hostname")
        if await self._is_private_host(parsed.hostname):
            return _error("private IP addresses are blocked")
        try:
            start = time.perf_counter()
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                event_hooks={"request": [ssrf_filter]},
                headers={"User-Agent": "chicen-nugget-bot/1.0"},
            ) as http:
                resp = await http.get(raw)
            duration_ms = round((time.perf_counter() - start) * 1000)
            return _json({
                "url": raw,
                "final_url": str(resp.url),
                "status_code": resp.status_code,
                "latency_ms": duration_ms,
                "server": resp.headers.get("server"),
                "content_type": resp.headers.get("content-type"),
                "content_length": resp.headers.get("content-length") or len(resp.content),
                "redirects": len(resp.history),
            })
        except Exception as exc:  # noqa: BLE001
            return _error(f"http check failed: {exc}")

    async def _tool_dog_fact(self, args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "chicen-nugget-bot"}) as http:
                resp = await http.get("https://dogapi.dog/api/v2/facts")
                resp.raise_for_status()
                data = resp.json()
                facts = data.get("data") or []
                if facts:
                    fact = facts[0].get("attributes", {}).get("body", "")
                    if fact:
                        return f":dog: dog fact: {fact}"
            return "dogs are loyal, intelligent, and awesome companions."
        except Exception as exc:  # noqa: BLE001
            return _error(f"failed to fetch dog fact: {exc}")

    async def _tool_taco_recipe(self, args: dict[str, Any], ctx: ToolContext) -> str:
        theme = str(args.get("theme") or "").lower().strip()
        shells = [
            "charred white corn tortilla",
            "crispy yellow corn shell",
            "warm handmade flour tortilla",
            "parmesan cheese shell",
            "crispy blue corn tortilla",
        ]
        proteins = {
            "spicy": ("Ghost Pepper Barbacoa", "slow-braised chipotle shredded beef infused with roasted ghost pepper"),
            "breakfast": ("Morning Chorizo Crunch", "crispy chorizo, fluffy scrambled eggs, diced hash browns"),
            "birria": ("Guajillo Birria de Res", "slow-cooked beef shank bathed in dried chiles with a cup of rich consommé for dipping"),
            "crispy": ("Baja Crispy Beer-Battered Cod", "crispy golden fish fillet with sea salt and fresh Mexican oregano"),
            "vegan": ("Charred Adobo Jackfruit & Black Bean", "smoky shredded jackfruit simmered in citrus adobo and cumin black beans"),
            "street": ("Classic Citrus Carnitas", "pork shoulder confited in lard with orange peel, mexican cinnamon, and bay leaves"),
        }
        name, filling = proteins.get(theme, random.choice(list(proteins.values())))
        salsas = [
            "Charred Habanero & Pineapple Salsa",
            "Roasted Tomatillo Salsa Verde",
            "Creamy Chipotle Crema",
            "Smoky Salsa Roja",
            "Avocado Cilantro Crema",
        ]
        toppings = [
            "finely diced white onion & cilantro",
            "crumbled cotija cheese",
            "pickled red onions",
            "radish wheels",
            "crushed chicharrones",
            "squeeze of fresh key lime",
        ]
        chosen_shell = random.choice(shells)
        chosen_salsa = random.choice(salsas)
        chosen_toppings = random.sample(toppings, 3)
        return (
            f":taco: *{name}*\n"
            f"- *Shell:* {chosen_shell}\n"
            f"- *Filling:* {filling}\n"
            f"- *Salsa:* {chosen_salsa}\n"
            f"- *Toppings:* {', '.join(chosen_toppings)}\n"
            f"- *Pro Tip:* Toast your tortillas on a smoking hot comal with a splash of beef fat or lime juice."
        )

    async def _tool_urban_dict(self, args: dict[str, Any], ctx: ToolContext) -> str:
        term = str(args.get("term") or "").strip()
        if not term:
            return _error("term is required")
        try:
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "chicen-nugget-bot"}) as http:
                resp = await http.get(f"https://api.urbandictionary.com/v0/define?term={quote(term)}")
                resp.raise_for_status()
                data = resp.json()
            entries = data.get("list") or []
            if not entries:
                return f"no urban dictionary definitions found for '{term}'"
            top = entries[0]
            defn = re.sub(r"[\[\]]", "", top.get("definition") or "")
            example = re.sub(r"[\[\]]", "", top.get("example") or "")
            thumbs_up = top.get("thumbs_up", 0)
            thumbs_down = top.get("thumbs_down", 0)
            res = f"*Urban Dictionary: {top.get('word')}* (+{thumbs_up}/-{thumbs_down})\n\n{defn}"
            if example:
                res += f"\n\n*Example:*\n_{example}_"
            return res[:MAX_TOOL_RESULT]
        except Exception as exc:  # noqa: BLE001
            return _error(f"urban dictionary lookup failed: {exc}")

    async def _tool_image_access(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can manage image access")
        action = str(args.get("action") or "list").lower().strip()
        settings = ctx.settings
        env_users = sorted(settings.image_user_ids)
        runtime_users = sorted(image_access.extra(ctx.db))

        if action == "list":
            return _json(
                {
                    "from_env": env_users,
                    "added_at_runtime": runtime_users,
                    "admins": sorted(settings.admin_user_ids),
                }
            )

        raw = str(args.get("user") or "").strip()
        user_id = re.sub(r"[<@>]", "", raw).split("|")[0].strip()
        if not re.fullmatch(r"[UW][A-Z0-9]{6,}", user_id):
            return _error("pass a Slack user id or @mention")

        if action == "add":
            if settings.is_admin(user_id):
                return f"<@{user_id}> is already an admin and always has access"
            image_access.add(ctx.db, user_id)
            return f"added <@{user_id}> to the image allowlist"
        if action == "remove":
            image_access.remove(ctx.db, user_id)
            if settings.is_admin(user_id):
                return f"<@{user_id}> is an admin, so admin access still applies"
            return f"removed <@{user_id}> from the image allowlist"
        return _error("action must be list, add or remove")

    async def _tool_bot_config(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can view or change bot settings")
        action = str(args.get("action") or "list").lower().strip()
        raw_key = str(args.get("key") or "").strip().lower()
        value = str(args.get("value") or "").strip()

        key_aliases = {
            "recap": "recaps",
            "daily_recap": "recaps",
            "daily_recaps": "recaps",
            "digest": "daily_digest",
            "access": "tier2",
            "tier2_channel": "access_channel_id",
            "access_channel": "access_channel_id",
            "admin_channel": "admin_channel_id",
            "admin_chan": "admin_channel_id",
            "admin_channel_id": "admin_channel_id",
            "ambient": "ambient_ai",
            "weather": "weather_location",
            "updates": "personal_updates",
            "personal": "personal_updates",
            "threads": "thread_followups",
        }
        key = key_aliases.get(raw_key, raw_key)
        known_keys = [
            "recaps",
            "daily_digest",
            "welcomer",
            "tier2",
            "access_channel_id",
            "admin_channel_id",
            "ambient_ai",
            "thread_followups",
            "personal_updates",
            "weather_location",
        ]

        if action == "list":
            lines = ["current bot runtime settings:"]
            for k in known_keys:
                db_val = ctx.db.kv_get(f"config:{k}")
                status = db_val if db_val is not None else "(default)"
                lines.append(f"- *{k}*: `{status}`")
            return "\n".join(lines)

        if not key:
            return _error("key is required for get/set/reset")
        if key not in known_keys:
            return _error(f"unknown setting '{raw_key}'. valid settings: {', '.join(known_keys)}")

        if action == "get":
            db_val = ctx.db.kv_get(f"config:{key}")
            if db_val is None:
                return f"setting *{key}* is not set (using default)."
            return f"setting *{key}* is currently `{db_val}`."

        if action == "reset":
            ctx.db.kv_del(f"config:{key}")
            if key == "recaps":
                try:
                    from bot.features import scheduler
                    scheduler._ensure_recap_jobs()
                except Exception:
                    pass
            elif key == "daily_digest":
                try:
                    from bot.features import scheduler
                    scheduler._ensure_digest_job()
                except Exception:
                    pass
            return f"setting *{key}* reset to default."

        if action == "set":
            if not value:
                return _error("value is required when setting a config")

            val_lower = value.lower()
            if key == "admin_channel_id":
                if val_lower in {"dm", "dms", "none", "off", "false", "no", "disable", "disabled", "0"}:
                    normalized_val = "dm"
                else:
                    match = re.search(r"[CG][A-Z0-9]{8,}", value, re.IGNORECASE)
                    normalized_val = match.group(0).upper() if match else value.strip("#")
            elif val_lower in {"off", "false", "no", "disable", "disabled", "0"}:
                normalized_val = "false"
            elif val_lower in {"on", "true", "yes", "enable", "enabled", "1"}:
                normalized_val = "true"
            else:
                if key == "access_channel_id":
                    match = re.search(r"[CG][A-Z0-9]{8,}", value, re.IGNORECASE)
                    normalized_val = match.group(0).upper() if match else value.strip("#")
                else:
                    normalized_val = value

            ctx.db.kv_set(f"config:{key}", normalized_val)

            if key == "admin_channel_id":
                if normalized_val == "dm":
                    return "tier 2 access requests will route to admin DMs."
                return f"tier 2 access requests will route to <#{normalized_val}>."

            if key == "recaps":
                if normalized_val == "false":
                    ctx.db.cancel_reminders_by_kind("recap_public")
                    ctx.db.cancel_reminders_by_kind("recap_reminder")
                    return "daily recaps disabled. cancelled any pending recap reminders."
                try:
                    from bot.features import scheduler
                    scheduler._ensure_recap_jobs()
                except Exception:
                    pass
                return "daily recaps enabled."

            if key == "daily_digest":
                if normalized_val == "false":
                    ctx.db.cancel_reminders_by_kind("digest")
                    return "daily digest disabled. cancelled pending digest jobs."
                try:
                    from bot.features import scheduler
                    scheduler._ensure_digest_job()
                except Exception:
                    pass
                return "daily digest enabled."

            if key in {"personal_updates", "thread_followups"}:
                # Both schedulers only run at startup and on config changes.
                try:
                    from bot.features import scheduler
                    scheduler._ensure_digest_job()
                    scheduler._ensure_recap_jobs()
                except Exception:
                    pass
                if key == "personal_updates":
                    if normalized_val == "false":
                        return "personal updates off. no digest or recap reminder DMs; public recaps keep running."
                    return "personal updates on. digest and recap reminder DMs are back."
                if normalized_val == "false":
                    return "unprompted thread replies off. i'll only answer when tagged in a thread."
                return "unprompted thread replies on."

            return f"updated *{key}* to `{normalized_val}`."

        return _error(f"unknown action '{action}'")

    async def _tool_system_diagnostics(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can inspect system diagnostics")
        info = collect_system_diagnostics(self.settings, ctx.db, runtime.started_at)
        return format_system_diagnostics(info)

    async def _tool_manage_tasks(self, args: dict[str, Any], ctx: ToolContext) -> str:
        action = str(args.get("action") or "list").lower().strip()
        user_id = ctx.user_id

        if action == "add":
            title = str(args.get("title") or "").strip()
            if not title:
                return _error("task title is required")
            task_id = ctx.db.add_task(user_id, title)
            return f":white_check_mark: added task `#{task_id}`: {title}"

        if action == "list":
            show_all = bool(args.get("show_all", False))
            tasks = ctx.db.list_tasks(user_id, show_all=show_all)
            if not tasks:
                return "you don't have any pending tasks! :tada:" if not show_all else "no tasks recorded."
            lines = [f"*your tasks ({'all' if show_all else 'pending'}):*"]
            for t in tasks:
                box = ":white_check_mark:" if t["done"] else ":white_square:"
                title = f"~{t['title']}~" if t["done"] else t["title"]
                lines.append(f"{box} `#{t['id']}` {title}")
            return "\n".join(lines)

        if action == "complete":
            try:
                task_id = int(args.get("task_id") or 0)
            except (ValueError, TypeError):
                task_id = 0
            if not task_id:
                return _error("task_id is required")
            ok = ctx.db.complete_task(task_id, user_id)
            return f":white_check_mark: completed task `#{task_id}`" if ok else f"task `#{task_id}` not found"

        if action == "uncomplete":
            try:
                task_id = int(args.get("task_id") or 0)
            except (ValueError, TypeError):
                task_id = 0
            if not task_id:
                return _error("task_id is required")
            ok = ctx.db.uncomplete_task(task_id, user_id)
            return f":white_square: reopened task `#{task_id}`" if ok else f"task `#{task_id}` not found"

        if action == "delete":
            try:
                task_id = int(args.get("task_id") or 0)
            except (ValueError, TypeError):
                task_id = 0
            if not task_id:
                return _error("task_id is required")
            ok = ctx.db.delete_task(task_id, user_id)
            return f":wastebasket: deleted task `#{task_id}`" if ok else f"task `#{task_id}` not found"

        if action == "clear_completed":
            count = ctx.db.clear_completed_tasks(user_id)
            return f":wastebasket: cleared {count} completed task(s)."

        return _error(f"unknown task action: {action}")

    # ---------------------------------------------------------------- helpers
    async def _resolve_channel(self, ctx: ToolContext, value: str) -> str | None:
        value = value.strip()
        if value.lower() in {"here", "this", "current"}:
            return ctx.channel_id
        channel_id = extract_id(value, "#")
        if channel_id:
            return channel_id
        if value and value[0] in {"C", "G", "D"} and value.isalnum():
            return value
        needle = value.lstrip("#").lower()
        for channel in await all_channels(ctx.client):
            if str(channel.get("name", "")).lower() == needle:
                return channel.get("id")
        return None

    async def _is_private_host(self, hostname: str) -> bool:
        return await is_private_host(hostname)


def format_now(settings: Settings) -> str:
    return datetime.now(tzinfo()).strftime("%A %d %B %Y, %H:%M %Z")


__all__ = ["ToolContext", "Toolbox", "format_now", "format_when"]

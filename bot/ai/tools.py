"""The tools the AI model can call.

Every tool returns a short human/model readable string. Owner-only tools are
checked here against the real Slack sender, not just the prompt.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import socket
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx
from slack_sdk.web.async_client import AsyncWebClient

from bot.config import Settings
from bot.db import Database
from bot.features.nuggets import random_fact
from bot.features.stats import format_stats
from bot.reminders import confirmation_text, dm_target, create_reminder, reminder_line
from bot.slack_api import all_channels, all_users, channel_info, error_code, post, user_info
from bot.utils import dice
from bot.utils.text import extract_id
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
                "Send a direct message to a Slack user as the bot. Owner/admin only.",
                {
                    "user": {"type": "string", "description": "User id, @name, display name, or email."},
                    "text": {"type": "string", "description": "Message text (Slack mrkdwn)."},
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
                "Save a durable note the owner wants you to remember. Owner/admin only.",
                {"text": {"type": "string", "description": "The fact or note to store."}},
                ["text"],
            ),
            tool(
                "recall",
                "Search your saved notes and past AI conversation turns. Owner/admin only.",
                {"query": {"type": "string", "description": "What to search for. Omit for recent notes."}},
                ["query"],
            ),
            tool(
                "post_message",
                "Post a message to a Slack channel as the bot. Owner/admin only.",
                {
                    "channel": {"type": "string", "description": "Channel id, #name, or 'here'."},
                    "text": {"type": "string", "description": "Message text (Slack mrkdwn)."},
                },
                ["channel", "text"],
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
                "Read a public web page as plain text. Blocks local/private addresses. Owner/admin only.",
                {"url": {"type": "string", "description": "Public http(s) URL."}},
                ["url"],
            ),
        ]

        if self.settings.tavily_api_key:
            tools.append(
                tool(
                    "search_web",
                    "Search the live web and get a short answer with sources. Owner/admin only.",
                    {"query": {"type": "string", "description": "Search query."}},
                    ["query"],
                )
            )
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
        except Exception as exc:  # noqa: BLE001 - never crash the model loop
            logger.exception("tool %s failed", name)
            return _error(f"tool failed: {exc}")

    # ------------------------------------------------------------------ tools
    async def _tool_get_weather(self, args: dict[str, Any], ctx: ToolContext) -> str:
        location = (args.get("location") or self.settings.weather_default_location or "").strip()
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
        try:
            response = await ctx.client.conversations_history(channel=channel_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
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
        if not query:
            return _error("which user?")
        if not text:
            return _error("message text is required")
        user = await self._find_user(ctx, query)
        if not user:
            return _error(f"could not find a user matching {query!r}")
        user_id = user.get("id")
        if not await post(ctx.client, user_id, text):
            return _error("slack rejected the DM")
        return f"sent a DM to <@{user_id}>."

    async def _tool_nugget_fact(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return random_fact()

    async def _find_user(self, ctx: ToolContext, query: str) -> dict[str, Any] | None:
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
        note_id = ctx.db.add_note(ctx.user_id, text)
        return f"saved note #{note_id}."

    async def _tool_recall(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can search memory")
        query = str(args.get("query") or "").strip()
        if query:
            notes = ctx.db.search_notes(query, 10)
            turns = ctx.db.search_messages(query, 10)
        else:
            notes = ctx.db.recent_notes(10)
            turns = []
        payload = {
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
        if not payload["notes"] and not payload["conversations"]:
            return "nothing found in memory."
        return _json(payload)

    async def _tool_post_message(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can post as the bot")
        channel_value = str(args.get("channel") or "here").strip()
        text = str(args.get("text") or "").strip()
        if not text:
            return _error("message text is required")
        channel_id = await self._resolve_channel(ctx, channel_value)
        if not channel_id:
            return _error(f"could not resolve channel {channel_value!r}")
        result = await post(ctx.client, channel_id, text)
        if not result:
            return _error("slack rejected the message")
        return f"posted to <#{channel_id}> (ts {result.get('ts')})."

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
        try:
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=False,
                headers={"User-Agent": "chicen-nugget-bot/1.0"},
            ) as http:
                response = await http.get(url)
        except httpx.HTTPError as exc:
            return _error(f"fetch failed: {exc}")
        if response.status_code in {301, 302, 303, 307, 308}:
            return _error(f"redirected to {response.headers.get('location')!r}; ask me to fetch that url directly")
        if response.status_code >= 400:
            return _error(f"http {response.status_code}")
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            return _error(f"unsupported content type: {content_type or 'unknown'}")
        parser = _TextExtractor()
        parser.feed(response.text[:500_000])
        text = html.unescape("".join(parser.parts))
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        return text[:MAX_TOOL_RESULT]

    async def _tool_search_web(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not _need_owner(ctx):
            return _error("only the owner/admin can search the web")
        if not self.settings.tavily_api_key:
            return _error("TAVILY_API_KEY is not configured")
        query = str(args.get("query") or "").strip()
        if not query:
            return _error("query is required")
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
        except Exception as exc:  # noqa: BLE001
            return _error(f"web search failed: {exc}")
        return _json(
            {
                "answer": data.get("answer"),
                "results": [
                    {"title": item.get("title"), "url": item.get("url"), "content": item.get("content", "")[:700]}
                    for item in (data.get("results") or [])
                ],
            }
        )

    # ---------------------------------------------------------------- helpers
    async def _resolve_channel(self, ctx: ToolContext, value: str) -> str | None:
        value = value.strip()
        if value.lower() in {"here", "this", "current"}:
            return ctx.channel_id
        channel_id = extract_id(value, "#")
        if channel_id:
            return channel_id
        if value and value[0] in {"C", "G"} and value.isalnum():
            return value
        needle = value.lstrip("#").lower()
        for channel in await all_channels(ctx.client):
            if str(channel.get("name", "")).lower() == needle:
                return channel.get("id")
        return None

    async def _is_private_host(self, hostname: str) -> bool:
        try:
            if ipaddress.ip_address(hostname).is_private:
                return True
        except ValueError:
            pass
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
        except OSError:
            return True
        for info in infos:
            address = info[4][0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return True
        return False


def format_now(settings: Settings) -> str:
    return datetime.now(tzinfo()).strftime("%A %d %B %Y, %H:%M %Z")


__all__ = ["Toolbox", "ToolContext", "format_now", "format_when"]

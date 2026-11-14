"""Post-huddle summaries from Slack's AI notes transcript.

Slack has no public API for starting or joining a huddle, but its web client
does keep an AI transcript when huddle notes were enabled. That transcript is
stored as a canvas in the huddle thread. This module reads it with the same
xoxc/xoxd web session the user gateway already uses.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from bot.runtime import runtime
from bot.slack_api import post_ephemeral, user_info
from bot.utils.tz import tzinfo

logger = logging.getLogger(__name__)

BASE_URL = "https://slack.com/api"
MAX_TRANSCRIPT_CHARS = 24_000
MAX_RESULT_CHARS = 3_800


class HuddleError(RuntimeError):
    pass


def _credentials() -> tuple[str, str] | None:
    settings = runtime.settings
    token = (settings.slack_web_token or settings.slack_user_token or "").strip()
    cookie = (settings.slack_web_cookie or settings.slack_user_cookie or "").strip()
    if not token.startswith("xoxc") or not cookie:
        return None
    return token, cookie


async def _call(
    http: httpx.AsyncClient,
    method: str,
    token: str,
    cookie: str,
    **fields: Any,
) -> dict[str, Any]:
    form = {"token": token}
    for key, value in fields.items():
        if value is None:
            continue
        form[key] = (
            "true"
            if value is True
            else "false"
            if value is False
            else str(value)
        )
    files = {key: (None, value) for key, value in form.items()}
    try:
        response = await http.post(
            f"{BASE_URL}/{method}",
            headers={"Cookie": f"d={cookie}"},
            files=files,
        )
    except httpx.HTTPError as exc:
        raise HuddleError(f"slack request failed: {exc}") from exc
    if response.status_code in {401, 403}:
        raise HuddleError("slack web session expired; refresh xoxc/xoxd")
    if response.status_code == 429:
        raise HuddleError("slack is rate limiting huddle reads")
    if response.status_code != 200:
        raise HuddleError(f"slack returned http {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise HuddleError("slack returned invalid json") from exc
    if not data.get("ok"):
        raise HuddleError(str(data.get("error") or "slack error"))
    return data


async def latest_huddle(
    http: httpx.AsyncClient, token: str, cookie: str, channel: str
) -> dict[str, Any] | None:
    data = await _call(
        http, "huddles.history", token, cookie, channel=channel, limit=12
    )
    huddles = [
        item
        for item in data.get("huddles") or []
        if isinstance(item, dict) and item.get("transcript_file_id")
    ]
    if not huddles:
        return None
    return max(huddles, key=lambda item: float(item.get("date_start") or 0))


async def _file_info(
    http: httpx.AsyncClient,
    token: str,
    cookie: str,
    file_id: str,
    *,
    include_transcription: bool = False,
) -> dict[str, Any]:
    data = await _call(
        http,
        "files.info",
        token,
        cookie,
        file=file_id,
        include_transcription=include_transcription,
    )
    file_obj = data.get("file")
    if not isinstance(file_obj, dict):
        raise HuddleError("slack returned no file for the huddle transcript")
    return file_obj


async def fetch_transcript(
    http: httpx.AsyncClient,
    token: str,
    cookie: str,
    huddle: dict[str, Any],
) -> list[dict[str, Any]]:
    canvas_id = str(huddle.get("transcript_file_id") or "")
    if not canvas_id:
        raise HuddleError("this huddle has no AI notes")
    canvas = await _file_info(http, token, cookie, canvas_id)
    transcript_id = str(canvas.get("huddle_transcript_file_id") or "")
    if not transcript_id:
        raise HuddleError("this huddle has no AI transcript")
    transcript_file = await _file_info(
        http, token, cookie, transcript_id, include_transcription=True
    )
    transcription = transcript_file.get("huddle_transcription")
    if not isinstance(transcription, dict):
        raise HuddleError("slack did not return a transcript")
    lines = transcription.get("lines")
    return [line for line in lines or [] if isinstance(line, dict)]


def _stamp(milliseconds: Any) -> str:
    try:
        total = max(0, int(float(milliseconds) / 1000))
    except (TypeError, ValueError):
        return "00:00"
    return f"{total // 60:02d}:{total % 60:02d}"


async def format_transcript(
    lines: list[dict[str, Any]], client
) -> str:
    names: dict[str, str] = {}
    merged: list[tuple[str, str, list[str]]] = []
    for line in lines:
        text = " ".join(str(line.get("contents") or line.get("text") or "").split())
        if not text:
            continue
        user_id = str(line.get("user_id") or "")
        name = names.get(user_id)
        if name is None:
            if user_id:
                user = await user_info(client, user_id)
                profile = (user or {}).get("profile") or {}
                name = (
                    profile.get("display_name")
                    or (user or {}).get("real_name")
                    or (user or {}).get("name")
                    or user_id
                )
            else:
                name = "someone"
            names[user_id] = name
        stamp = _stamp(line.get("start_time_ms"))
        if merged and merged[-1][0] == user_id:
            merged[-1][2].append(text)
        else:
            merged.append((user_id, stamp, [text]))
    return "\n".join(
        f"*{names.get(user_id, 'someone')}* [{stamp}]: {' '.join(parts)}"
        for user_id, stamp, parts in merged
    )


async def summarize(transcript: str, user_id: str) -> str:
    agent = runtime.agent
    if agent is None:
        return transcript[:MAX_RESULT_CHARS]
    system = (
        "You are summarizing a Slack huddle transcript. Use only the transcript. "
        "Write 3-6 short bullets covering decisions, action items with owners, and "
        "open questions. Skip small talk and filler. If something is unclear, say so. "
        "Use Slack mrkdwn, no headings."
    )
    return await agent.complete(
        system=system,
        text=transcript[:MAX_TRANSCRIPT_CHARS],
        max_tokens=700,
        user_id=user_id,
    )


def _when(huddle: dict[str, Any]) -> str:
    try:
        started = datetime.fromtimestamp(float(huddle.get("date_start")), tzinfo())
        return started.strftime("%a %d %b, %H:%M")
    except (TypeError, ValueError, OSError):
        return "unknown time"


async def cmd_huddle(ack, command, client) -> None:
    await ack()
    channel = command["channel_id"]
    user = command["user_id"]
    settings = runtime.settings
    if not settings.is_admin(user):
        await post_ephemeral(
            client, channel, user, "huddle summaries are admin-only."
        )
        return
    credentials = _credentials()
    if credentials is None:
        await post_ephemeral(
            client,
            channel,
            user,
            "huddle summaries need a slack web session: set `SLACK_WEB_TOKEN`/`SLACK_WEB_COOKIE` (xoxc + xoxd).",
        )
        return

    await post_ephemeral(client, channel, user, ":headphones: reading the last huddle…")
    token, cookie = credentials
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
            huddle = await latest_huddle(http, token, cookie, channel)
            if huddle is None:
                await post_ephemeral(
                    client,
                    channel,
                    user,
                    "i couldn't find a huddle with AI notes in this channel. slack only keeps a transcript when AI notes were on.",
                )
                return
            lines = await fetch_transcript(http, token, cookie, huddle)
    except HuddleError as exc:
        await post_ephemeral(
            client, channel, user, f"couldn't read the huddle: {exc}"
        )
        return
    except Exception:
        logger.exception("huddle read failed")
        await post_ephemeral(
            client, channel, user, "huddle reading broke. check my logs?"
        )
        return

    transcript = await format_transcript(lines, client)
    if not transcript:
        await post_ephemeral(
            client, channel, user, "the huddle transcript was empty."
        )
        return
    summary = await summarize(transcript, user)
    participants = len({line.get("user_id") for line in lines if line.get("user_id")})
    header = (
        f"*huddle summary* · {_when(huddle)} · {participants or '?'} people · "
        f"{len(lines)} lines"
    )
    await post_ephemeral(
        client,
        channel,
        user,
        f"{header}\n{summary}"[:MAX_RESULT_CHARS],
    )


__all__ = [
    "HuddleError",
    "cmd_huddle",
    "fetch_transcript",
    "format_transcript",
    "latest_huddle",
    "summarize",
]

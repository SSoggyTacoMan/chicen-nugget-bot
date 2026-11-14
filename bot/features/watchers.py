"""Polling watchers: GitHub CI, GitHub notifications, email and uptime URLs."""

from __future__ import annotations

import asyncio
import email
import email.header
import imaplib
import json
import logging
import re
import time
from typing import Any

import httpx

from bot import integrations
from bot.features.proactive import is_quiet_now, note_proactive, proactive_available
from bot.runtime import runtime
from bot.slack_api import post

logger = logging.getLogger(__name__)

CI_SEEN_KEY = "watch:github_runs"
NOTIFY_SEEN_KEY = "watch:github_notifications"
EMAIL_SEEN_KEY = "watch:email_uids"
URL_STATE_KEY = "watch:url_state"

_last_ci = 0.0
_last_notifications = 0.0
_last_email = 0.0
_last_urls = 0.0


def _kv_bool(db, key: str, default: bool) -> bool:
    raw = db.kv_get(f"config:{key}") if db else None
    if raw is None:
        return default
    return str(raw).lower() in {"1", "true", "yes", "y", "on"}


def _kv_int(db, key: str, default: int) -> int:
    raw = db.kv_get(f"config:{key}") if db else None
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _kv_csv(db, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = db.kv_get(f"config:{key}") if db else None
    if raw is None:
        return default
    return tuple(item.strip() for item in str(raw).split(",") if item.strip())


def watcher_config() -> dict[str, Any]:
    settings = runtime.settings
    db = runtime.db
    return {
        "github_ci": _kv_bool(db, "watch_github_ci", settings.github_watch),
        "github_ci_interval": max(
            60, _kv_int(db, "watch_github_ci_interval", settings.github_watch_interval)
        ),
        "github_notifications": _kv_bool(
            db, "watch_github_notifications", settings.watch_github_notifications
        ),
        "github_notifications_interval": max(
            60,
            _kv_int(
                db,
                "watch_github_notifications_interval",
                settings.watch_github_notifications_interval,
            ),
        ),
        "email": _kv_bool(db, "watch_email", settings.watch_email),
        "email_interval": max(
            60, _kv_int(db, "watch_email_interval", settings.watch_email_interval)
        ),
        "email_limit": max(
            1, min(10, _kv_int(db, "watch_email_limit", settings.watch_email_limit))
        ),
        "urls": _kv_csv(db, "watch_urls", settings.watch_urls),
        "url_interval": max(
            60, _kv_int(db, "watch_url_interval", settings.watch_url_interval)
        ),
        "url_timeout": max(
            5, _kv_int(db, "watch_url_timeout", settings.watch_url_timeout)
        ),
    }


async def _notify(text: str) -> bool:
    settings = runtime.settings
    if not settings.owner_user_id or runtime.db is None:
        return False
    if is_quiet_now(settings) or not proactive_available(runtime.db, settings):
        return False
    sent = await post(runtime.client, settings.owner_user_id, text)
    if sent:
        note_proactive(runtime.db, settings)
    return bool(sent)


async def tick() -> None:
    await _github_ci()
    await _github_notifications()
    await _email()
    await _urls()


async def _github_ci() -> None:
    global _last_ci
    cfg = watcher_config()
    if not cfg["github_ci"] or not runtime.settings.owner_user_id or runtime.db is None:
        return
    now = time.monotonic()
    if now - _last_ci < cfg["github_ci_interval"]:
        return
    _last_ci = now

    token, repo = _github_repo()
    if not token or not repo:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.get(
                f"https://api.github.com/repos/{repo}/actions/runs?per_page=5",
                headers=_github_headers(token),
            )
    except Exception:
        logger.debug("github ci watcher request failed", exc_info=True)
        return
    if response.status_code != 200:
        return
    runs = ((response.json() or {}).get("workflow_runs")) or []
    run_ids = [str(run.get("id")) for run in runs if run.get("id")]
    seen = _seen(CI_SEEN_KEY)
    if seen is None:
        runtime.db.kv_set(CI_SEEN_KEY, json.dumps(run_ids))
        return
    failures = [
        run
        for run in runs
        if str(run.get("id")) not in seen and run.get("conclusion") == "failure"
    ]
    if not failures:
        _save_seen(CI_SEEN_KEY, run_ids, seen)
        return
    lines = [
        f"- *{run.get('name') or 'workflow'}* on `{run.get('head_branch') or '?'}`: "
        f"{run.get('html_url') or ''}"
        for run in failures[:3]
    ]
    if await _notify(f":warning: *CI failed* in `{repo}`\n" + "\n".join(lines)):
        _save_seen(CI_SEEN_KEY, run_ids, seen)


async def _github_notifications() -> None:
    global _last_notifications
    cfg = watcher_config()
    if (
        not cfg["github_notifications"]
        or not runtime.settings.owner_user_id
        or runtime.db is None
    ):
        return
    now = time.monotonic()
    if now - _last_notifications < cfg["github_notifications_interval"]:
        return
    _last_notifications = now

    token, _ = _github_repo()
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.get(
                "https://api.github.com/notifications",
                params={"all": "false", "per_page": 20},
                headers=_github_headers(token),
            )
    except Exception:
        logger.debug("github notification watcher failed", exc_info=True)
        return
    if response.status_code != 200:
        return
    items = response.json() or []
    ids = [str(item.get("id")) for item in items if item.get("id")]
    seen = _seen(NOTIFY_SEEN_KEY)
    if seen is None:
        runtime.db.kv_set(NOTIFY_SEEN_KEY, json.dumps(ids))
        return
    new = [item for item in items if str(item.get("id")) not in seen]
    if not new:
        _save_seen(NOTIFY_SEEN_KEY, ids, seen)
        return
    lines = []
    for item in new[:5]:
        repo_obj = item.get("repository") or {}
        repo = repo_obj.get("full_name") or "github"
        subject = (item.get("subject") or {}).get("title") or "notification"
        reason = item.get("reason") or ""
        url = repo_obj.get("html_url") or item.get("url") or ""
        lines.append(f"- *{repo}*: {subject} ({reason})\n  {url}")
    if await _notify(":github: *new github notifications*\n" + "\n".join(lines)):
        _save_seen(NOTIFY_SEEN_KEY, ids, seen)


async def _email() -> None:
    global _last_email
    cfg = watcher_config()
    if not cfg["email"] or not runtime.settings.owner_user_id or runtime.db is None:
        return
    now = time.monotonic()
    if now - _last_email < cfg["email_interval"]:
        return
    _last_email = now

    imap = integrations.imap_config(runtime.db, runtime.settings)
    host, user, password = imap["host"], imap["user"], imap["password"]
    if not (host and user and password):
        return
    try:
        port = int(imap["port"] or 993)
    except ValueError:
        port = 993
    try:
        current, messages = await asyncio.to_thread(
            _fetch_unseen, host, port, user, password, cfg["email_limit"]
        )
    except Exception:
        logger.debug("email watcher failed", exc_info=True)
        return
    seen = _seen(EMAIL_SEEN_KEY)
    if seen is None:
        runtime.db.kv_set(EMAIL_SEEN_KEY, json.dumps(current[-300:]))
        return
    new = [uid for uid in current if uid not in seen]
    if not new:
        _save_seen(EMAIL_SEEN_KEY, current[-300:], seen)
        return
    lines = [
        f"- *{item['subject'] or '(no subject)'}* from {item['from'] or 'someone'}\n  {item['snippet'][:180]}"
        for item in messages
        if item["uid"] in new
    ][: cfg["email_limit"]]
    if not lines:
        lines = [f"{len(new)} new unread email(s)"]
    elif len(new) > len(lines):
        lines.append(f"- and {len(new) - len(lines)} more")
    if await _notify(":email: *new email*\n" + "\n".join(lines)):
        _save_seen(EMAIL_SEEN_KEY, current[-300:], seen)


async def _urls() -> None:
    global _last_urls
    cfg = watcher_config()
    if not cfg["urls"] or not runtime.settings.owner_user_id or runtime.db is None:
        return
    now = time.monotonic()
    if now - _last_urls < cfg["url_interval"]:
        return
    _last_urls = now

    try:
        state = json.loads(runtime.db.kv_get(URL_STATE_KEY) or "{}")
        if not isinstance(state, dict):
            state = {}
    except (TypeError, ValueError):
        state = {}
    updated = dict(state)
    for url in cfg["urls"]:
        if not url.startswith(("http://", "https://")):
            continue
        up = await _url_up(url, cfg["url_timeout"])
        if url not in state:
            updated[url] = up
            continue
        was_up = bool(state[url])
        if was_up == up:
            continue
        label = url
        text = (
            f":warning: *down*: {label}"
            if not up
            else f":white_check_mark: *back up*: {label}"
        )
        if await _notify(text):
            updated[url] = up
    runtime.db.kv_set(URL_STATE_KEY, json.dumps(updated))


async def _url_up(url: str, timeout: int) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
            response = await http.get(url)
        return response.status_code < 400
    except Exception:  # noqa: BLE001
        return False


def _github_repo() -> tuple[str, str]:
    settings = runtime.settings
    if runtime.db is None:
        return "", ""
    github = integrations.github_config(runtime.db, settings)
    token = (github.get("token") or "").strip()
    repo = (github.get("repo") or "").strip()
    repo = repo.replace("https://github.com/", "").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return token, ""
    return token, repo


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "chicen-nugget-bot",
    }


def _seen(key: str) -> set[str] | None:
    if runtime.db is None:
        return None
    raw = runtime.db.kv_get(key)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    return {str(item) for item in value} if isinstance(value, list) else set()


def _save_seen(key: str, ids: list[str], seen: set[str]) -> None:
    if runtime.db is None:
        return
    runtime.db.kv_set(key, json.dumps((ids + list(seen))[:300]))


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for chunk, charset in email.header.decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return " ".join(parts).strip()[:200]


def _email_snippet(message: Any, max_len: int = 300) -> str:
    body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
    else:
        payload = message.get_payload(decode=True)
        if payload:
            body = payload.decode(message.get_content_charset() or "utf-8", errors="replace")
    return " ".join(body.split())[:max_len]


def _fetch_unseen(
    host: str, port: int, user: str, password: str, limit: int
) -> tuple[list[str], list[dict[str, str]]]:
    client = imaplib.IMAP4_SSL(host, port, timeout=15)
    try:
        typ, _ = client.login(user, password)
        if typ != "OK":
            raise RuntimeError("imap login failed")
        typ, _ = client.select("INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError("could not open INBOX")
        typ, data = client.uid("search", None, "UNSEEN")
        if typ != "OK":
            raise RuntimeError("imap search failed")
        uids = [item.decode("ascii", errors="replace") for item in (data[0].split() if data else [])]
        messages: list[dict[str, str]] = []
        for uid in reversed(uids[-limit:]):
            typ, fetched = client.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            msg = email.message_from_bytes(fetched[0][1])
            messages.append(
                {
                    "uid": uid,
                    "subject": _decode_header(msg.get("Subject")),
                    "from": _decode_header(msg.get("From")),
                    "snippet": _email_snippet(msg),
                }
            )
        return uids, messages
    finally:
        try:
            client.logout()
        except Exception:
            logger.debug("imap logout failed", exc_info=True)

"""Small polling watchers. First one: failed GitHub Actions runs."""

from __future__ import annotations

import json
import logging
import re
import time

import httpx

from bot import integrations
from bot.features.proactive import is_quiet_now, note_proactive, proactive_available
from bot.runtime import runtime
from bot.slack_api import post

logger = logging.getLogger(__name__)

SEEN_KEY = "watch:github_runs"
_last_tick = 0.0


async def tick() -> None:
    global _last_tick
    settings = runtime.settings
    if not settings.github_watch or not settings.owner_user_id or runtime.db is None:
        return
    now = time.monotonic()
    if now - _last_tick < settings.github_watch_interval:
        return
    _last_tick = now

    github = integrations.github_config(runtime.db, settings)
    token = (github.get("token") or "").strip()
    repo = (github.get("repo") or "").strip()
    repo = repo.replace("https://github.com/", "").removesuffix(".git")
    if not token or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.get(
                f"https://api.github.com/repos/{repo}/actions/runs?per_page=5",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "chicen-nugget-bot",
                },
            )
    except Exception:
        logger.debug("github ci watcher request failed", exc_info=True)
        return
    if response.status_code != 200:
        return
    runs = ((response.json() or {}).get("workflow_runs")) or []
    if not runs:
        return
    run_ids = [str(run.get("id")) for run in runs if run.get("id")]
    raw = runtime.db.kv_get(SEEN_KEY)
    if not raw:
        runtime.db.kv_set(SEEN_KEY, json.dumps(run_ids))
        return
    try:
        seen = set(json.loads(raw))
    except (TypeError, ValueError):
        seen = set()

    failures = [
        run
        for run in runs
        if str(run.get("id")) not in seen and run.get("conclusion") == "failure"
    ]
    if not failures:
        runtime.db.kv_set(SEEN_KEY, json.dumps((run_ids + list(seen))[:100]))
        return
    if is_quiet_now(settings) or not proactive_available(runtime.db, settings):
        return

    lines = []
    for run in failures[:3]:
        name = run.get("name") or "workflow"
        branch = run.get("head_branch") or "?"
        url = run.get("html_url") or ""
        lines.append(f"- *{name}* on `{branch}`: {url}")
    sent = await post(
        runtime.client,
        settings.owner_user_id,
        f":warning: *CI failed* in `{repo}`\n" + "\n".join(lines),
    )
    if sent:
        runtime.db.kv_set(SEEN_KEY, json.dumps((run_ids + list(seen))[:100]))
        note_proactive(runtime.db, settings)

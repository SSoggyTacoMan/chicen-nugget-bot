"""`/nugget doctor`: one pass over the things that usually break."""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

import httpx

from bot import integrations
from bot.runtime import runtime
from bot.slack_api import granted_scopes, post_ephemeral

logger = logging.getLogger(__name__)

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "slack-app-manifest.json"


async def cmd_doctor(ack, command, client) -> None:
    await ack()
    channel = command["channel_id"]
    user = command["user_id"]
    if not runtime.settings.is_admin(user):
        await post_ephemeral(client, channel, user, "doctor is admin only.")
        return
    lines = ["*nugget doctor*"]
    ok = ":white_check_mark:"
    warn = ":warning:"

    try:
        lines.append(f"{ok} sqlite: {runtime.db.integrity_check()}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"{warn} sqlite: {exc}")

    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        expected = set(
            ((manifest.get("oauth_config") or {}).get("scopes") or {}).get("bot")
            or []
        )
        actual = await granted_scopes(runtime.client)
        if actual is None:
            lines.append(f"{warn} bot scopes: could not read them")
        else:
            missing = sorted(expected - actual)
            if missing:
                lines.append(f"{warn} bot scopes missing: `{', '.join(missing)}`")
            else:
                lines.append(f"{ok} bot scopes match the manifest")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"{warn} bot scopes: {exc}")

    if runtime.user_client is None:
        lines.append(f"{warn} boi: not configured")
    else:
        try:
            auth = await runtime.user_client.auth_test()
            lines.append(f"{ok} boi: <@{auth.get('user_id')}>")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{warn} boi: {exc}")

    settings = runtime.settings
    if settings.ai_enabled:
        lines.append(
            f"{ok} ai: `{settings.ai_model}` -> `{settings.ai_base_url}`"
        )
    else:
        lines.append(f"{warn} ai: disabled or missing AI_API_KEY")

    if importlib.util.find_spec("playwright") is None:
        lines.append(f"{warn} browser: playwright is not installed")
    elif not settings.browser_enabled:
        lines.append(f"{warn} browser: disabled")
    else:
        lines.append(f"{ok} browser: playwright present")

    sandbox = Path(getattr(settings, "sandbox_dir", settings.data_dir / "sandboxes"))
    lines.append(
        f"{ok if sandbox.exists() else warn} sandbox: host path `{sandbox}`"
    )

    github = integrations.github_config(runtime.db, settings)
    token = (github.get("token") or "").strip()
    if not token:
        lines.append(f"{warn} github: no token, reads only")
    else:
        repo = (github.get("repo") or "").strip()
        repo = repo.replace("https://github.com/", "").removesuffix(".git")
        try:
            async with httpx.AsyncClient(timeout=12) as http:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "chicen-nugget-bot",
                }
                user_resp = await http.get("https://api.github.com/user", headers=headers)
                if user_resp.status_code != 200:
                    lines.append(
                        f"{warn} github: token rejected ({user_resp.status_code})"
                    )
                else:
                    login = (user_resp.json() or {}).get("login")
                    if "/" in repo:
                        repo_resp = await http.get(
                            f"https://api.github.com/repos/{repo}", headers=headers
                        )
                        permissions = (
                            (repo_resp.json() or {}).get("permissions")
                            if repo_resp.status_code == 200
                            else {}
                        )
                        push = "write" if permissions.get("push") else "read"
                        lines.append(
                            f"{ok} github: @{login}, {repo} ({push})"
                        )
                    else:
                        lines.append(f"{ok} github: @{login}, no repo set")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{warn} github: {exc}")

    lines.append(f"{ok} gateway: {'on' if runtime.user_gateway else 'off'}")
    await post_ephemeral(client, channel, user, "\n".join(lines))

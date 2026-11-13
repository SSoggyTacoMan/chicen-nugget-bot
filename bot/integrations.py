"""Runtime integration config and setup status.

Values are resolved from SQLite first, then from the environment. That lets the
owner connect Google, the Mac bridge, GitHub, IMAP email, and owner posting from
Slack without editing `.env` on the server.
"""

from __future__ import annotations

import sys
from typing import Any

from bot.config import Settings
from bot.db import Database

SETUP_INTEGRATIONS: tuple[tuple[str, str], ...] = (
    ("google", "Google"),
    ("mac", "Mac"),
    ("github", "GitHub"),
    ("email", "Email"),
    ("slack", "Slack profile"),
)

# Values that must not be typed into chat, even by an admin. They belong in the
# setup modals, which are ephemeral Slack forms.
SECRET_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "google_access_token",
        "google_refresh_token",
        "google_client_secret",
        "mac_bridge_token",
        "github_token",
        "imap_password",
        "slack_owner_token",
        "slack_owner_cookie",
    }
)

SECRET_SETUP_TARGET: dict[str, str] = {
    "google_access_token": "google",
    "google_refresh_token": "google",
    "google_client_secret": "google",
    "mac_bridge_token": "mac",
    "github_token": "github",
    "imap_password": "email",
    "slack_owner_token": "slack",
    "slack_owner_cookie": "slack",
}


def secret_setup_target(key: str) -> str:
    return SECRET_SETUP_TARGET.get(key, "the relevant integration")

# Keys cleared by `/nugget setup disconnect <integration>`.
DISCONNECT_KEYS: dict[str, tuple[str, ...]] = {
    "google": (
        "google_client_id",
        "google_client_secret",
        "google_refresh_token",
        "google_access_token",
        "google_redirect_uri",
        "google_oauth_state",
        "google_email",
    ),
    "mac": ("mac_bridge_url", "mac_bridge_token"),
    "github": ("github_username", "github_token", "repo_url"),
    "email": ("imap_host", "imap_port", "imap_user", "imap_password"),
    "slack": ("slack_owner_token", "slack_owner_cookie"),
}


def _get(db: Database | None, key: str) -> str:
    value = db.kv_get(f"config:{key}") if db else None
    return value.strip() if isinstance(value, str) else ""


def _setting(settings: Settings, name: str) -> str:
    value = getattr(settings, name, None)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def google_credentials(db: Database | None, settings: Settings) -> dict[str, str]:
    return {
        "client_id": _get(db, "google_client_id") or _setting(settings, "google_client_id"),
        "client_secret": _get(db, "google_client_secret")
        or _setting(settings, "google_client_secret"),
        "refresh_token": _get(db, "google_refresh_token")
        or _setting(settings, "google_refresh_token"),
        "access_token": _get(db, "google_access_token")
        or _setting(settings, "google_access_token"),
    }


def google_redirect_uri(db: Database | None, settings: Settings) -> str:
    return (
        _get(db, "google_redirect_uri")
        or _setting(settings, "google_redirect_uri")
        or "http://localhost:9876/oauth2callback"
    )


def google_email(db: Database | None) -> str:
    return _get(db, "google_email")


def mac_config(db: Database | None, settings: Settings) -> dict[str, str]:
    return {
        "url": _get(db, "mac_bridge_url") or _setting(settings, "mac_bridge_url"),
        "token": _get(db, "mac_bridge_token") or _setting(settings, "mac_bridge_token"),
    }


def github_config(db: Database | None, settings: Settings) -> dict[str, str]:
    return {
        "username": _get(db, "github_username") or _setting(settings, "github_username"),
        "token": _get(db, "github_token") or _setting(settings, "github_token"),
        "repo": _get(db, "repo_url") or _setting(settings, "repo_url"),
    }


def imap_config(db: Database | None, settings: Settings) -> dict[str, str]:
    port = _get(db, "imap_port") or _setting(settings, "imap_port")
    return {
        "host": _get(db, "imap_host") or _setting(settings, "imap_host"),
        "port": port or "993",
        "user": _get(db, "imap_user") or _setting(settings, "imap_user"),
        "password": _get(db, "imap_password") or _setting(settings, "imap_password"),
    }


def owner_token_config(db: Database | None, settings: Settings) -> dict[str, str]:
    return {
        "token": _get(db, "slack_owner_token") or _setting(settings, "slack_owner_token"),
        "cookie": _get(db, "slack_owner_cookie") or _setting(settings, "slack_owner_cookie"),
    }


def clear_integration(db: Database | None, integration: str) -> int:
    """Remove stored settings for one integration. Returns the number cleared."""
    if not db:
        return 0
    removed = 0
    for key in DISCONNECT_KEYS.get(integration, ()):
        if db.kv_get(f"config:{key}") is not None:
            db.kv_del(f"config:{key}")
            removed += 1
    if integration == "google":
        for key in ("cache:google_access_token", "cache:google_token_exp"):
            if db.kv_get(key) is not None:
                db.kv_del(key)
                removed += 1
    return removed


def setup_status_text(
    db: Database | None,
    settings: Settings,
    *,
    owner_posting: bool = False,
    assistant_identity: bool = False,
) -> str:
    """Short status report shared by the setup command and the AI tool."""
    google = google_credentials(db, settings)
    mac = mac_config(db, settings)
    github = github_config(db, settings)
    imap = imap_config(db, settings)
    owner = owner_token_config(db, settings)

    if google["refresh_token"] and google["client_id"] and google["client_secret"]:
        email = google_email(db)
        google_state = f"connected{f' as {email}' if email else ''}"
    elif google["access_token"]:
        google_state = "access token configured (expires; reconnect for auto-refresh)"
    elif google["client_id"] and google["client_secret"]:
        google_state = "client configured, needs authorization"
    else:
        google_state = "not configured"

    if github["token"] and github["username"]:
        github_state = f"connected as @{github['username']} with a token"
    elif github["username"]:
        github_state = f"public API only as @{github['username']} (no token)"
    elif github["token"]:
        github_state = "token configured, username not set"
    else:
        github_state = "not configured"

    if imap["host"] and imap["user"] and imap["password"]:
        email_state = f"configured for {imap['user']} at {imap['host']}"
    else:
        email_state = "not configured"

    if mac["url"]:
        mac_state = f"bridge configured at {mac['url']}"
    elif sys.platform == "darwin":
        mac_state = "local macOS control available"
    else:
        mac_state = "not configured (run the companion bridge or deploy on a Mac)"

    if owner_posting:
        slack_state = "native posting active (your own token, no app badge)"
    elif owner["token"]:
        slack_state = (
            "token stored but not validated (run `/nugget setup slack`); posting in your "
            "name currently uses your display name + avatar on a bot message"
        )
    else:
        slack_state = (
            "posting in your name uses your display name + avatar on a bot message "
            "(app badge visible; add your own user token for a native post)"
        )

    boi_state = (
        "connected" if assistant_identity else "not configured (SLACK_USER_TOKEN in the server env)"
    )

    lines = [
        "*integration status*",
        f"• *Google Workspace*: {google_state}",
        f"• *Mac companion*: {mac_state}",
        f"• *GitHub*: {github_state}",
        f"• *Email (IMAP)*: {email_state}",
        f"• *Boi user account*: {boi_state}",
        f"• *Your Slack profile*: {slack_state}",
        "",
        "connect or change these with `/nugget setup`, or ask me to open the setup panel.",
    ]
    return "\n".join(lines)


def prompt_integration_lines(
    db: Database | None,
    settings: Settings,
    *,
    owner_posting: bool = False,
    assistant_identity: bool = False,
    bot_model: str,
    assistant_model: str,
) -> str:
    """Accurate, compact integration status for the persona prompt."""
    google = google_credentials(db, settings)
    mac = mac_config(db, settings)
    github = github_config(db, settings)
    imap = imap_config(db, settings)
    owner = owner_token_config(db, settings)

    if google["refresh_token"] and google["client_id"] and google["client_secret"]:
        google_state = "connected"
    elif google["access_token"]:
        google_state = "connected with a temporary access token (reconnect for auto-refresh)"
    elif google["client_id"] and google["client_secret"]:
        google_state = "client configured, needs authorization (use `/nugget setup google`)"
    else:
        google_state = "not configured (use `/nugget setup google`)"
    if mac["url"]:
        mac_state = "connected via companion bridge"
    elif sys.platform == "darwin":
        mac_state = "connected (local macOS control)"
    else:
        mac_state = "not configured (run `scripts/mac_companion.py` and `/nugget setup mac`)"
    email_state = (
        f"connected for {imap['user']}"
        if imap["host"] and imap["user"] and imap["password"]
        else "not configured (use `/nugget setup email`)"
    )
    if owner_posting:
        owner_state = "native, your own token (no app badge)"
    elif owner["token"]:
        owner_state = "token stored but not validated (run `/nugget setup slack`)"
    else:
        owner_state = (
            "your display name + avatar on a bot message (app badge; add your own token "
            "for a native post)"
        )
    gh_user = github["username"] or "not set"
    gh_state = (
        f"@{gh_user} with token"
        if github["token"] and github["username"]
        else (
            f"@{gh_user} public API only"
            if github["username"]
            else "not configured (use `/nugget setup github`)"
        )
    )
    return (
        f"- GitHub & Hackatime: {gh_state}\n"
        f"- Google Workspace (Calendar, Gmail, Drive): {google_state}\n"
        f"- Mac companion link (volume, apps, clipboard, lock/sleep, shell): {mac_state}\n"
        f"- Email (IMAP): {email_state}\n"
        f"- Boi personal user account (SLACK_USER_TOKEN): "
        f"{'connected' if assistant_identity else 'not configured (server env only)'}\n"
        f"- Posting in the owner's name: {owner_state}\n"
        f"- models: bot app `{bot_model}`, Boi `{assistant_model}`"
    )


def setup_panel_blocks(requested: str | None = None) -> list[dict[str, Any]]:
    """A DM panel of setup buttons. Secrets are collected in modals, not chat."""
    labels = {
        "google": ("Google", "Calendar, Gmail and Drive"),
        "mac": ("Mac", "Companion bridge or local macOS"),
        "github": ("GitHub", "Username, token and default repo"),
        "email": ("Email", "IMAP inbox and app password"),
        "slack": ("Slack profile", "Post in your own name"),
    }
    if requested and requested in labels:
        title, detail = labels[requested]
        intro = f"*{title} setup* - {detail}. tap the button to open the form."
        keys = (requested,)
    else:
        intro = "pick an integration. secrets open in a Slack form, never in chat."
        keys = tuple(key for key, _ in SETUP_INTEGRATIONS)
    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": labels[key][0]},
            "action_id": "setup_open",
            "value": key,
        }
        for key in keys
    ]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": intro}},
        {"type": "actions", "elements": buttons},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "`/nugget setup status` shows what is connected. "
                    "`/nugget setup disconnect <name>` removes stored credentials.",
                }
            ],
        },
    ]

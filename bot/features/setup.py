"""Chat-based integration setup.

Everything the bot can connect to can be configured from Slack: Google
Workspace, the Mac companion, GitHub, IMAP email, and owner-profile posting.
Secrets are collected in Slack modals instead of chat messages or `.env` edits.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

import httpx

from bot import integrations
from bot.google_oauth import (
    GoogleOAuthError,
    build_auth_url,
    exchange_code,
    fetch_identity,
    parse_oauth_input,
)
from bot.integrations import (
    clear_integration,
    google_credentials,
    google_redirect_uri,
    setup_panel_blocks,
    setup_status_text,
)
from bot.owner_posting import (
    deactivate_owner_posting,
    save_owner_credentials,
    validate_owner_token,
)
from bot.runtime import runtime
from bot.slack_api import error_code, post, post_ephemeral

logger = logging.getLogger(__name__)

INTEGRATION_NAMES: dict[str, str] = {
    "google": "Google Workspace",
    "mac": "Mac companion",
    "github": "GitHub",
    "email": "Email",
    "slack": "your Slack profile",
}


def register(app) -> None:
    app.action("setup_open")(on_setup_open)
    app.action("setup_google_code")(on_google_code_button)
    app.action("setup_google_creds")(on_google_creds_button)
    app.view("setup_google_credentials")(on_google_credentials_submit)
    app.view("setup_google_code")(on_google_code_submit)
    app.view("setup_github")(on_github_submit)
    app.view("setup_email")(on_email_submit)
    app.view("setup_mac")(on_mac_submit)
    app.view("setup_slack")(on_slack_submit)


# ------------------------------------------------------------------ utilities
def _status() -> str:
    return setup_status_text(
        runtime.db,
        runtime.settings,
        owner_posting=runtime.owner_client is not None,
        assistant_identity=runtime.user_client is not None,
    )


def _is_admin(user_id: str | None) -> bool:
    return bool(user_id) and runtime.settings.is_admin(user_id)


def _set(key: str, value: str) -> None:
    if runtime.db:
        if value:
            runtime.db.kv_set(f"config:{key}", value)
        else:
            runtime.db.kv_del(f"config:{key}")


def _del(*keys: str) -> None:
    if runtime.db:
        for key in keys:
            runtime.db.kv_del(f"config:{key}")


def _merge(new: str, current: str) -> str:
    """Blank keeps the current value; '-', 'none', 'clear' removes it."""
    value = (new or "").strip()
    if not value:
        return current
    if value.lower() in {"-", "none", "clear", "remove", "delete"}:
        return ""
    return value


def _values(view: dict[str, Any]) -> dict[str, str]:
    values = (view.get("state") or {}).get("values") or {}
    out: dict[str, str] = {}
    for block_id, block in values.items():
        element = (block or {}).get("value") or {}
        out[block_id] = str(element.get("value") or "").strip()
    return out


def _input(
    block_id: str,
    label: str,
    *,
    placeholder: str = "",
    initial: str | None = None,
    multiline: bool = False,
    optional: bool = True,
    max_length: int = 400,
) -> dict[str, Any]:
    element: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": "value",
        "multiline": multiline,
        "max_length": max_length,
    }
    if placeholder:
        element["placeholder"] = {"type": "plain_text", "text": placeholder}
    if initial:
        element["initial_value"] = initial
    return {
        "type": "input",
        "block_id": block_id,
        "optional": optional,
        "label": {"type": "plain_text", "text": label},
        "element": element,
    }


def _modal(
    callback_id: str, title: str, blocks: list[dict[str, Any]], *, submit: str = "Save"
) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": {"type": "plain_text", "text": title[:24]},
        "submit": {"type": "plain_text", "text": submit[:24]},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def _note(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


async def _open_modal(client, trigger_id: str | None, view: dict[str, Any]) -> bool:
    if not trigger_id:
        return False
    try:
        await client.views_open(trigger_id=trigger_id, view=view)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("setup views_open failed: %s", error_code(exc))
        return False


async def _dm(client, user_id: str, text: str, blocks: list[dict[str, Any]] | None = None) -> bool:
    return bool(await post(client, user_id, text, blocks=blocks))


async def _say(client, command: dict[str, Any], text: str) -> None:
    await post_ephemeral(
        client,
        command.get("channel_id") or command.get("user_id") or "",
        command.get("user_id") or "",
        text,
    )


# ------------------------------------------------------------------- /nugget setup
async def cmd_setup(ack, command, client) -> None:
    await ack()
    user_id = command.get("user_id")
    if not _is_admin(user_id):
        await _say(client, command, "only the owner/admin can set up integrations.")
        return

    text = (command.get("text") or "").strip()
    parts = text.split()
    action = parts[0].lower() if parts else ""

    if not action or action in {"panel", "home"}:
        await post_ephemeral(
            client,
            command.get("channel_id") or user_id or "",
            user_id or "",
            "integration setup",
            blocks=setup_panel_blocks(),
        )
        return

    if action == "status":
        await _say(client, command, _status())
        return

    if action in {"help", "?"}:
        await _say(client, command, _usage())
        return

    if action == "disconnect":
        await _disconnect(client, command, parts[1].lower() if len(parts) > 1 else "")
        return

    if action not in INTEGRATION_NAMES:
        await _say(client, command, _usage())
        return

    if action == "google":
        rest = text.partition(" ")[2].strip()
        lowered = rest.lower()
        if lowered in {"help", "?"}:
            await _say(client, command, _google_usage())
            return
        if lowered == "status":
            await _say(client, command, _status())
            return
        if lowered in {"credentials", "creds", "client", "change", "app"}:
            if not await _open_modal(
                client, command.get("trigger_id"), _google_credentials_modal()
            ):
                await _say(client, command, "couldn't open the setup form. try again in a second.")
            return
        if lowered == "code" or lowered.startswith("code "):
            code_raw = rest[4:].strip()
            if code_raw:
                await _say(
                    client, command, await _complete_google_code(client, user_id or "", code_raw)
                )
                return
            await _open_modal(client, command.get("trigger_id"), _google_code_modal())
            return
        if lowered in {"link", "auth", "authorize", "reconnect"}:
            if not _google_ready():
                await _say(
                    client,
                    command,
                    "add a Google client id and secret first with `/nugget setup google credentials`.",
                )
                return
            ok, message = await _send_google_link(client, user_id or "")
            await _say(client, command, message if ok else f"couldn't send the auth link: {message}")
            return
        if rest:
            await _say(client, command, await _complete_google_code(client, user_id or "", rest))
            return
        if _google_ready():
            ok, message = await _send_google_link(client, user_id or "")
            await _say(client, command, message if ok else f"couldn't send the auth link: {message}")
            return
        if not await _open_modal(client, command.get("trigger_id"), _google_credentials_modal()):
            await _say(client, command, "couldn't open the setup form. try again in a second.")
        return

    if not await _open_modal(
        client, command.get("trigger_id"), _integration_modal(action)
    ):
        await _say(client, command, "couldn't open the setup form. try again in a second.")


def _usage() -> str:
    return (
        "*integration setup*\n"
        "• `/nugget setup` - button panel\n"
        "• `/nugget setup status` - what is connected\n"
        "• `/nugget setup google` - Google Calendar, Gmail, Drive\n"
        "• `/nugget setup google credentials` - change the OAuth client\n"
        "• `/nugget setup google link` - resend the authorization link\n"
        "• `/nugget setup google code` - paste the redirected URL\n"
        "• `/nugget setup github` - username, token, default repo\n"
        "• `/nugget setup email` - IMAP inbox\n"
        "• `/nugget setup mac` - companion bridge\n"
        "• `/nugget setup slack` - posting in your name (optional native token)\n"
        "• `/nugget setup disconnect <name>` - remove stored credentials\n\n"
        "secrets go into Slack modals, never chat. Google uses a desktop OAuth "
        "client and a `http://localhost:9876/oauth2callback` redirect; you paste "
        "the redirected URL back here."
    )


def _google_usage() -> str:
    return (
        "*google setup*\n"
        "• `/nugget setup google` - authorize, or add client credentials if missing\n"
        "• `/nugget setup google credentials` - change the OAuth client id/secret\n"
        "• `/nugget setup google link` - resend the authorization link\n"
        "• `/nugget setup google code` - paste the redirected URL or code\n\n"
        "scopes: Calendar, Gmail modify, Drive read-only. the OAuth client must be "
        "a *Desktop app* with `http://localhost:9876/oauth2callback` as a redirect URI."
    )


# ----------------------------------------------------------------------- panel
async def on_setup_open(ack, body, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    actions = body.get("actions") or []
    integration = str((actions[0] if actions else {}).get("value") or "").lower()
    if integration == "google":
        if _google_ready():
            ok, message = await _send_google_link(client, user_id or "")
            if not ok:
                await _dm(client, user_id or "", f"couldn't send the auth link: {message}")
            return
        view = _google_credentials_modal()
    else:
        view = _integration_modal(integration)
    if not view:
        return
    if not await _open_modal(client, body.get("trigger_id"), view):
        await _dm(client, user_id or "", "couldn't open that setup form. try `/nugget setup`.")


async def on_google_code_button(ack, body, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    if not await _open_modal(client, body.get("trigger_id"), _google_code_modal()):
        await _dm(client, user_id or "", "couldn't open the code form. try `/nugget setup google code`.")


async def on_google_creds_button(ack, body, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    if not await _open_modal(client, body.get("trigger_id"), _google_credentials_modal()):
        await _dm(
            client,
            user_id or "",
            "couldn't open the client form. try `/nugget setup google credentials`.",
        )


# ---------------------------------------------------------------------- google
def _google_ready() -> bool:
    creds = google_credentials(runtime.db, runtime.settings)
    return bool(creds["client_id"] and creds["client_secret"])


def _google_credentials_modal() -> dict[str, Any]:
    creds = google_credentials(runtime.db, runtime.settings)
    redirect = google_redirect_uri(runtime.db, runtime.settings)
    blocks = [
        _note(
            "*connect Google Workspace*\n"
            "you need a Google Cloud project with these APIs enabled: Calendar, "
            "Gmail, and Drive.\n\n"
            "1. open <https://console.cloud.google.com/apis/credentials|Google Cloud credentials>\n"
            "2. create an *OAuth client ID* of type *Desktop app*\n"
            f"3. add `{redirect}` as an authorized redirect URI\n"
            "4. paste the client id and secret below\n\n"
            "the client id is not very secret; the client secret stays in the bot's database."
        ),
        _input(
            "client_id",
            "Google OAuth client id",
            placeholder="1234567890-abc.apps.googleusercontent.com",
            initial=creds["client_id"] or None,
            max_length=300,
        ),
        _input(
            "client_secret",
            "Google OAuth client secret",
            placeholder="leave blank to keep the current secret",
            max_length=300,
        ),
        _input(
            "redirect_uri",
            "Redirect URI",
            initial=redirect,
            max_length=300,
        ),
        _note("next i'll DM you an authorization link. after approving, paste the redirected URL back in Slack."),
    ]
    return _modal("setup_google_credentials", "Connect Google", blocks, submit="Next")


async def on_google_credentials_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    values = _values(view)
    current = google_credentials(runtime.db, runtime.settings)
    client_id = _merge(values.get("client_id", ""), current["client_id"])
    secret = _merge(values.get("client_secret", ""), current["client_secret"])
    redirect = _merge(values.get("redirect_uri", ""), google_redirect_uri(runtime.db, runtime.settings))
    if not client_id or not secret:
        await _dm(client, user_id or "", "google needs both a client id and a client secret.")
        return

    changed = client_id != current["client_id"] or secret != current["client_secret"]
    _set("google_client_id", client_id)
    _set("google_client_secret", secret)
    _set("google_redirect_uri", redirect)
    if changed:
        _del("google_refresh_token", "google_access_token", "google_email", "google_oauth_state")
        if runtime.db:
            runtime.db.kv_del("cache:google_access_token")
            runtime.db.kv_del("cache:google_token_exp")

    ok, message = await _send_google_link(client, user_id or "")
    if not ok:
        await _dm(client, user_id or "", f"couldn't send the auth link: {message}")


async def _send_google_link(client, user_id: str) -> tuple[bool, str]:
    creds = google_credentials(runtime.db, runtime.settings)
    if not creds["client_id"] or not creds["client_secret"]:
        return False, "google client id and secret are not configured"
    redirect = google_redirect_uri(runtime.db, runtime.settings)
    state = secrets.token_urlsafe(24)
    if runtime.db:
        runtime.db.kv_set("config:google_oauth_state", state)
    url = build_auth_url(creds["client_id"], redirect, state)
    blocks = [
        _note(
            "*authorize Google*\n"
            f"open <{url}|this authorization link> and approve access.\n\n"
            "Google redirects to `localhost`, which probably shows an error page. "
            "that is expected: copy the *full URL from the address bar* and paste it "
            "back here. i only need the `code` parameter from it."
        ),
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Paste redirected URL"},
                    "action_id": "setup_google_code",
                    "value": "google",
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Change client"},
                    "action_id": "setup_google_creds",
                    "value": "google",
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"redirect URI: `{redirect}` · scopes: calendar, gmail modify, drive read",
                }
            ],
        },
    ]
    ok = await _dm(client, user_id, "connect google workspace", blocks=blocks)
    return ok, "sent you a DM with the authorization link." if ok else "Slack refused the DM."


def _google_code_modal() -> dict[str, Any]:
    return _modal(
        "setup_google_code",
        "Google authorization",
        [
            _note(
                "after Google redirects to localhost, copy the full URL from your "
                "browser's address bar and paste it here. a raw `code` value works too."
            ),
            _input(
                "code",
                "Redirected URL or authorization code",
                placeholder="http://localhost:9876/oauth2callback?state=...&code=4/...",
                multiline=True,
                optional=False,
                max_length=2000,
            ),
        ],
        submit="Connect",
    )


async def on_google_code_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    raw = _values(view).get("code", "")
    await _dm(client, user_id or "", await _complete_google_code(client, user_id or "", raw))


async def _complete_google_code(client, user_id: str, raw: str) -> str:
    code, state = parse_oauth_input(raw)
    if not code:
        return "that didn't contain a Google authorization code. paste the full redirected URL."

    if runtime.db:
        expected = runtime.db.kv_get("config:google_oauth_state")
        if state and expected and state != expected:
            return "that authorization link is from an older setup attempt. run `/nugget setup google` again."

    creds = google_credentials(runtime.db, runtime.settings)
    if not creds["client_id"] or not creds["client_secret"]:
        return "google client id and secret are missing. run `/nugget setup google` first."
    redirect = google_redirect_uri(runtime.db, runtime.settings)

    try:
        tokens = await exchange_code(
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            code=code,
            redirect_uri=redirect,
        )
    except GoogleOAuthError as exc:
        return f"google setup failed: {exc}"

    _set("google_access_token", tokens["access_token"])
    if tokens["refresh_token"]:
        _set("google_refresh_token", tokens["refresh_token"])
    if runtime.db:
        runtime.db.kv_set("cache:google_access_token", tokens["access_token"])
        runtime.db.kv_set(
            "cache:google_token_exp",
            str(time.time() + int(tokens["expires_in"])),
        )
        runtime.db.kv_del("config:google_oauth_state")

    try:
        identity = await fetch_identity(tokens["access_token"])
    except GoogleOAuthError as exc:
        return (
            "google tokens are saved, but the API check failed: "
            f"{exc} try `/nugget setup status` or reconnect."
        )

    email = identity.get("email") or ""
    _set("google_email", email)
    calendar = "calendar works" if identity.get("calendar_ok") else "calendar check failed"
    who = f" as {email}" if email else ""
    warning = ""
    if not tokens["refresh_token"] and not creds["refresh_token"]:
        warning = (
            " google did not return a refresh token, so access will expire; "
            "run `/nugget setup google` again to reconnect."
        )
    return f"google workspace connected{who}. {calendar}. gmail and drive are ready to use.{warning}"


# ------------------------------------------------------------------- other modals
def _integration_modal(integration: str) -> dict[str, Any] | None:
    if integration == "github":
        github = integrations.github_config(runtime.db, runtime.settings)
        return _modal(
            "setup_github",
            "Connect GitHub",
            [
                _note(
                    "username and repository are public. the token is optional for public "
                    "reads, but needed for notifications and higher rate limits.\n"
                    "create a fine-grained token with read access to your repos. "
                    "enter `-` in a field to clear it."
                ),
                _input("username", "GitHub username", placeholder="octocat", initial=github["username"] or None),
                _input("token", "GitHub token (optional)", placeholder="github_pat_... or ghp_...", max_length=500),
                _input(
                    "repo_url",
                    "Default repository",
                    placeholder="https://github.com/owner/repo",
                    initial=github["repo"] or None,
                    max_length=500,
                ),
            ],
            submit="Save GitHub",
        )
    if integration == "email":
        imap = integrations.imap_config(runtime.db, runtime.settings)
        return _modal(
            "setup_email",
            "Connect email",
            [
                _note(
                    "IMAP works with Gmail app passwords, Fastmail app passwords, and most "
                    "mail hosts. use an app password, not your account password."
                ),
                _input("host", "IMAP host", placeholder="imap.gmail.com", initial=imap["host"] or None),
                _input("port", "IMAP port", placeholder="993", initial=imap["port"] or "993", max_length=6),
                _input("user", "Email address", placeholder="you@example.com", initial=imap["user"] or None),
                _input("password", "App password", placeholder="leave blank to keep the current password", max_length=500),
            ],
            submit="Save email",
        )
    if integration == "mac":
        mac = integrations.mac_config(runtime.db, runtime.settings)
        return _modal(
            "setup_mac",
            "Connect Mac",
            [
                _note(
                    "run `python3 scripts/mac_companion.py --token YOUR_SECRET` on your Mac, "
                    "then expose it with a Cloudflare Tunnel or ngrok.\n"
                    "the URL is the public tunnel URL. leave it blank if this bot runs on the Mac itself."
                ),
                _input(
                    "url",
                    "Companion bridge URL",
                    placeholder="https://your-tunnel.trycloudflare.com",
                    initial=mac["url"] or None,
                    max_length=500,
                ),
                _input(
                    "token",
                    "Bridge token",
                    placeholder="leave blank to keep the current token",
                    max_length=500,
                ),
            ],
            submit="Save Mac",
        )
    if integration == "slack":
        return _modal(
            "setup_slack",
            "Your Slack profile",
            [
                _note(
                    "posting in your name works out of the box. the bot app posts with your "
                    "*display name and profile picture* (Slack `chat:write.customize`), so it "
                    "looks like you and still carries the small `APP` badge plus a "
                    "`Sent by` line.\n"
                    "optional: add *your own* user token for a fully native post with no app "
                    "badge. `xoxp-...`, or `xoxc-...` plus the `d` cookie from your Slack web "
                    "session. it is validated against your owner id before it is saved."
                ),
                _input(
                    "token",
                    "Optional: your own user token",
                    placeholder="xoxp-... or xoxc-... (blank keeps current, '-' clears)",
                    max_length=500,
                ),
                _input(
                    "cookie",
                    "Optional: Slack d cookie (only for xoxc)",
                    placeholder="leave blank to keep the current cookie",
                    max_length=1000,
                ),
            ],
            submit="Save",
        )
    return None


async def on_github_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    values = _values(view)
    current = integrations.github_config(runtime.db, runtime.settings)
    username = _merge(values.get("username", ""), current["username"])
    token = _merge(values.get("token", ""), current["token"])
    repo = _merge(values.get("repo_url", ""), current["repo"])

    warning = ""
    if token and token != current["token"]:
        try:
            async with httpx.AsyncClient(
                timeout=12,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "chicen-nugget-bot",
                    "Accept": "application/vnd.github+json",
                },
            ) as http:
                resp = await http.get("https://api.github.com/user")
            if resp.status_code == 401:
                await _dm(client, user_id or "", "GitHub rejected that token, so i didn't save it.")
                return
            if resp.status_code == 200 and not username:
                username = str((resp.json() or {}).get("login") or "")
        except Exception as exc:  # noqa: BLE001
            warning = f" saved, but token check failed: {exc}"

    _set("github_username", username)
    _set("github_token", token)
    _set("repo_url", repo)
    message = f"github connected as @{username}." if username else "github settings saved."
    if warning:
        message += warning
    await _dm(client, user_id or "", message)


async def on_email_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    values = _values(view)
    current = integrations.imap_config(runtime.db, runtime.settings)
    host = _merge(values.get("host", ""), current["host"])
    port = _merge(values.get("port", ""), current["port"]) or "993"
    user = _merge(values.get("user", ""), current["user"])
    password = _merge(values.get("password", ""), current["password"])
    if port.isdigit():
        _set("imap_port", port)
    _set("imap_host", host)
    _set("imap_user", user)
    _set("imap_password", password)
    if host and user and password:
        await _dm(client, user_id or "", f"email saved for {user} at {host}. ask me to check your inbox to verify.")
    else:
        await _dm(client, user_id or "", "email needs a host, address, and app password.")


async def on_mac_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    values = _values(view)
    current = integrations.mac_config(runtime.db, runtime.settings)
    url = _merge(values.get("url", ""), current["url"])
    token = _merge(values.get("token", ""), current["token"])
    _set("mac_bridge_url", url)
    _set("mac_bridge_token", token)
    if not url:
        await _dm(client, user_id or "", "mac bridge cleared. local macOS control still works if the bot runs on a Mac.")
        return
    try:
        async with httpx.AsyncClient(timeout=8) as http:
            resp = await http.get(f"{url.rstrip('/')}/health")
        if resp.status_code == 200:
            await _dm(client, user_id or "", "mac companion saved and reachable. try `what's my battery?`.")
        else:
            await _dm(client, user_id or "", f"mac companion saved, but health returned {resp.status_code}.")
    except Exception as exc:  # noqa: BLE001
        await _dm(client, user_id or "", f"mac companion saved, but i couldn't reach it: {exc}")


async def on_slack_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    values = _values(view)
    current = integrations.owner_token_config(runtime.db, runtime.settings)
    token = _merge(values.get("token", ""), current["token"])
    cookie = _merge(values.get("cookie", ""), current["cookie"])
    if not token:
        save_owner_credentials(runtime.db, token="", cookie="")
        deactivate_owner_posting()
        await _dm(
            client,
            user_id or "",
            "no owner token stored. posting in your name will use your display name and "
            "profile picture on a bot message (app badge visible).",
        )
        return
    if token.startswith("xoxp-"):
        cookie = ""

    result = await validate_owner_token(
        token=token, cookie=cookie, settings=runtime.settings, quiet=True
    )
    if result.error:
        await _dm(client, user_id or "", f"owner token not saved: {result.error}")
        return

    save_owner_credentials(runtime.db, token=token, cookie=cookie)
    runtime.owner_client = result.client
    runtime.owner_user_id = result.user_id
    await _dm(
        client,
        user_id or "",
        f"native owner posting enabled as <@{result.user_id}>. messages posted with "
        "`as_user=true` now come from your real profile with no app badge, plus the "
        "small `Sent by` line.",
    )


# ------------------------------------------------------------------ disconnect
async def _disconnect(client, command: dict[str, Any], integration: str) -> None:
    if not integration:
        await _say(client, command, "usage: `/nugget setup disconnect google|mac|github|email|slack`")
        return
    if integration == "all":
        targets = list(INTEGRATION_NAMES)
    elif integration in INTEGRATION_NAMES:
        targets = [integration]
    else:
        await _say(client, command, f"unknown integration `{integration}`.")
        return

    for name in targets:
        clear_integration(runtime.db, name)
        if name == "slack":
            deactivate_owner_posting()
    await _say(
        client,
        command,
        f":white_check_mark: cleared {', '.join(INTEGRATION_NAMES[n] for n in targets)}. "
        "environment variables may still provide defaults after a restart.",
    )

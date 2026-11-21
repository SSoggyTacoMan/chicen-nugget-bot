"""Chat-based integration setup.

Everything the bot can connect to can be configured from Slack: Google
Workspace, the Mac companion, GitHub, IMAP email, and owner-profile posting.
Secrets are collected in Slack modals instead of chat messages or `.env` edits.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx

from bot import integrations
from bot.api_keys import API_SERVICES, api_token, save_api_token
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
from bot.slack_api import all_channels, error_code, post, post_ephemeral

logger = logging.getLogger(__name__)

INTEGRATION_NAMES: dict[str, str] = {
    "google": "Google Workspace",
    "mac": "Mac companion",
    "github": "GitHub",
    "email": "Email",
    "apis": "API keys",
    "slack": "your Slack profile",
}
CONFIG_NAMES = {"ai", "voice", "images", "welcome", "fallback", "watchers", "storage", "channels"}


@dataclass(frozen=True)
class SetupQuestion:
    """One question in a conversational setup flow.

    ``required`` questions are asked in order, one at a time. Optional ones are
    offered once the required answers are in, so a ten-field modal becomes a
    short chat instead of an interrogation.
    """

    key: str
    ask: str
    accepts: str
    required: bool = False


@dataclass(frozen=True)
class SetupGuide:
    title: str
    intro: str
    questions: tuple[SetupQuestion, ...]
    note: str = ""


CHAT_SETUP_AREAS: dict[str, SetupGuide] = {
    "welcome": SetupGuide(
        title="welcome & ping group",
        intro=(
            "welcomes live in the channels i watch. every message can use "
            "`{user}`, `{channel}`, `{bot}` and `{name}`."
        ),
        questions=(
            SetupQuestion(
                "watched_channel_ids",
                "which channels should i welcome people in? give me #names, ids, "
                "`all` for every channel i'm in, or `off`.",
                "#general, #doggy-taco-fellas, a comma list, `all`, or `off`",
                required=True,
            ),
            SetupQuestion(
                "welcome_public",
                "should i post a welcome message in the channel when someone joins?",
                "on or off",
                required=True,
            ),
            SetupQuestion(
                "welcome_dm_enabled",
                "should i also DM each new member a welcome?",
                "on or off",
                required=True,
            ),
            SetupQuestion(
                "welcome_message",
                "what should the public welcome say?",
                "text with {user} / {channel}; `default` keeps the current one",
            ),
            SetupQuestion(
                "welcome_dm",
                "what should the welcome DM say?",
                "text with {user} / {channel} / {bot}",
            ),
            SetupQuestion(
                "welcome_dm_once",
                "DM new members only on their first ever join?",
                "on or off",
            ),
            SetupQuestion(
                "welcome_thread_reply",
                "want a small reply in the thread under the join ping?",
                "on or off",
            ),
            SetupQuestion(
                "welcome_thread_text",
                "what should that thread reply say?",
                "text, e.g. `hey {user} :wave:`",
            ),
            SetupQuestion(
                "welcome_ping",
                "any fixed ping to add to every welcome?",
                "a user/usergroup mention, or `off`",
            ),
            SetupQuestion(
                "usergroup_id",
                "is there a ping group (Slack usergroup id, looks like S0123ABCD) "
                "new members should be offered?",
                "usergroup id, or `off`",
            ),
            SetupQuestion(
                "welcome_ping_auto_add",
                "auto-add joiners to that ping group?",
                "on or off",
            ),
            SetupQuestion(
                "welcome_ping_button",
                "put a join/leave ping-group button in the welcome?",
                "on or off",
            ),
            SetupQuestion(
                "usergroup_prompt",
                "ask new members to join the ping group?",
                "on or off",
            ),
            SetupQuestion(
                "usergroup_prompt_text",
                "what should that ping-group ask say?",
                "text",
            ),
        ),
        note=(
            "`/nugget setup welcome` still opens the full modal, and a "
            "per-channel welcome override lives there too. mention that once at the end."
        ),
    ),
    "ai": SetupGuide(
        title="AI & behavior",
        intro="models take effect on new turns. `default` keeps what is set now.",
        questions=(
            SetupQuestion(
                "recaps",
                "want daily recaps?",
                "on or off",
            ),
            SetupQuestion(
                "thread_followups",
                "should i reply in threads i'm already part of without being tagged?",
                "on or off",
            ),
            SetupQuestion(
                "ambient_ai",
                "should i ever chime in unprompted in channels?",
                "on or off",
            ),
            SetupQuestion(
                "welcomer",
                "keep the welcomer / onboarding flow on?",
                "on or off",
            ),
            SetupQuestion(
                "personal_updates",
                "send you the private digest and recap reminder DMs?",
                "on or off",
            ),
            SetupQuestion(
                "ai_model",
                "which model should the bot use?",
                "a model id, or `default`",
            ),
            SetupQuestion(
                "ai_assistant_model",
                "which model should Boi (your personal assistant) use?",
                "a model id, or `default`",
            ),
        ),
    ),
    "voice": SetupGuide(
        title="voice",
        intro="qwen3 tts powers `/nugget speak` and the `speak` tool.",
        questions=(
            SetupQuestion(
                "tts_model",
                "which tts model?",
                "a model id, or `default`",
            ),
            SetupQuestion(
                "tts_voice",
                "which voice?",
                "Aiden, Ryan, Serena, Vivian, Dylan, Eric, Uncle_fu, Ono_anna, or Sohee",
            ),
            SetupQuestion(
                "tts_language",
                "which language?",
                "auto, English, Chinese, Japanese, Korean, French, German, Italian, Spanish, Portuguese, or Russian",
            ),
        ),
        note="leave style, char cap and transcription models out unless asked.",
    ),
    "images": SetupGuide(
        title="images",
        intro="nano banana models power `/nugget imagine` and the image tool.",
        questions=(
            SetupQuestion(
                "imagine_model",
                "which image model should i use by default?",
                "a model from the images setup list",
            ),
            SetupQuestion(
                "imagine_fallback_model",
                "want a fallback image model?",
                "a model id or `none`",
            ),
            SetupQuestion(
                "imagine_cost_usd",
                "how much should each image count against the daily budget, in USD?",
                "a number like `0.04`",
            ),
        ),
    ),
    "fallback": SetupGuide(
        title="fallback AI",
        intro="only used when the main provider returns a quota or payment error.",
        questions=(
            SetupQuestion(
                "ai_router_base_url",
                "what is the fallback base URL?",
                "an https URL",
            ),
            SetupQuestion(
                "ai_router_models",
                "which fallback models, in order?",
                "one model id per line",
            ),
        ),
        note=(
            "the API key is a secret and cannot be typed in chat. finish by sending "
            "the fallback setup panel with `integration_setup` so they can paste the key "
            "into the modal."
        ),
    ),
    "watchers": SetupGuide(
        title="watchers",
        intro="watchers baseline on first run and honor quiet hours plus a daily cap.",
        questions=(
            SetupQuestion(
                "watch_github_ci",
                "watch failed GitHub Actions runs?",
                "on or off",
            ),
            SetupQuestion(
                "watch_github_notifications",
                "watch new GitHub notifications?",
                "on or off",
            ),
            SetupQuestion(
                "watch_email",
                "watch new unread email? (email must already be connected)",
                "on or off",
            ),
            SetupQuestion(
                "watch_urls",
                "any URLs i should watch for changes?",
                "comma-separated https links, or `off`",
            ),
        ),
        note="intervals are optional; only set them if they ask.",
    ),
    "storage": SetupGuide(
        title="storage",
        intro="`0` disables a retention rule. `/nugget storage prune` applies it now.",
        questions=(
            SetupQuestion(
                "memory_retention_days",
                "how many days of conversation history should i keep?",
                "a number, `0` to keep forever",
            ),
            SetupQuestion(
                "sandbox_ttl_days",
                "how many days should sandbox workspaces stick around?",
                "a number",
            ),
            SetupQuestion(
                "storage_min_free_gb",
                "warn when free disk drops below how many GB?",
                "a number",
            ),
        ),
    ),
    "channels": SetupGuide(
        title="auto-join channels",
        intro="public channels i should join on startup. private ones still need an invite.",
        questions=(
            SetupQuestion(
                "auto_join_channels",
                "which public channels should i auto-join?",
                "#names, comma-separated ids, or `off`",
            ),
        ),
    ),
}

# Friendly names people actually type, mapped onto the guide keys.
CHAT_SETUP_ALIASES: dict[str, str] = {
    "welcomer": "welcome",
    "welcome": "welcome",
    "onboarding": "welcome",
    "greeting": "welcome",
    "welcome_message": "welcome",
    "ai": "ai",
    "behavior": "ai",
    "behaviour": "ai",
    "models": "ai",
    "voice": "voice",
    "tts": "voice",
    "image": "images",
    "images": "images",
    "imagine": "images",
    "fallback": "fallback",
    "9router": "fallback",
    "watchers": "watchers",
    "watcher": "watchers",
    "storage": "storage",
    "retention": "storage",
    "channels": "channels",
    "autochannels": "channels",
    "auto_join": "channels",
}


def chat_setup_area(name: str) -> str | None:
    """Resolve a typed setup area, e.g. ``welcomer`` or ``Voice``."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    if not cleaned:
        return None
    return CHAT_SETUP_ALIASES.get(cleaned, cleaned if cleaned in CHAT_SETUP_AREAS else None)


def _current_setting(key: str) -> str:
    db = runtime.db
    if db is not None:
        value = db.kv_get(f"config:{key}")
        if value is not None:
            return value or "(cleared)"
    default = getattr(runtime.settings, key, None)
    if isinstance(default, bool):
        return "on" if default else "off"
    if isinstance(default, (frozenset, list, tuple, set)):
        joined = ", ".join(str(item) for item in default)
        return joined or "(none)"
    if default is None:
        return "(not set)"
    text = str(default).strip()
    return text or "(not set)"


def chat_setup_script(area: str) -> str | None:
    """Model-facing script for a guided setup conversation.

    The model asks the required questions one at a time and applies each answer
    with ``bot_config action=set``. No secret ever belongs in this path.
    """
    resolved = chat_setup_area(area)
    guide = CHAT_SETUP_AREAS.get(resolved or "")
    if guide is None:
        return None

    lines = [
        f"guided chat setup: {guide.title} (area: {resolved})",
        guide.intro,
        "",
        "how to run it:",
        "- ask the required questions in order, ONE short question per message. never dump the whole list.",
        "- after each real answer, apply it with `bot_config` `action=set`, then ask the next one.",
        "- keep the current value when they say skip / whatever / default / idk.",
        "- after the required ones, offer the optional tweaks in a single short line and stop if they don't care.",
        "- when done, summarize what changed in one or two lines. do not repeat the whole config.",
        f"- if you lose track mid-flow, call `nugget_command` with `setup {resolved}` again to re-read this script.",
    ]
    required = [q for q in guide.questions if q.required]
    optional = [q for q in guide.questions if not q.required]

    def describe(index: int, question: SetupQuestion) -> list[str]:
        current = _current_setting(question.key)
        lines_out = [
            f"{index}. ask: {question.ask}",
            f"   apply: bot_config action=set key={question.key} value=<answer>",
            f"   accepts: {question.accepts}",
            f"   current: {current}",
        ]
        return lines_out

    lines.append("")
    if required:
        lines.append("required:")
        for index, question in enumerate(required, 1):
            lines.extend(describe(index, question))
    if optional:
        lines.append("optional (offer together, don't ask one by one):")
        for index, question in enumerate(optional, 1):
            lines.extend(describe(index, question))
    if guide.note:
        lines.append("")
        lines.append(f"note: {guide.note}")
    return "\n".join(lines)



def register(app) -> None:
    app.action("setup_open")(on_setup_open)
    app.action("setup_google_code")(on_google_code_button)
    app.action("setup_google_creds")(on_google_creds_button)
    app.view("setup_google_credentials")(on_google_credentials_submit)
    app.view("setup_google_code")(on_google_code_submit)
    app.view("setup_github")(on_github_submit)
    app.view("setup_email")(on_email_submit)
    app.view("setup_apis")(on_apis_submit)
    app.view("setup_ai")(on_ai_submit)
    app.view("setup_voice")(on_voice_submit)
    app.view("setup_images")(on_images_submit)
    app.view("setup_welcome")(on_welcome_submit)
    app.view("setup_fallback_ai")(on_fallback_ai_submit)
    app.view("setup_watchers")(on_watchers_submit)
    app.view("setup_storage")(on_storage_submit)
    app.view("setup_channels")(on_channels_submit)
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
    # The setup panel writes the owner's tokens (slack user session, gmail,
    # github, mac bridge), so it follows private access, not just admin status.
    from bot.config import Settings

    settings = runtime.settings
    if not user_id:
        return False
    if isinstance(settings, Settings):
        return settings.private_access(user_id)
    return settings.is_admin(user_id)


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


def _checkboxes(
    block_id: str,
    label: str,
    options: list[tuple[str, str]],
    selected: set[str],
) -> dict[str, Any]:
    elements = [
        {"text": {"type": "plain_text", "text": text}, "value": value}
        for value, text in options
    ]
    element: dict[str, Any] = {
        "type": "checkboxes",
        "action_id": "value",
        "options": elements,
    }
    initial = [item for item in elements if item["value"] in selected]
    if initial:
        element["initial_options"] = initial
    return {
        "type": "input",
        "block_id": block_id,
        "optional": True,
        "label": {"type": "plain_text", "text": label},
        "element": element,
    }


def _checked(view: dict[str, Any], block_id: str) -> set[str]:
    values = (view.get("state") or {}).get("values") or {}
    element = ((values.get(block_id) or {}).get("value") or {})
    return {
        str(item.get("value"))
        for item in (element.get("selected_options") or [])
        if item.get("value")
    }


def _select(
    block_id: str,
    label: str,
    options: list[tuple[str, str]],
    selected: str,
) -> dict[str, Any]:
    elements = [
        {"text": {"type": "plain_text", "text": text}, "value": value}
        for value, text in options
    ]
    element: dict[str, Any] = {
        "type": "static_select",
        "action_id": "value",
        "options": elements,
    }
    for item in elements:
        if item["value"] == selected:
            element["initial_option"] = item
            break
    return {
        "type": "input",
        "block_id": block_id,
        "optional": True,
        "label": {"type": "plain_text", "text": label},
        "element": element,
    }


def _selected(view: dict[str, Any], block_id: str) -> str:
    values = (view.get("state") or {}).get("values") or {}
    element = ((values.get(block_id) or {}).get("value") or {})
    selected = element.get("selected_option") or {}
    return str(selected.get("value") or "")


def _current(key: str, default: str = "") -> str:
    raw = runtime.db.kv_get(f"config:{key}") if runtime.db else None
    return str(raw) if raw not in (None, "") else default


def _current_bool(key: str, default: bool = True) -> bool:
    raw = runtime.db.kv_get(f"config:{key}") if runtime.db else None
    if raw is None:
        return default
    return str(raw).lower() not in {"false", "0", "no", "off"}


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


async def _resolve_channel(client, raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    match = re.match(r"<#([CG][A-Z0-9]+)(?:\|[^>]+)?>", text)
    if match:
        return match.group(1)
    if re.match(r"^[CG][A-Z0-9]{5,}$", text):
        return text
    name = text.lstrip("#").lower()
    try:
        channels = await all_channels(client)
    except Exception:  # noqa: BLE001
        return ""
    for channel in channels:
        if channel.get("id") == text or (channel.get("name") or "").lower() == name:
            return str(channel.get("id") or "")
    return ""


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
    user_id = command.get("user_id")
    text = (command.get("text") or "").strip()
    parts = text.split()
    action = parts[0].lower() if parts else ""

    if not _is_admin(user_id):
        await ack()
        await _say(client, command, "only the owner/admin can set up integrations.")
        return

    if not action or action in {"panel", "home"}:
        # Respond through the slash-command ack. chat.postEphemeral needs the
        # app to be in the conversation, which it isn't in a DM between the
        # user and Boi; the silent fallback DM then looks like the command did
        # nothing. the ack response renders in whatever conversation ran it.
        blocks = setup_panel_blocks()
        if command.get("trigger_id") or command.get("response_url"):
            await ack(text="integration setup", blocks=blocks)
        else:
            await ack()
            await post_ephemeral(
                client,
                command.get("channel_id") or user_id or "",
                user_id or "",
                "integration setup",
                blocks=blocks,
            )
        return

    await ack()

    if action == "status":
        await _say(client, command, _status())
        return

    if action in {"help", "?"}:
        await _say(client, command, _usage())
        return

    if action == "disconnect":
        await _disconnect(client, command, parts[1].lower() if len(parts) > 1 else "")
        return

    if action in CONFIG_NAMES:
        view = _config_modal(action)
        if not view or not await _open_modal(
            client, command.get("trigger_id"), view
        ):
            await _say(client, command, "couldn't open that setup form. try again in a second.")
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
        "• `/nugget setup ai` - models and behavior toggles\n"
        "• `/nugget setup voice` - qwen3 tts voice, language and style\n"
        "• `/nugget setup images` - nano banana model, fallback and budget cost\n"
        "• `/nugget setup welcome` - welcome messages, watched channels and ping group\n"
        "• `/nugget setup fallback` - 9router key and model for empty HCAI quota\n"
        "• `/nugget setup watchers` - CI, notifications, email, URLs\n"
        "• `/nugget setup storage` - retention and cleanup\n"
        "• `/nugget setup channels` - auto-join public channels\n"
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
    elif integration in {"ai", "voice", "images", "welcome", "fallback", "watchers", "storage", "channels"}:
        view = _config_modal(integration)
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
        if not state:
            return (
                "that didn't include the OAuth state parameter. paste the full URL "
                "from your browser's address bar, then run `/nugget setup google` "
                "again if it keeps failing."
            )
        if not expected or not secrets.compare_digest(state, expected):
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
def _config_modal(integration: str) -> dict[str, Any] | None:
    settings = runtime.settings
    if integration == "ai":
        selected = {
            key
            for key, default in (
                ("recaps", True),
                ("thread_followups", True),
                ("ambient_ai", True),
                ("welcomer", True),
                ("personal_updates", True),
            )
            if _current_bool(key, default)
        }
        return _modal(
            "setup_ai",
            "AI and behavior",
            [
                _note(
                    "models apply to new turns. leave a field blank to keep it, or enter `-` to fall back to the env default."
                ),
                _input("ai_model", "Bot model", initial=_current("ai_model", settings.ai_model)),
                _input(
                    "ai_assistant_model",
                    "Boi model",
                    initial=_current("ai_assistant_model", settings.ai_assistant_model),
                ),
                _input(
                    "ai_assistant_fast_model",
                    "Boi quick-chat model",
                    initial=_current(
                        "ai_assistant_fast_model",
                        settings.ai_assistant_fast_model or "",
                    ),
                ),
                _input(
                    "ai_assistant_fast_fallback_model",
                    "Boi quick-chat fallback",
                    initial=_current(
                        "ai_assistant_fast_fallback_model",
                        settings.ai_assistant_fast_fallback_model or "",
                    ),
                ),
                _input("ai_code_model", "Code model", initial=_current("ai_code_model", settings.ai_code_model or "")),
                _checkboxes(
                    "behavior",
                    "Behavior",
                    [
                        ("recaps", "daily recaps"),
                        ("thread_followups", "unprompted thread replies"),
                        ("ambient_ai", "ambient replies"),
                        ("welcomer", "welcomer / onboarding"),
                        ("personal_updates", "owner digest and recap DMs"),
                    ],
                    selected,
                ),
            ],
            submit="Save AI",
        )
    if integration == "voice":
        from bot.ai import tts

        cfg = tts.voice_config(settings, runtime.db)
        return _modal(
            "setup_voice",
            "Voice",
            [
                _note(
                    "qwen3 tts through hack club ai. `/nugget speak` and the speak tool use these defaults. leave a field blank to keep it, or enter `-` to fall back to env."
                ),
                _input(
                    "tts_model",
                    "TTS model",
                    initial=cfg.model,
                ),
                _select(
                    "tts_voice",
                    "Voice",
                    list(tts.VOICES),
                    cfg.voice,
                ),
                _select(
                    "tts_language",
                    "Language",
                    list(tts.LANGUAGES),
                    cfg.language,
                ),
                _input(
                    "tts_style",
                    "Style instruction",
                    placeholder="speak slowly and calmly",
                    initial=cfg.style,
                    max_length=200,
                ),
                _input(
                    "tts_max_chars",
                    "Max characters",
                    initial=str(cfg.max_chars),
                    max_length=4,
                ),
                _input(
                    "transcribe_model",
                    "Transcription model",
                    initial=_current("transcribe_model", settings.transcribe_model),
                ),
                _input(
                    "transcribe_fallback_model",
                    "Transcription fallback",
                    initial=_current(
                        "transcribe_fallback_model",
                        settings.transcribe_fallback_model or "",
                    ),
                ),
            ],
            submit="Save voice",
        )
    if integration == "images":
        from bot.ai import images as image_models

        cfg = image_models.imagine_config(settings, runtime.db)
        return _modal(
            "setup_images",
            "Images",
            [
                _note(
                    "nano banana 2 is the default. 2.1 is newer, pro is best but slowest and priciest, lite is cheapest. the cost field is what `/nugget budget` charges per image, not what google bills."
                ),
                _select(
                    "imagine_model",
                    "Image model",
                    list(image_models.MODEL_OPTIONS),
                    cfg.model or image_models.DEFAULT_MODEL,
                ),
                _select(
                    "imagine_fallback_model",
                    "Fallback model",
                    list(image_models.FALLBACK_OPTIONS),
                    cfg.fallback or "none",
                ),
                _input(
                    "imagine_cost_usd",
                    "Budget charge per image (USD)",
                    initial=str(cfg.cost_usd),
                    max_length=8,
                ),
            ],
            submit="Save images",
        )
    if integration == "welcome":
        from bot.features.welcomer import welcome_config

        cfg = welcome_config()
        selected = {
            key
            for key, enabled in (
                ("welcome_public", cfg.public),
                ("welcome_dm_enabled", cfg.dm_enabled),
                ("welcome_dm_once", cfg.dm_once),
                ("welcome_ping_auto_add", cfg.auto_add),
                ("welcome_ping_button", cfg.ping_button),
                ("welcome_thread_reply", cfg.thread_reply),
                ("usergroup_prompt", cfg.prompt),
                ("usergroup_prompt_once", cfg.prompt_once),
            )
            if enabled
        }
        return _modal(
            "setup_welcome",
            "Welcome & ping group",
            [
                _note(
                    "placeholders: `{user}`, `{channel}`, `{bot}`, `{name}`. watched channels can be ids or #names; use `*` for every channel the bot is in. a per-channel override only fires when that channel is also watched."
                ),
                _input(
                    "watched_channel_ids",
                    "Watched channels",
                    placeholder="C0123ABCD,C0456EFGH or *",
                    initial=",".join(cfg.watched),
                    max_length=1500,
                ),
                _input(
                    "welcome_message",
                    "Public welcome",
                    initial=cfg.message,
                    multiline=True,
                    max_length=1500,
                ),
                _input(
                    "welcome_dm",
                    "Welcome DM",
                    initial=cfg.dm,
                    multiline=True,
                    max_length=1500,
                ),
                _input(
                    "welcome_thread_text",
                    "Thread reply text",
                    placeholder="hey {user} :wave:",
                    initial=cfg.thread_text,
                    max_length=800,
                ),
                _input(
                    "welcome_ping",
                    "Welcome ping line",
                    placeholder="optional, e.g. <@S0123> or a usergroup mention",
                    initial=cfg.ping,
                    max_length=200,
                ),
                _input(
                    "usergroup_id",
                    "Ping group id",
                    placeholder="S0123ABCD",
                    initial=cfg.usergroup_id or "",
                    max_length=40,
                ),
                _input(
                    "usergroup_prompt_text",
                    "Ping group prompt",
                    initial=cfg.prompt_text,
                    multiline=True,
                    max_length=800,
                ),
                _checkboxes(
                    "welcome_toggles",
                    "Toggles",
                    [
                        ("welcome_public", "send public welcome"),
                        ("welcome_dm_enabled", "send welcome DM"),
                        ("welcome_dm_once", "DM only on first join"),
                        ("welcome_ping_auto_add", "auto-add to ping group"),
                        ("welcome_ping_button", "join/leave button in welcome"),
                        ("welcome_thread_reply", "post a thread reply"),
                        ("usergroup_prompt", "ask new members to join the ping group"),
                        ("usergroup_prompt_once", "only ask once"),
                    ],
                    selected,
                ),
                _note(
                    "*per-channel override* - optional. useful for #doggy-taco-fellas."
                ),
                _input(
                    "welcome_override_channel",
                    "Channel",
                    placeholder="#doggy-taco-fellas or C0123ABCD",
                    max_length=100,
                ),
                _input(
                    "welcome_override_message",
                    "Override public welcome",
                    multiline=True,
                    max_length=1500,
                ),
                _input(
                    "welcome_override_dm",
                    "Override welcome DM",
                    multiline=True,
                    max_length=1500,
                ),
            ],
            submit="Save welcome",
        )
    if integration == "fallback":
        from bot.ai.providers import parse_models

        models_raw = _current("ai_router_models") or _current("ai_router_model")
        initial_models = (
            "\n".join(parse_models(models_raw))
            if models_raw
            else "\n".join(settings.ai_router_models)
        )
        return _modal(
            "setup_fallback_ai",
            "Fallback AI",
            [
                _note(
                    "used only when hack club ai returns a quota/payment error. models are tried top to bottom. 9router is text-only here; voice, images and memory search still need HCAI quota."
                ),
                _input(
                    "ai_router_base_url",
                    "9router base URL",
                    initial=_current("ai_router_base_url", settings.ai_router_base_url),
                    max_length=300,
                ),
                _input(
                    "ai_router_models",
                    "Fallback models (in order)",
                    placeholder="kgw/kilo-auto/free\nother/model\nbackup/model",
                    initial=initial_models,
                    multiline=True,
                    max_length=1000,
                ),
                _input(
                    "ai_router_api_key",
                    "9router API key",
                    placeholder="paste the key; blank keeps the current one",
                    max_length=300,
                ),
            ],
            submit="Save fallback",
        )
    if integration == "watchers":
        from bot.features.watchers import watcher_config

        cfg = watcher_config()
        selected = {
            key
            for key, enabled in (
                ("watch_github_ci", cfg["github_ci"]),
                ("watch_github_notifications", cfg["github_notifications"]),
                ("watch_email", cfg["email"]),
            )
            if enabled
        }
        return _modal(
            "setup_watchers",
            "Watchers",
            [
                _note(
                    "all watchers baseline on first run and honor quiet hours plus the daily cap. email needs `/nugget setup email`."
                ),
                _checkboxes(
                    "watchers",
                    "Enable",
                    [
                        ("watch_github_ci", "failed GitHub Actions runs"),
                        ("watch_github_notifications", "new GitHub notifications"),
                        ("watch_email", "new unread email"),
                    ],
                    selected,
                ),
                _input(
                    "watch_urls",
                    "URLs to watch",
                    placeholder="https://example.com,https://status.example.com",
                    initial=",".join(cfg["urls"]),
                    max_length=1500,
                ),
                _input(
                    "watch_github_ci_interval",
                    "CI interval (seconds)",
                    initial=str(cfg["github_ci_interval"]),
                    max_length=6,
                ),
                _input(
                    "watch_github_notifications_interval",
                    "Notification interval (seconds)",
                    initial=str(cfg["github_notifications_interval"]),
                    max_length=6,
                ),
                _input(
                    "watch_email_interval",
                    "Email interval (seconds)",
                    initial=str(cfg["email_interval"]),
                    max_length=6,
                ),
                _input(
                    "watch_url_interval",
                    "URL interval (seconds)",
                    initial=str(cfg["url_interval"]),
                    max_length=6,
                ),
            ],
            submit="Save watchers",
        )
    if integration == "storage":
        from bot.features import retention

        return _modal(
            "setup_storage",
            "Storage",
            [
                _note(
                    "`0` disables a retention rule. run `/nugget storage prune` to apply now."
                ),
                _input(
                    "memory_retention_days",
                    "History retention (days)",
                    initial=str(retention._cfg_int(runtime.db, "memory_retention_days", settings.memory_retention_days)),
                    max_length=5,
                ),
                _input(
                    "sandbox_ttl_days",
                    "Sandbox TTL (days)",
                    initial=str(retention._cfg_int(runtime.db, "sandbox_ttl_days", settings.sandbox_ttl_days)),
                    max_length=5,
                ),
                _input(
                    "storage_min_free_gb",
                    "Low disk warning (GB)",
                    initial=str(retention._cfg_float(runtime.db, "storage_min_free_gb", settings.storage_min_free_gb)),
                    max_length=8,
                ),
            ],
            submit="Save storage",
        )
    if integration == "channels":
        return _modal(
            "setup_channels",
            "Auto-join channels",
            [
                _note(
                    "comma separated public channel ids. empty joins nothing. private channels still need an invite. enter `-` to clear and fall back to env."
                ),
                _input(
                    "auto_join_channels",
                    "Channel ids",
                    placeholder="C0123ABCD,C0456EFGH",
                    initial=_current(
                        "auto_join_channels", ",".join(settings.auto_join_channels)
                    ),
                    max_length=2000,
                ),
            ],
            submit="Save channels",
        )
    return None


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
    if integration == "apis":
        return _modal(
            "setup_apis",
            "API keys",
            [
                _note(
                    "optional tokens for the generic `api_request` tool. leave a field "
                    "blank to keep it, or enter `-` to clear it. tokens are stored in "
                    "SQLite and never echoed back."
                ),
                _input("vercel", "Vercel token", placeholder="vercel_...", max_length=500),
                _input("cloudflare", "Cloudflare API token", placeholder="cloudflare token", max_length=500),
                _input("notion", "Notion integration token", placeholder="secret_...", max_length=500),
                _input("linear", "Linear API key", placeholder="lin_api_...", max_length=500),
                _input("sentry", "Sentry auth token", placeholder="sntrys_...", max_length=500),
                _input("resend", "Resend API key", placeholder="re_...", max_length=500),
            ],
            submit="Save API keys",
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
                    "posting in your name needs *no personal token*. the bot app posts with "
                    "your *display name and profile picture* (Slack `chat:write.customize`), "
                    "so it looks like you and still carries the small `APP` badge plus a "
                    "`Sent by` line.\n"
                    "if Slack shows the bot's own name/avatar instead, the app needs a "
                    "reinstall so the scope takes effect. the fields below are advanced and "
                    "optional: add *your own* user token only if you want a fully native "
                    "post with no app badge."
                ),
                _input(
                    "token",
                    "Advanced: native posting token (optional)",
                    placeholder="xoxp-... or xoxc-... (blank keeps current, '-' clears)",
                    max_length=500,
                ),
                _input(
                    "cookie",
                    "Advanced: Slack d cookie (only for xoxc)",
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


async def on_apis_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    values = _values(view)
    saved: list[str] = []
    cleared: list[str] = []
    for service, preset in API_SERVICES.items():
        raw = values.get(service, "")
        if not raw:
            continue
        merged = _merge(raw, api_token(runtime.db, runtime.settings, service))
        save_api_token(runtime.db, service, merged)
        if merged:
            saved.append(str(preset["label"]))
        else:
            cleared.append(str(preset["label"]))
    if cleared:
        await _dm(client, user_id or "", f"cleared API keys: {', '.join(cleared)}.")
    elif saved:
        await _dm(
            client,
            user_id or "",
            f"saved API keys: {', '.join(saved)}. ask me to call the API when you need it.",
        )
    else:
        await _dm(client, user_id or "", "no API key changes.")


async def on_ai_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    values = _values(view)
    for key in (
        "ai_model",
        "ai_assistant_model",
        "ai_assistant_fast_model",
        "ai_assistant_fast_fallback_model",
        "ai_code_model",
    ):
        raw = values.get(key, "")
        if not raw:
            continue
        _set(key, "" if raw.strip() == "-" else raw.strip())
    checked = _checked(view, "behavior")
    for key in ("recaps", "thread_followups", "ambient_ai", "welcomer", "personal_updates"):
        _set(key, "true" if key in checked else "false")
    await _dm(client, user_id or "", "saved AI and behavior settings.")


async def on_voice_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    from bot.ai import tts

    values = _values(view)
    raw_model = values.get("tts_model", "")
    if raw_model:
        _set("tts_model", "" if raw_model.strip() == "-" else raw_model.strip())
    raw_style = values.get("tts_style", "")
    if raw_style:
        _set("tts_style", "" if raw_style.strip() == "-" else raw_style.strip())
    voice = _selected(view, "tts_voice")
    if voice in {value for value, _ in tts.VOICES}:
        _set("tts_voice", voice)
    language = _selected(view, "tts_language")
    if language in {value for value, _ in tts.LANGUAGES}:
        _set("tts_language", language)
    raw_chars = values.get("tts_max_chars", "")
    if raw_chars.isdigit():
        _set("tts_max_chars", str(max(50, min(1500, int(raw_chars)))))
    for key in ("transcribe_model", "transcribe_fallback_model"):
        raw = values.get(key, "")
        if raw:
            _set(key, "" if raw.strip() == "-" else raw.strip())
    await _dm(client, user_id or "", "saved voice settings.")


async def on_images_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    from bot.ai import images as image_models

    model = _selected(view, "imagine_model")
    if model in {value for value, _ in image_models.MODEL_OPTIONS}:
        _set("imagine_model", model)
    fallback = _selected(view, "imagine_fallback_model")
    if fallback == "none":
        _set("imagine_fallback_model", "none")
    elif fallback in {value for value, _ in image_models.MODEL_OPTIONS}:
        _set("imagine_fallback_model", fallback)
    raw_cost = _values(view).get("imagine_cost_usd", "")
    try:
        if raw_cost:
            _set("imagine_cost_usd", str(max(0.0, float(raw_cost))))
    except ValueError:
        pass
    await _dm(client, user_id or "", "saved image settings.")


async def on_welcome_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    values = _values(view)
    warnings: list[str] = []

    raw_watched = values.get("watched_channel_ids", "")
    if raw_watched.strip() == "-":
        _set("watched_channel_ids", "")
    elif raw_watched:
        resolved: list[str] = []
        for part in re.split(r"[,\s]+", raw_watched):
            part = part.strip()
            if not part:
                continue
            if part == "*":
                resolved.append("*")
                continue
            channel_id = await _resolve_channel(client, part)
            if channel_id:
                resolved.append(channel_id)
            else:
                warnings.append(f"couldn't resolve `{part}`")
        if resolved:
            _set("watched_channel_ids", ",".join(dict.fromkeys(resolved)))

    for key in (
        "welcome_message",
        "welcome_dm",
        "welcome_thread_text",
        "welcome_ping",
        "usergroup_id",
        "usergroup_prompt_text",
    ):
        raw = values.get(key, "")
        if raw:
            _set(key, "" if raw.strip() == "-" else raw.strip())

    checked = _checked(view, "welcome_toggles")
    for key in (
        "welcome_public",
        "welcome_dm_enabled",
        "welcome_dm_once",
        "welcome_ping_auto_add",
        "welcome_ping_button",
        "welcome_thread_reply",
        "usergroup_prompt",
        "usergroup_prompt_once",
    ):
        _set(key, "true" if key in checked else "false")

    raw_channel = values.get("welcome_override_channel", "")
    if raw_channel:
        channel_id = await _resolve_channel(client, raw_channel)
        if channel_id:
            overrides = (
                ("welcome_override_message", "welcome_message"),
                ("welcome_override_dm", "welcome_dm"),
            )
            for field, key in overrides:
                raw = values.get(field, "")
                if raw:
                    _set(
                        f"{key}:{channel_id}",
                        "" if raw.strip() == "-" else raw.strip(),
                    )
        else:
            warnings.append(f"couldn't resolve channel `{raw_channel}`")

    text = "saved welcome settings."
    if warnings:
        text += " " + "; ".join(warnings) + "."
    await _dm(client, user_id or "", text)


async def on_fallback_ai_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    values = _values(view)
    raw_base = values.get("ai_router_base_url", "")
    if raw_base:
        _set("ai_router_base_url", "" if raw_base.strip() == "-" else raw_base.strip())
    raw_models = values.get("ai_router_models", "")
    if raw_models:
        if raw_models.strip() == "-":
            _set("ai_router_models", "")
        else:
            from bot.ai.providers import parse_models

            models = parse_models(raw_models)
            if models:
                _set("ai_router_models", ",".join(models))
        _set("ai_router_model", "")
    api_key = values.get("ai_router_api_key", "")
    if api_key:
        _set("ai_router_api_key", "" if api_key.strip() == "-" else api_key.strip())
    await _dm(client, user_id or "", "saved fallback ai settings.")


async def on_watchers_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    values = _values(view)
    checked = _checked(view, "watchers")
    for key in ("watch_github_ci", "watch_github_notifications", "watch_email"):
        _set(key, "true" if key in checked else "false")
    raw_urls = values.get("watch_urls", "")
    if raw_urls:
        _set("watch_urls", "" if raw_urls.strip() == "-" else raw_urls.strip())
    for key in (
        "watch_github_ci_interval",
        "watch_github_notifications_interval",
        "watch_email_interval",
        "watch_url_interval",
    ):
        raw = values.get(key, "")
        if raw.isdigit():
            _set(key, str(max(60, int(raw))))
    await _dm(client, user_id or "", "saved watcher settings.")


async def on_storage_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    values = _values(view)
    for key in ("memory_retention_days", "sandbox_ttl_days"):
        raw = values.get(key, "")
        if raw.isdigit():
            _set(key, str(max(0, int(raw))))
    raw_free = values.get("storage_min_free_gb", "")
    try:
        if raw_free:
            _set("storage_min_free_gb", str(max(0.0, float(raw_free))))
    except ValueError:
        pass
    await _dm(client, user_id or "", "saved storage retention settings.")


async def on_channels_submit(ack, body, view, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    if not _is_admin(user_id):
        return
    raw = _values(view).get("auto_join_channels", "")
    if raw:
        _set("auto_join_channels", "" if raw.strip() == "-" else raw.strip())
    await _dm(client, user_id or "", "saved auto-join channels.")


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
        await _say(client, command, "usage: `/nugget setup disconnect google|mac|github|email|apis|slack`")
        return
    if integration == "all":
        targets = list(INTEGRATION_NAMES)
    elif integration in INTEGRATION_NAMES:
        targets = [integration]
    else:
        await _say(client, command, f"unknown integration `{integration}`.")
        return

    for name in targets:
        if name == "apis":
            for service in API_SERVICES:
                save_api_token(runtime.db, service, "")
        else:
            clear_integration(runtime.db, name)
        if name == "slack":
            deactivate_owner_posting()
    await _say(
        client,
        command,
        f":white_check_mark: cleared {', '.join(INTEGRATION_NAMES[n] for n in targets)}. "
        "environment variables may still provide defaults after a restart.",
    )

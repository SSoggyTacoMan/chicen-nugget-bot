"""Tier 2 access requests: modal -> admin approval -> private channel invite."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.runtime import runtime
from bot.slack_api import (
    error_code,
    invite_to_channel,
    is_channel_member,
    post,
    post_ephemeral,
    update,
)
from bot.utils.text import channel_mention, escape_mrkdwn
from bot.utils.timeparse import format_when
from bot.utils.tz import tzinfo

logger = logging.getLogger(__name__)

PANEL_KV = "access_panel:{}"


def register(app) -> None:
    app.action("access_request")(on_request_button)
    app.action("access_approve")(on_approve)
    app.action("access_deny")(on_deny)
    app.view("access_request_modal")(on_request_submit)
    app.view("access_deny_modal")(on_deny_submit)


def access_channel_id() -> str | None:
    if runtime.db:
        override = runtime.db.kv_get("config:access_channel_id")
        if override:
            return override
    return runtime.settings.access_channel_id


def access_enabled() -> bool:
    if runtime.db and runtime.db.kv_get("config:tier2") == "false":
        return False
    return bool(access_channel_id())


# --------------------------------------------------------------------- panel
async def ensure_panel(client, channel: str | None = None) -> str | None:
    settings = runtime.settings
    if not access_enabled():
        return None
    channel = channel or settings.access_panel_channel_id
    if not channel and settings.watched_channel_ids:
        watched = sorted(settings.watched_channel_ids - {"*"})
        channel = watched[0] if watched else None
    if not channel:
        return None

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": settings.access_panel_text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Request access"},
                    "style": "primary",
                    "action_id": "access_request",
                    "value": "panel",
                }
            ],
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"requests are reviewed by <@{settings.owner_user_id}>"}
            ],
        },
    ]
    text = "tier 2 access — hit the button to request it"
    key = PANEL_KV.format(channel)
    ts = runtime.db.kv_get(key)
    if ts:
        if await update(client, channel, ts, text, blocks=blocks):
            return ts
    result = await post(client, channel, text, blocks=blocks)
    if result:
        runtime.db.kv_set(key, result["ts"])
        return result["ts"]
    return None


# --------------------------------------------------------------------- modal
def _request_modal() -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "access_request_modal",
        "title": {"type": "plain_text", "text": "Tier 2 access"},
        "submit": {"type": "plain_text", "text": "Send request"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "tell me a little about why you want in. an admin will review it.",
                },
            },
            {
                "type": "input",
                "block_id": "reason",
                "optional": True,
                "label": {"type": "plain_text", "text": "Why do you want access?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "multiline": True,
                    "max_length": 500,
                    "placeholder": {"type": "plain_text", "text": "optional, but helps"},
                },
            },
            {
                "type": "input",
                "block_id": "agree",
                "label": {"type": "plain_text", "text": "House rules"},
                "element": {
                    "type": "checkboxes",
                    "action_id": "value",
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "I agree to be kind and follow the rules."},
                            "value": "yes",
                        }
                    ],
                },
            },
        ],
    }


async def _open_modal(client, trigger_id: str, user_id: str) -> None:
    if not access_enabled():
        await post(client, user_id, "tier 2 access isn't set up yet. ask an admin.")
        return
    try:
        await client.views_open(trigger_id=trigger_id, view=_request_modal())
    except Exception as exc:  # noqa: BLE001
        logger.warning("views_open failed: %s", error_code(exc))


async def on_request_button(ack, body, client) -> None:
    await ack()
    user_id = (body.get("user") or {}).get("id")
    trigger_id = body.get("trigger_id")
    if user_id and trigger_id:
        await _open_modal(client, trigger_id, user_id)


async def on_request_submit(ack, body, view, client) -> None:
    await ack()
    settings = runtime.settings
    user_id = (body.get("user") or {}).get("id")
    if not user_id:
        return
    if not access_enabled():
        await post(client, user_id, "tier 2 access isn't set up yet. ask an admin.")
        return

    values = (view.get("state") or {}).get("values") or {}
    reason = (((values.get("reason") or {}).get("value") or {}).get("value") or "").strip()
    agreed = bool(((values.get("agree") or {}).get("value") or {}).get("selected_options"))
    if not agreed:
        await post(client, user_id, "you have to agree to the house rules first.")
        return

    target_channel = access_channel_id()
    if target_channel:
        already = await is_channel_member(client, target_channel, user_id)
        if already:
            await post(
                client,
                user_id,
                f"you're already inside {channel_mention(target_channel)} — "
                "the door's already open for you. :door:",
            )
            return

    pending = runtime.db.pending_access_for_user(user_id)
    if pending:
        await post(client, user_id, f"you already have request *#{pending['id']}* pending. hang tight.")
        return

    last = runtime.db.last_access_for_user(user_id)
    if last:
        created = _parse_iso(last.get("created_at"))
        cooldown = timedelta(hours=settings.access_cooldown_hours)
        if created and datetime.now(timezone.utc) - created < cooldown:
            if last.get("status") == "approved":
                await post(
                    client,
                    user_id,
                    f"you were already approved for {channel_mention(target_channel or '')}. "
                    "if you're not in it, ask an admin to re-invite you.",
                )
            else:
                remaining = cooldown - (datetime.now(timezone.utc) - created)
                hours = max(1, int(remaining.total_seconds() // 3600))
                await post(client, user_id, f"you asked recently. try again in about {hours}h.")
            return

    request_id = runtime.db.create_access_request(user_id, reason)

    if settings.access_auto_approve and target_channel:
        ok, error = await invite_to_channel(client, target_channel, user_id)
        if ok:
            runtime.db.decide_access(request_id, "approved", settings.owner_user_id or "auto")
            await post(
                client,
                user_id,
                "the door swings open — you're in "
                f"{channel_mention(target_channel)}! :tada:",
            )
            await _log(
                f"tier 2 request *#{request_id}* from <@{user_id}> auto-approved: "
                f"{escape_mrkdwn(reason) or 'no reason given'}"
            )
            return
        logger.warning("auto-approve invite failed for %s: %s", user_id, error)
        await post(
            client,
            user_id,
            f"the door's stuck (`{error}`) — i sent request *#{request_id}* to an admin.",
        )
    else:
        await post(
            client,
            user_id,
            f"the door creaks… request *#{request_id}* is with the admins. "
            "i'll dm you when they decide.",
        )

    await _notify_admins(client, request_id, user_id, reason)
    await _log(
        f"tier 2 request *#{request_id}* from <@{user_id}>: "
        f"{escape_mrkdwn(reason) or 'no reason given'}"
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ------------------------------------------------------------------- admins
def _request_blocks(request: dict[str, Any]) -> list[dict[str, Any]]:
    reason = request.get("reason") or "no reason given"
    safe_reason = escape_mrkdwn(reason)
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Tier 2 access request #{request['id']}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*from:* <@{request['user_id']}>\n*reason:* {safe_reason}",
            },
        },
        {
            "type": "actions",
            "block_id": "access_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "access_approve",
                    "value": str(request["id"]),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": "access_deny",
                    "value": str(request["id"]),
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"requested {request.get('created_at', '')}"}
            ],
        },
    ]


async def _notify_admins(client, request_id: int, user_id: str, reason: str) -> None:
    settings = runtime.settings
    request = runtime.db.access_request(request_id) or {
        "id": request_id,
        "user_id": user_id,
        "reason": reason,
    }
    fallback = f"tier 2 access request #{request_id} from <@{user_id}>: {escape_mrkdwn(reason) or 'no reason given'}"
    blocks = _request_blocks(request)
    targets = [settings.admin_channel_id] if settings.admin_channel_id else sorted(settings.admin_user_ids)
    first: tuple[str, str] | None = None
    for target in targets:
        result = await post(client, target, fallback, blocks=blocks)
        if result and not first:
            first = (target, result["ts"])
    if first:
        runtime.db.set_access_admin_message(request_id, *first)


async def _decision_blocks(
    request: dict[str, Any], status: str, admin_id: str, note: str | None = None
) -> list[dict[str, Any]]:
    if status == "approved":
        label = ":white_check_mark: approved"
        detail = (
            f"the door swings open — approved by <@{admin_id}>, invited to "
            f"{channel_mention(access_channel_id() or '')}."
        )
    else:
        label = ":x: denied"
        detail = f"the door stays shut — denied by <@{admin_id}>."
        if note:
            detail += f"\nreason: {note}"
    reason = escape_mrkdwn((request.get("reason") or "no reason given").replace("\n", " "))
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*tier 2 request #{request['id']} — {label}*\n"
                f"from <@{request['user_id']}> · {reason}\n{detail}",
            },
        }
    ]


async def on_approve(ack, body, client) -> None:
    await ack()
    admin_id = (body.get("user") or {}).get("id")
    if not runtime.settings.is_admin(admin_id):
        await _ephemeral_to(body, client, "only admins can approve access requests.")
        return
    request_id = _action_value(body)
    request = runtime.db.access_request(request_id) if request_id else None
    if not request:
        await _ephemeral_to(body, client, "that request no longer exists.")
        return
    if request["status"] != "pending":
        await _update_from_body(client, body, await _decision_blocks(request, request["status"], admin_id))
        await _ephemeral_to(body, client, f"request #{request_id} was already {request['status']}.")
        return

    ok, error = await invite_to_channel(
        client, access_channel_id() or "", request["user_id"]
    )
    if not ok:
        hint = {
            "not_in_channel": "i'm not in the private channel — invite me to it, then try again.",
            "missing_scope": "i need the groups:write scope to invite people.",
            "channel_not_found": "ACCESS_CHANNEL_ID doesn't point at a channel i can see.",
        }.get(error or "", "check my scopes and channel membership.")
        await _ephemeral_to(body, client, f"couldn't invite <@{request['user_id']}>: `{error}`. {hint}")
        return

    runtime.db.decide_access(request_id, "approved", admin_id)
    refreshed = runtime.db.access_request(request_id) or request
    await _update_from_body(client, body, await _decision_blocks(refreshed, "approved", admin_id))
    await post(
        client,
        request["user_id"],
        "the door swings open — you're in! welcome to "
        f"{channel_mention(access_channel_id() or '')} :tada:",
    )
    await _log(f"tier 2 request *#{request_id}* approved by <@{admin_id}> for <@{request['user_id']}>.")


async def on_deny(ack, body, client) -> None:
    await ack()
    admin_id = (body.get("user") or {}).get("id")
    if not runtime.settings.is_admin(admin_id):
        await _ephemeral_to(body, client, "only admins can deny access requests.")
        return
    request_id = _action_value(body)
    request = runtime.db.access_request(request_id) if request_id else None
    if not request:
        await _ephemeral_to(body, client, "that request no longer exists.")
        return
    if request["status"] != "pending":
        await _update_from_body(client, body, await _decision_blocks(request, request["status"], admin_id))
        await _ephemeral_to(body, client, f"request #{request_id} was already {request['status']}.")
        return

    metadata = json.dumps(
        {
            "id": request_id,
            "channel": (body.get("container") or {}).get("channel_id")
            or (body.get("channel") or {}).get("id"),
            "ts": (body.get("container") or {}).get("message_ts")
            or (body.get("message") or {}).get("ts"),
            "admin": admin_id,
        }
    )
    view = {
        "type": "modal",
        "callback_id": "access_deny_modal",
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": "Deny request"},
        "submit": {"type": "plain_text", "text": "Deny"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "note",
                "optional": True,
                "label": {"type": "plain_text", "text": "Reason (sent to the requester)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "multiline": True,
                    "max_length": 400,
                },
            }
        ],
    }
    try:
        await client.views_open(trigger_id=body.get("trigger_id"), view=view)
    except Exception as exc:  # noqa: BLE001
        logger.warning("deny modal failed: %s", error_code(exc))
        await _ephemeral_to(body, client, "couldn't open the deny dialog; try again.")


async def on_deny_submit(ack, body, view, client) -> None:
    await ack()
    try:
        metadata = json.loads((view.get("private_metadata") or "{}"))
    except json.JSONDecodeError:
        return
    admin_id = metadata.get("admin") or (body.get("user") or {}).get("id")
    if not runtime.settings.is_admin(admin_id):
        return
    request_id = metadata.get("id")
    request = runtime.db.access_request(int(request_id)) if request_id else None
    if not request or request["status"] != "pending":
        return
    values = (view.get("state") or {}).get("values") or {}
    note = (((values.get("note") or {}).get("value") or {}).get("value") or "").strip()
    runtime.db.decide_access(request["id"], "denied", admin_id, note or None)
    refreshed = runtime.db.access_request(request["id"]) or request

    if metadata.get("channel") and metadata.get("ts"):
        await update(
            client,
            metadata["channel"],
            metadata["ts"],
            f"tier 2 request #{request['id']} denied",
            blocks=await _decision_blocks(refreshed, "denied", admin_id, note or None),
        )
    await post(
        client,
        request["user_id"],
        f"the door stays shut for now — your tier 2 request *#{request['id']}* was denied."
        + (f"\nreason: {note}" if note else ""),
    )
    await _log(f"tier 2 request *#{request['id']}* denied by <@{admin_id}> for <@{request['user_id']}>.")


# ------------------------------------------------------------------ commands
async def cmd_access(ack, command, client) -> None:
    await ack()
    trigger_id = command.get("trigger_id")
    user_id = command.get("user_id")
    if trigger_id and user_id:
        await _open_modal(client, trigger_id, user_id)


async def cmd_accesspanel(ack, command, client) -> None:
    await ack()
    user_id = command.get("user_id")
    channel_id = command.get("channel_id")
    if not runtime.settings.is_admin(user_id):
        await post_ephemeral(client, channel_id, user_id, "only admins can post the access panel.")
        return
    target = _first_channel_arg(command.get("text", ""))
    panel_channel = target or runtime.settings.access_panel_channel_id or channel_id
    ts = await ensure_panel(client, panel_channel)
    if ts:
        await post_ephemeral(client, channel_id, user_id, f"access panel updated in <#{panel_channel}>.")
    else:
        await post_ephemeral(
            client, channel_id, user_id, "couldn't post the panel. check ACCESS_CHANNEL_ID and my scopes."
        )


async def cmd_pending(ack, command, client) -> None:
    await ack()
    user_id = command.get("user_id")
    channel_id = command.get("channel_id")
    if not runtime.settings.is_admin(user_id):
        await post_ephemeral(client, channel_id, user_id, "only admins can see pending requests.")
        return
    rows = runtime.db.pending_access()
    if not rows:
        await post_ephemeral(client, channel_id, user_id, "no pending tier 2 requests.")
        return
    tz = tzinfo()
    lines = ["*pending tier 2 requests*"]
    for row in rows:
        created = _parse_iso(row.get("created_at"))
        when = format_when(created, tz) if created else "unknown"
        reason = escape_mrkdwn((row.get("reason") or "no reason given").replace("\n", " "))
        lines.append(f"• `#{row['id']}` <@{row['user_id']}> — {reason} _(requested {when})_")
    lines.append("\napprove/deny from the admin DM, or `/pending` again after acting.")
    await post_ephemeral(client, channel_id, user_id, "\n".join(lines))


# ------------------------------------------------------------------- helpers
def _action_value(body: dict[str, Any]) -> int | None:
    try:
        return int(body["actions"][0]["value"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


async def _update_from_body(client, body: dict[str, Any], blocks: list[dict[str, Any]]) -> None:
    channel = (body.get("container") or {}).get("channel_id") or (body.get("channel") or {}).get("id")
    ts = (body.get("container") or {}).get("message_ts") or (body.get("message") or {}).get("ts")
    if channel and ts:
        await update(client, channel, ts, "tier 2 access request update", blocks=blocks)


async def _ephemeral_to(body: dict[str, Any], client, text: str) -> None:
    channel = (body.get("container") or {}).get("channel_id") or (body.get("channel") or {}).get("id")
    user_id = (body.get("user") or {}).get("id")
    if channel and user_id:
        await post_ephemeral(client, channel, user_id, text)


def _first_channel_arg(text: str) -> str | None:
    import re

    match = re.search(r"<#([A-Z0-9]+)(?:\|[^>]+)?>", text or "")
    return match.group(1) if match else None


async def _log(text: str) -> None:
    channel = runtime.settings.logs_channel_id
    if not channel or runtime.client is None:
        return
    await post(runtime.client, channel, text)


__all__ = ["register", "ensure_panel"]

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bot.runtime import runtime
from bot.slack_api import (
    ensure_usergroup_member,
    ensure_usergroup_not_member,
    error_code,
    post,
    post_ephemeral,
    update,
    user_info,
    usergroup_members,
)
from bot.utils.text import display_name

logger = logging.getLogger(__name__)

PROMPT_KV = "usergroup_prompt:{}"
JOIN_ACTION = "usergroup_join"
LEAVE_ACTION = "usergroup_leave"
SKIP_ACTION = "usergroup_skip"
PANEL_JOIN_ACTION = "usergroup_panel_join"
PANEL_LEAVE_ACTION = "usergroup_panel_leave"


@dataclass(frozen=True)
class WelcomeConfig:
    public: bool
    message: str
    dm: str
    dm_once: bool
    ping: str
    watched: tuple[str, ...]
    usergroup_id: str | None
    prompt: bool
    prompt_text: str
    prompt_once: bool


def _kv(key: str) -> str | None:
    return runtime.db.kv_get(f"config:{key}") if runtime.db else None


def _kv_bool(key: str, default: bool) -> bool:
    raw = _kv(key)
    if raw is None:
        return default
    return str(raw).lower() in {"1", "true", "yes", "y", "on"}


def _kv_csv(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _kv(key)
    if raw is None:
        return default
    return tuple(item.strip() for item in str(raw).split(",") if item.strip())


def welcome_config() -> WelcomeConfig:
    settings = runtime.settings
    return WelcomeConfig(
        public=_kv_bool("welcome_public", settings.welcome_public),
        message=_kv("welcome_message") or settings.welcome_message,
        dm=_kv("welcome_dm") or settings.welcome_dm,
        dm_once=_kv_bool("welcome_dm_once", settings.welcome_dm_once),
        ping=_kv("welcome_ping") or settings.welcome_ping,
        watched=_kv_csv("watched_channel_ids", tuple(settings.watched_channel_ids)),
        usergroup_id=_kv("usergroup_id") or settings.usergroup_id,
        prompt=_kv_bool("usergroup_prompt", settings.usergroup_prompt),
        prompt_text=_kv("usergroup_prompt_text") or settings.usergroup_prompt_text,
        prompt_once=_kv_bool("usergroup_prompt_once", settings.usergroup_prompt_once),
    )


def message_for(channel: str | None, cfg: WelcomeConfig) -> str:
    if channel:
        override = _kv(f"welcome_message:{channel}")
        if override:
            return override
    return cfg.message


def dm_for(channel: str | None, cfg: WelcomeConfig) -> str:
    if channel:
        override = _kv(f"welcome_dm:{channel}")
        if override:
            return override
    return cfg.dm


def render(template: str, **values: str) -> str:
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", value)
    return result


def register(app) -> None:
    app.event("member_joined_channel")(on_member_joined)
    app.event("member_left_channel")(on_member_left)
    app.action(JOIN_ACTION)(on_usergroup_join)
    app.action(LEAVE_ACTION)(on_usergroup_leave)
    app.action(SKIP_ACTION)(on_usergroup_skip)
    app.action(PANEL_JOIN_ACTION)(on_panel_join)
    app.action(PANEL_LEAVE_ACTION)(on_panel_leave)


def _watches(channel_id: str | None, watched: tuple[str, ...]) -> bool:
    if not channel_id:
        return False
    return "*" in watched or channel_id in watched


async def on_member_joined(event: dict[str, Any], client) -> None:
    if runtime.db and runtime.db.kv_get("config:welcomer") == "false":
        return
    settings = runtime.settings
    cfg = welcome_config()
    channel = event.get("channel")
    user_id = event.get("user")
    if not user_id or not _watches(channel, cfg.watched):
        return
    if event.get("user_type") == "bot" or user_id == runtime.bot_user_id:
        return

    user = await user_info(client, user_id)
    name = display_name(user)
    runtime.db.record_join(user_id, name, channel)
    member = runtime.db.member(user_id) or {}
    join_count = member.get("joins", 1)

    group_note = ""
    should_prompt = _should_prompt(user_id, cfg)
    if cfg.usergroup_id and runtime.user_client is not None and not cfg.prompt:
        result = await ensure_usergroup_member(runtime.user_client, cfg.usergroup_id, user_id)
        if result == "added":
            group_note = " and added to the ping group"
        elif result == "failed":
            group_note = " (couldn't add to the ping group)"

    await _log(
        f"<@{user_id}> joined <#{channel}> "
        f"(join #{join_count}){group_note}.",
        client,
    )

    message = message_for(channel, cfg)
    if cfg.public and message.strip():
        text = render(
            message,
            user=f"<@{user_id}>",
            channel=f"<#{channel}>",
            bot=settings.bot_name,
            name=name,
        )
        if cfg.ping:
            text = f"{text}\n\n{cfg.ping}"
        await post(client, channel, text)

    await _send_welcome_dm(client, user_id, name, channel, should_prompt, cfg)


async def on_member_left(event: dict[str, Any], client) -> None:
    cfg = welcome_config()
    channel = event.get("channel")
    user_id = event.get("user")
    if not user_id or not _watches(channel, cfg.watched):
        return
    if event.get("user_type") == "bot" or user_id == runtime.bot_user_id:
        return

    runtime.db.record_leave(user_id, channel)

    group_note = ""
    if (
        cfg.usergroup_id
        and runtime.user_client is not None
        and not cfg.prompt
    ):
        result = await ensure_usergroup_not_member(runtime.user_client, cfg.usergroup_id, user_id)
        if result == "removed":
            group_note = " and removed from the ping group"
        elif result == "failed":
            group_note = " (couldn't remove from the ping group, check logs)"

    await _log(f"<@{user_id}> left <#{channel}>{group_note}.", client)


# --------------------------------------------------------------- welcome dm
def _should_prompt(user_id: str, cfg: WelcomeConfig) -> bool:
    if not cfg.usergroup_id or not cfg.prompt:
        return False
    if runtime.user_client is None:
        logger.info("usergroup prompt skipped: no user token with usergroups:write")
        return False
    if cfg.prompt_once and runtime.db.kv_get(PROMPT_KV.format(user_id)):
        return False
    return True


async def _send_welcome_dm(
    client,
    user_id: str,
    name: str,
    channel: str | None,
    prompt: bool,
    cfg: WelcomeConfig,
) -> None:
    settings = runtime.settings
    dm_template = dm_for(channel, cfg)
    want_welcome = bool(dm_template.strip()) and not (
        cfg.dm_once and runtime.db.was_welcomed(user_id)
    )
    if not want_welcome and not prompt:
        return

    values = dict(
        user=f"<@{user_id}>",
        channel=f"<#{channel}>" if channel else "this channel",
        bot=settings.bot_name,
        name=name,
    )
    parts: list[str] = []
    if want_welcome:
        parts.append(render(dm_template, **values))
    if prompt:
        parts.append(render(cfg.prompt_text, **values))
    text = "\n\n".join(part for part in parts if part.strip())
    if not text:
        return

    blocks: list[dict[str, Any]] = []
    if prompt and cfg.usergroup_id:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":bell: add me", "emoji": True},
                        "style": "primary",
                        "action_id": JOIN_ACTION,
                        "value": cfg.usergroup_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "no thanks"},
                        "action_id": SKIP_ACTION,
                        "value": cfg.usergroup_id,
                    },
                ],
            }
        )
    if want_welcome and settings.access_enabled:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Request tier 2 access"},
                        "action_id": "access_request",
                        "value": "welcome",
                    }
                ],
            }
        )

    result = await post(client, user_id, text, blocks=blocks or None)
    if not result:
        return
    if want_welcome:
        runtime.db.mark_welcomed(user_id)
    if prompt:
        runtime.db.kv_set(PROMPT_KV.format(user_id), "asked")


# ------------------------------------------------------------------ actions
async def on_usergroup_join(ack, body, action, client, respond) -> None:
    await ack()
    cfg = welcome_config()
    user_id = (body.get("user") or {}).get("id")
    group = (action.get("value") or cfg.usergroup_id or "").strip()
    if not user_id or not group:
        return
    if runtime.user_client is None:
        await _finish(
            client,
            body,
            respond,
            "i can't manage the ping group right now - i need a user token with `usergroups:write`.",
        )
        return
    result = await ensure_usergroup_member(runtime.user_client, group, user_id)
    runtime.db.kv_set(PROMPT_KV.format(user_id), "joined" if result in {"added", "already"} else "asked")
    await _finish(client, body, respond, join_text(result))


async def on_usergroup_leave(ack, body, action, client, respond) -> None:
    await ack()
    cfg = welcome_config()
    user_id = (body.get("user") or {}).get("id")
    group = (action.get("value") or cfg.usergroup_id or "").strip()
    if not user_id or not group:
        return
    if runtime.user_client is None:
        await _finish(
            client,
            body,
            respond,
            "i can't manage the ping group right now - i need a user token with `usergroups:write`.",
        )
        return
    result = await ensure_usergroup_not_member(runtime.user_client, group, user_id)
    await _finish(client, body, respond, leave_text(result))


async def on_usergroup_skip(ack, body, client, respond) -> None:
    await ack()
    await _finish(
        client,
        body,
        respond,
        "no worries - not added. you can join any time with `/nugget pinggroup`.",
    )


async def on_panel_join(ack, body, action, client, respond) -> None:
    await ack()
    cfg = welcome_config()
    user_id = (body.get("user") or {}).get("id")
    group = (action.get("value") or cfg.usergroup_id or "").strip()
    if not user_id or not group:
        return
    if runtime.user_client is None:
        await _finish(
            client,
            body,
            respond,
            "i can't manage the ping group right now - i need a user token with `usergroups:write`.",
            replace=False,
        )
        return
    result = await ensure_usergroup_member(runtime.user_client, group, user_id)
    runtime.db.kv_set(
        PROMPT_KV.format(user_id),
        "joined" if result in {"added", "already"} else "asked",
    )
    await _finish(client, body, respond, join_text(result), replace=False)


async def on_panel_leave(ack, body, action, client, respond) -> None:
    await ack()
    cfg = welcome_config()
    user_id = (body.get("user") or {}).get("id")
    group = (action.get("value") or cfg.usergroup_id or "").strip()
    if not user_id or not group:
        return
    if runtime.user_client is None:
        await _finish(
            client,
            body,
            respond,
            "i can't manage the ping group right now - i need a user token with `usergroups:write`.",
            replace=False,
        )
        return
    result = await ensure_usergroup_not_member(runtime.user_client, group, user_id)
    await _finish(client, body, respond, leave_text(result), replace=False)


async def _finish(
    client,
    body: dict[str, Any] | None,
    respond,
    text: str,
    *,
    replace: bool = True,
) -> None:
    channel = ((body or {}).get("channel") or {}).get("id")
    ts = ((body or {}).get("message") or {}).get("ts")
    if replace and client is not None and channel and ts:
        if await update(client, channel, ts, text):
            return
    if respond is not None:
        try:
            await respond(text=text, replace_original=replace)
        except Exception:  # noqa: BLE001 - best effort
            logger.info("could not replace the original message")


def join_text(result: str) -> str:
    if result == "added":
        return ":bell: you're in the ping group. use `/nugget pinggroup leave` if you want out."
    if result == "already":
        return ":bell: you're already in the ping group."
    return (
        "couldn't add you to the ping group: my user token may be missing the "
        "`usergroups:write` scope. an admin can add you manually."
    )


def leave_text(result: str) -> str:
    if result == "removed":
        return ":wave: removed you from the ping group."
    if result == "not_member":
        return "you weren't in the ping group anyway."
    return "couldn't remove you from the ping group: check my `usergroups:write` scope."


# ------------------------------------------------------------------ command
async def cmd_pinggroup(ack, command, client) -> None:
    await ack()
    settings = runtime.settings
    channel = command["channel_id"]
    user_id = command["user_id"]
    group = welcome_config().usergroup_id
    if not group:
        await post_ephemeral(client, channel, user_id, "no ping group is configured yet.")
        return
    if runtime.user_client is None:
        await post_ephemeral(
            client,
            channel,
            user_id,
            "i need a user token with `usergroups:write` to manage the ping group.",
        )
        return

    arg = (command.get("text") or "").strip().lower()
    if arg == "panel":
        if not runtime.settings.is_admin(user_id):
            await post_ephemeral(
                client, channel, user_id, "only admins can post the ping group panel."
            )
            return
        await post(
            client,
            channel,
            "*ping group* :bell:\nwant pings for events and the good stuff? join or leave below.",
            blocks=[
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": ":bell: join the ping group",
                                "emoji": True,
                            },
                            "style": "primary",
                            "action_id": PANEL_JOIN_ACTION,
                            "value": group,
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": ":wave: leave",
                                "emoji": True,
                            },
                            "action_id": PANEL_LEAVE_ACTION,
                            "value": group,
                        },
                    ],
                }
            ],
        )
        return
    if arg in {"join", "add", "in"}:
        result = await ensure_usergroup_member(runtime.user_client, group, user_id)
        await post_ephemeral(client, channel, user_id, join_text(result))
        return
    if arg in {"leave", "remove", "out"}:
        result = await ensure_usergroup_not_member(runtime.user_client, group, user_id)
        await post_ephemeral(client, channel, user_id, leave_text(result))
        return

    members = await usergroup_members(runtime.user_client, group)
    if members is None:
        await post_ephemeral(
            client,
            channel,
            user_id,
            "couldn't check the ping group: my user token may be missing `usergroups:read`.",
        )
        return
    if user_id in members:
        text = ":bell: you're in the ping group."
        blocks = [
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":wave: leave the ping group", "emoji": True},
                        "action_id": LEAVE_ACTION,
                        "value": group,
                    }
                ],
            }
        ]
    else:
        text = "you're not in the ping group yet."
        blocks = [
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":bell: add me", "emoji": True},
                        "style": "primary",
                        "action_id": JOIN_ACTION,
                        "value": group,
                    }
                ],
            }
        ]
    await post_ephemeral(client, channel, user_id, text, blocks=blocks)


async def _log(text: str, client) -> None:
    channel = runtime.settings.logs_channel_id
    if not channel:
        return
    try:
        await post(client, channel, text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to log to %s: %s", channel, error_code(exc))

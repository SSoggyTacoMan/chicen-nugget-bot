"""Simple button-based polls with live tallies."""

from __future__ import annotations

import logging
import re
import secrets
from typing import Any

from bot.runtime import runtime
from bot.slack_api import post, post_ephemeral, update
from bot.utils.text import escape_mrkdwn

logger = logging.getLogger(__name__)

MAX_OPTIONS = 5


def register(app) -> None:
    app.action("poll_vote")(on_vote)
    app.action("poll_unvote")(on_unvote)


def _new_id() -> str:
    for _ in range(10):
        candidate = secrets.token_hex(3)
        if not runtime.db.poll(candidate):
            return candidate
    return secrets.token_hex(5)


def _parse_poll(text: str) -> tuple[str, list[str]] | None:
    quoted = [item.strip() for item in re.findall(r'"([^"]+)"', text or "") if item.strip()]
    if len(quoted) >= 3:
        question, options = quoted[0], quoted[1:]
    else:
        parts = [item.strip() for item in re.split(r"\s*\|\s*|\n+", text or "") if item.strip()]
        if len(parts) < 3:
            return None
        question, options = parts[0], parts[1:]
    options = options[:MAX_OPTIONS]
    if len(options) < 2:
        return None
    return question[:200], [option[:120] for option in options]


def _bar(count: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "░" * width
    filled = round(width * count / total)
    return "█" * filled + "░" * (width - filled)


def _blocks(poll: dict[str, Any], counts: list[int], total: int) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{poll['question']}*"},
        }
    ]
    lines = []
    for idx, option in enumerate(poll["options"]):
        count = counts[idx] if idx < len(counts) else 0
        pct = round(100 * count / total) if total else 0
        lines.append(f"`{_bar(count, total)}` {option}: *{count}* ({pct}%)")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    if not poll.get("closed"):
        buttons = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": f"{idx + 1}. {option[:55]}"},
                "action_id": "poll_vote",
                "value": f"{poll['id']}:{idx}",
            }
            for idx, option in enumerate(poll["options"])
        ]
        for start in range(0, len(buttons), 5):
            blocks.append({"type": "actions", "elements": buttons[start : start + 5]})
        if total:
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Clear my vote"},
                            "action_id": "poll_unvote",
                            "value": poll["id"],
                        }
                    ],
                }
            )

    footer = f"{total} vote{'s' if total != 1 else ''}"
    if poll.get("closed"):
        footer += " · poll closed"
    else:
        footer += " · click an option to vote, click another to change it"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


async def _refresh(client, poll_id: str) -> None:
    poll = runtime.db.poll(poll_id)
    if not poll or not poll.get("message_ts"):
        return
    counts, total = runtime.db.poll_counts(poll_id)
    await update(
        client,
        poll["channel_id"],
        poll["message_ts"],
        f"poll: {poll['question']}",
        blocks=_blocks(poll, counts, total),
    )


async def cmd_poll(ack, command, client) -> None:
    await ack()
    user_id = command.get("user_id")
    channel_id = command.get("channel_id")
    parsed = _parse_poll(command.get("text", ""))
    if not parsed:
        await post_ephemeral(
            client,
            channel_id,
            user_id,
            'usage: `/poll "question" "option one" "option two"` (2-5 options). '
            'you can also separate options with `|`.',
        )
        return
    question, options = parsed
    safe_question = escape_mrkdwn(question)
    safe_options = [escape_mrkdwn(opt) for opt in options]
    poll_id = _new_id()
    runtime.db.create_poll(poll_id, channel_id, safe_question, safe_options, user_id)
    poll = runtime.db.poll(poll_id)
    result = await post(
        client,
        channel_id,
        f"poll: {safe_question}",
        blocks=_blocks(poll, [0] * len(options), 0),
    )
    if result:
        runtime.db.set_poll_message(poll_id, result["ts"])


async def on_vote(ack, body, client) -> None:
    await ack()
    action = (body.get("actions") or [{}])[0]
    user_id = (body.get("user") or {}).get("id")
    if not user_id:
        return
    try:
        poll_id, index_s = (action.get("value") or "").split(":", 1)
        index = int(index_s)
    except (ValueError, AttributeError):
        return
    poll = runtime.db.poll(poll_id)
    if not poll:
        return
    if poll.get("closed"):
        await _ephemeral(body, client, "this poll is closed.")
        return
    if not (0 <= index < len(poll["options"])):
        return
    runtime.db.vote_poll(poll_id, user_id, index)
    await _refresh(client, poll_id)


async def on_unvote(ack, body, client) -> None:
    await ack()
    action = (body.get("actions") or [{}])[0]
    user_id = (body.get("user") or {}).get("id")
    poll_id = action.get("value")
    if not user_id or not poll_id:
        return
    poll = runtime.db.poll(poll_id)
    if not poll or poll.get("closed"):
        return
    runtime.db.unvote_poll(poll_id, user_id)
    await _refresh(client, poll_id)


async def cmd_pollclose(ack, command, client) -> None:
    await ack()
    user_id = command.get("user_id")
    channel_id = command.get("channel_id")
    poll_id = (command.get("text") or "").strip().lstrip("#")
    if not poll_id:
        await post_ephemeral(client, channel_id, user_id, "usage: `/pollclose <poll-id>`")
        return
    poll = runtime.db.poll(poll_id)
    if not poll:
        await post_ephemeral(client, channel_id, user_id, f"no poll `{poll_id}`.")
        return
    if poll["creator"] != user_id and not runtime.settings.is_admin(user_id):
        await post_ephemeral(client, channel_id, user_id, "only the poll creator or an admin can close it.")
        return
    runtime.db.close_poll(poll_id)
    await _refresh(client, poll_id)
    await post_ephemeral(client, channel_id, user_id, f"closed poll `{poll_id}`.")


async def _ephemeral(body: dict[str, Any], client, text: str) -> None:
    channel = (body.get("container") or {}).get("channel_id") or (body.get("channel") or {}).get("id")
    user_id = (body.get("user") or {}).get("id")
    if channel and user_id:
        await post_ephemeral(client, channel, user_id, text)

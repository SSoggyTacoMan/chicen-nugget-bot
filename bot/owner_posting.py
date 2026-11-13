"""Optional native owner posting via the owner's own Slack user token.

Posting "in the owner's name" works by default without this: the bot app shows
the owner's display name and avatar on a bot message via `chat:write.customize`.
A token validated here upgrades that to a fully native post from the owner's own
account, with no app badge. It must be an `xoxp-` token (or `xoxc-` plus the `d`
cookie) that belongs to ``OWNER_USER_ID``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from slack_sdk.web.async_client import AsyncWebClient

from bot.config import Settings
from bot.db import Database
from bot.integrations import owner_token_config
from bot.runtime import runtime

logger = logging.getLogger(__name__)


@dataclass
class OwnerPostingResult:
    client: AsyncWebClient | None = None
    user_id: str = ""
    error: str = ""


def save_owner_credentials(db: Database | None, *, token: str, cookie: str = "") -> None:
    if not db:
        return
    token = token.strip()
    cookie = cookie.strip()
    if token:
        db.kv_set("config:slack_owner_token", token)
    else:
        db.kv_del("config:slack_owner_token")
    if cookie:
        db.kv_set("config:slack_owner_cookie", cookie)
    else:
        db.kv_del("config:slack_owner_cookie")


def deactivate_owner_posting() -> None:
    runtime.owner_client = None
    runtime.owner_user_id = ""


async def activate_owner_posting(
    db: Database | None, settings: Settings, *, quiet: bool = False
) -> OwnerPostingResult:
    """Build and validate an owner client, updating shared runtime state.

    Fails closed: an invalid token never falls back to Boi or the bot app.
    """
    creds = owner_token_config(db, settings)
    result = await validate_owner_token(
        token=creds["token"], cookie=creds["cookie"], settings=settings, quiet=quiet
    )
    if result.client:
        runtime.owner_client = result.client
        runtime.owner_user_id = result.user_id
    else:
        deactivate_owner_posting()
    return result


async def validate_owner_token(
    *, token: str, cookie: str, settings: Settings, quiet: bool = False
) -> OwnerPostingResult:
    """Validate credentials without changing runtime or storage."""
    token = (token or "").strip()
    cookie = (cookie or "").strip()

    if not token:
        return OwnerPostingResult(error="no owner token is configured")

    if token.startswith("xoxb-"):
        return OwnerPostingResult(
            error="that is a bot token (xoxb-). owner posting needs the owner's own user token."
        )
    if token.startswith("xoxc-") and not cookie:
        return OwnerPostingResult(
            error="an xoxc- token needs the matching Slack `d` cookie too."
        )

    headers = {"Cookie": f"d={cookie}"} if cookie else {}
    client = AsyncWebClient(token=token, headers=headers)
    try:
        auth = await client.auth_test()
    except Exception as exc:  # noqa: BLE001
        return OwnerPostingResult(error=f"Slack rejected the owner token: {exc}")

    user_id = str(auth.get("user_id") or "")
    if not user_id or user_id != settings.owner_user_id:
        return OwnerPostingResult(
            error=(
                f"that token belongs to <@{user_id or 'unknown'}>, not the configured "
                f"owner <@{settings.owner_user_id}>."
            )
        )

    if not quiet:
        logger.info(
            "owner posting token active as %s (%s)", auth.get("user"), user_id
        )
    return OwnerPostingResult(client=client, user_id=user_id)

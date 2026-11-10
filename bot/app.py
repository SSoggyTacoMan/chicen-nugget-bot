from __future__ import annotations

import asyncio
import logging
import time

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from bot.runtime import runtime
from bot.ai.agent import Agent
from bot.ai.gate import Gate
from bot.ai.limits import RateLimiter
from bot.config import settings
from bot.db import Database
from bot.features import access, scheduler
from bot.handlers import register
from bot.logging_setup import setup_logging
from bot.slack_api import ensure_public_member

logger = logging.getLogger(__name__)


async def run() -> None:
    setup_logging()
    problems = settings.missing_required()
    if problems:
        raise SystemExit(
            "cannot start:\n  - " + "\n  - ".join(problems) + "\n\ncopy .env.example to .env and fill it in."
        )

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    db.init()
    runtime.db = db
    runtime.settings = settings
    runtime.started_at = time.time()

    app = AsyncApp(token=settings.slack_bot_token)
    runtime.app = app
    runtime.client = app.client

    try:
        auth = await app.client.auth_test()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"slack auth failed: {exc}\ncheck SLACK_BOT_TOKEN.") from exc

    runtime.bot_user_id = auth.get("user_id") or ""
    runtime.bot_id = auth.get("bot_id") or ""

    user_client = None
    if settings.slack_user_token:
        user_client = AsyncWebClient(token=settings.slack_user_token)
        try:
            user_auth = await user_client.auth_test()
            logger.info("user token active as %s", user_auth.get("user_id"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("SLACK_USER_TOKEN is set but invalid: %s", exc)
            user_client = None
    runtime.user_client = user_client

    if settings.ai_enabled:
        runtime.ai_limiter = RateLimiter(settings.ai_rate_limit_per_minute)
        try:
            runtime.agent = Agent(
                settings,
                db,
                client=app.client,
                user_client=user_client,
                bot_user_id=runtime.bot_user_id,
            )
            logger.info(
                "ai agent enabled with model %s%s",
                settings.ai_model,
                f" (fallback {settings.ai_fallback_model})" if settings.ai_fallback_model else "",
            )
            if settings.ai_gate_enabled:
                runtime.gate = Gate(settings)
                channels = (
                    "everywhere"
                    if "*" in settings.ai_ambient_channels
                    else ", ".join(sorted(settings.ai_ambient_channels)) or "nowhere"
                )
                logger.info(
                    "gate enabled with %s; ambient replies in %s (cooldown %ss)",
                    settings.ai_gate_model,
                    channels,
                    settings.ai_ambient_cooldown,
                )
        except Exception:  # noqa: BLE001
            logger.exception("failed to initialise the ai agent; continuing without it")
            runtime.agent = None

    register(app)

    if settings.access_enabled:
        try:
            ts = await access.ensure_panel(app.client)
            if ts:
                logger.info("access panel ready")
            else:
                logger.warning(
                    "access panel could not be posted; set ACCESS_PANEL_CHANNEL_ID "
                    "and invite me to that channel"
                )
        except Exception:  # noqa: BLE001
            logger.exception("failed to prepare the access panel")

    if settings.recap_channel_id:
        try:
            status = await ensure_public_member(app.client, settings.recap_channel_id)
            if status == "joined":
                logger.info("joined recap channel %s", settings.recap_channel_id)
            elif status == "member":
                logger.info("already in recap channel %s", settings.recap_channel_id)
            elif status == "missing_scope":
                logger.warning(
                    "cannot join recap channel %s: missing channels:join scope "
                    "(reinstall the app from the manifest, or invite me manually)",
                    settings.recap_channel_id,
                )
            elif status == "private":
                logger.warning(
                    "recap channel %s is private or archived; invite me manually",
                    settings.recap_channel_id,
                )
            else:
                logger.warning("could not join recap channel %s", settings.recap_channel_id)
        except Exception:  # noqa: BLE001
            logger.exception("failed to prepare the recap channel")

    scheduler.start()

    logger.info(
        "%s online as bot user %s (%s) | ai=%s welcomer=%s access=%s user_token=%s",
        settings.bot_name,
        runtime.bot_user_id,
        auth.get("team"),
        settings.ai_enabled,
        settings.welcomer_enabled,
        settings.access_enabled,
        bool(user_client),
    )
    logger.info("starting socket mode...")

    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    try:
        await handler.start_async()
    finally:
        await scheduler.stop()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(run())

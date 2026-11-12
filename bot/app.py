from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import time

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from bot.runtime import runtime
from bot.ai.agent import Agent
from bot.ai.budget import Budget
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
        headers = {}
        if settings.slack_user_cookie:
            headers["Cookie"] = f"d={settings.slack_user_cookie}"
        user_client = AsyncWebClient(token=settings.slack_user_token, headers=headers)
        try:
            user_auth = await user_client.auth_test()
            runtime.user_id = user_auth.get("user_id") or ""
            logger.info("user token active as %s (%s)", user_auth.get("user"), runtime.user_id)
            if settings.slack_user_cookie:
                from bot.user_gateway import UserGateway
                runtime.user_gateway = UserGateway(
                    token=settings.slack_user_token,
                    cookie=settings.slack_user_cookie,
                    user_id=runtime.user_id,
                    user_client=user_client,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("SLACK_USER_TOKEN is set but invalid: %s", exc)
            user_client = None
    runtime.user_client = user_client

    owner_client = None
    if settings.slack_owner_token:
        owner_headers = {}
        if settings.slack_owner_cookie:
            owner_headers["Cookie"] = f"d={settings.slack_owner_cookie}"
        owner_client = AsyncWebClient(token=settings.slack_owner_token, headers=owner_headers)
        try:
            owner_auth = await owner_client.auth_test()
            runtime.owner_user_id = owner_auth.get("user_id") or ""
            if runtime.owner_user_id and runtime.owner_user_id != settings.owner_user_id:
                logger.warning(
                    "SLACK_OWNER_TOKEN belongs to %s (%s), not the configured owner (%s); "
                    "owner posting disabled",
                    owner_auth.get("user"),
                    runtime.owner_user_id,
                    settings.owner_user_id,
                )
                owner_client = None
            else:
                logger.info(
                    "owner posting token active as %s (%s)",
                    owner_auth.get("user"),
                    runtime.owner_user_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("SLACK_OWNER_TOKEN is set but invalid: %s", exc)
            owner_client = None
    runtime.owner_client = owner_client

    if settings.ai_enabled:
        runtime.budget = Budget(settings, db)
        runtime.ai_limiter = RateLimiter(settings.ai_rate_limit_per_minute)
        runtime.ai_global_limiter = RateLimiter(settings.ai_global_rate_limit_per_minute)
        try:
            runtime.agent = Agent(
                settings,
                db,
                client=app.client,
                user_client=user_client,
                bot_user_id=runtime.bot_user_id,
                user_id=runtime.user_id,
                budget=runtime.budget,
            )
            logger.info(
                "ai agent enabled with bot model %s%s; Boi model %s%s",
                settings.ai_model,
                f" (fallback {settings.ai_fallback_model})" if settings.ai_fallback_model else "",
                settings.ai_assistant_model,
                f" (fallback {settings.ai_assistant_fallback_model})"
                if settings.ai_assistant_fallback_model
                else "",
            )
            status = runtime.budget.status()
            global_stop = (
                f"${settings.ai_daily_budget_usd:.2f}/day"
                if settings.ai_daily_budget_usd > 0
                else "off (account limit applies)"
            )
            logger.info(
                "ai usage: $%.4f total, %s calls today; per-user cap %s/day; bot-wide stop %s; "
                "limits %s/min per user, %s/min total",
                status.spent,
                status.calls,
                f"${settings.ai_user_daily_budget_usd:.2f}",
                global_stop,
                settings.ai_rate_limit_per_minute or "off",
                settings.ai_global_rate_limit_per_minute or "off",
            )
            if settings.ai_gate_enabled:
                runtime.gate = Gate(settings, budget=runtime.budget)
                channels = (
                    "everywhere"
                    if "*" in settings.ai_ambient_channels
                    else ", ".join(sorted(settings.ai_ambient_channels)) or "nowhere"
                )
                logger.info(
                    "gate enabled with %s at %s; ambient replies in %s (cooldown %ss)",
                    settings.ai_gate_model,
                    runtime.gate.jev.url,
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
        "%s online as bot user %s (%s) | ai=%s welcomer=%s access=%s user_token=%s owner_posting=%s",
        settings.bot_name,
        runtime.bot_user_id,
        auth.get("team"),
        settings.ai_enabled,
        settings.welcomer_enabled,
        settings.access_enabled,
        bool(user_client),
        bool(owner_client),
    )
    logger.info("starting socket mode...")

    gateway_task = None
    if runtime.user_gateway:
        gateway_task = asyncio.create_task(runtime.user_gateway.start())

    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    try:
        await handler.start_async()
    finally:
        if runtime.user_gateway:
            await runtime.user_gateway.stop()
        if gateway_task:
            gateway_task.cancel()
            with suppress(asyncio.CancelledError):
                await gateway_task
        await scheduler.stop()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(run())

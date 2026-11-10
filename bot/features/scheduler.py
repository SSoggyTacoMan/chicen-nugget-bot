"""Background loop for reminders and the optional daily admin digest."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from bot.runtime import runtime
from bot.features import recaps
from bot.features.stats import build_digest
from bot.reminders import create_reminder, dm_target
from bot.slack_api import post
from bot.utils.timeparse import next_occurrence, parse_when
from bot.utils.tz import tzinfo

logger = logging.getLogger(__name__)

TICK_SECONDS = 15
_task: asyncio.Task | None = None


def start() -> asyncio.Task:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="chicen-nugget-scheduler")
    return _task


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


async def _loop() -> None:
    _ensure_digest_job()
    _ensure_recap_jobs()
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)


def _ensure_digest_job() -> None:
    settings = runtime.settings
    if not settings.daily_digest_time or not settings.owner_user_id:
        return
    existing = runtime.db.find_recurring("digest", settings.owner_user_id)
    if existing:
        return
    parsed = parse_when(f"daily {settings.daily_digest_time} daily digest", tz=tzinfo())
    if not parsed:
        logger.warning("could not parse DAILY_DIGEST_TIME=%s", settings.daily_digest_time)
        return
    reminder = create_reminder(
        runtime.db,
        settings,
        user_id=settings.owner_user_id,
        target=dm_target(settings.owner_user_id),
        parsed=parsed,
        text="daily digest",
        tz=tzinfo(),
        kind="digest",
    )
    if reminder:
        logger.info("daily digest scheduled for %s", settings.daily_digest_time)


def _ensure_recap_jobs() -> None:
    settings = runtime.settings
    if not settings.recaps_enabled or not settings.owner_user_id:
        return
    tz = tzinfo()

    if not runtime.db.find_recurring("recap_public", settings.owner_user_id):
        parsed = parse_when(f"daily {settings.recap_time} daily recap", tz=tz)
        if parsed:
            reminder = create_reminder(
                runtime.db,
                settings,
                user_id=settings.owner_user_id,
                target=settings.recap_channel_id or "",
                parsed=parsed,
                text="daily recap",
                tz=tz,
                kind="recap_public",
            )
            if reminder:
                logger.info("daily recap scheduled for %s", settings.recap_time)
        else:
            logger.warning("could not parse RECAP_TIME=%s", settings.recap_time)

    if not settings.recap_reminder_minutes:
        return
    if runtime.db.find_recurring("recap_reminder", settings.owner_user_id):
        return
    reminder_time = _shift_hhmm(settings.recap_time or "00:00", -settings.recap_reminder_minutes)
    parsed = parse_when(f"daily {reminder_time} recap reminder", tz=tz)
    if not parsed:
        logger.warning("could not parse recap reminder time %s", reminder_time)
        return
    reminder = create_reminder(
        runtime.db,
        settings,
        user_id=settings.owner_user_id,
        target=dm_target(settings.owner_user_id),
        parsed=parsed,
        text=(
            f"your daily recap is in {settings.recap_reminder_minutes} minutes — "
            "get ready to send it!"
        ),
        tz=tz,
        kind="recap_reminder",
    )
    if reminder:
        logger.info("daily recap reminder scheduled for %s", reminder_time)


def _shift_hhmm(value: str, delta_minutes: int) -> str:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except (TypeError, ValueError):
        return "00:00"
    total = (hour * 60 + minute + delta_minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


async def _tick() -> None:
    now = datetime.now(tzinfo())
    for reminder in runtime.db.due_reminders(now):
        recurrence = reminder.get("recurrence")
        following = next_occurrence(recurrence, now, tzinfo()) if recurrence else None
        # advance first, so a crash mid-send never double-fires the same reminder
        runtime.db.advance_reminder(reminder["id"], following)
        try:
            await _deliver(reminder)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reminder %s failed: %s", reminder["id"], exc)
            if not recurrence:
                runtime.db.finish_reminder(reminder["id"], str(exc)[:500])


async def _deliver(reminder: dict) -> None:
    client = runtime.client
    if client is None:
        return
    if reminder.get("kind") == "recap_public":
        await recaps.post_public_prompt(client)
        logger.info("delivered daily recap prompt")
        return
    if reminder.get("kind") == "digest":
        text = build_digest(runtime.db, runtime.settings, tzinfo())
    else:
        text = reminder["text"]

    target = reminder["target"]
    if target.startswith("dm:"):
        await post(client, target[3:], text)
    else:
        await post(client, target, text)
    logger.info("delivered reminder %s to %s", reminder["id"], target)

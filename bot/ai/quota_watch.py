"""Watch a known primary AI outage and ping the owner when it clears."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from bot.ai.quota import (
    KIND_DAILY,
    back_notified_key,
    classify_quota_error,
    clear_cooldown,
    mark_unavailable,
    outage,
)
from bot.runtime import runtime

logger = logging.getLogger(__name__)

CHECK_SECONDS = 5 * 60
_task: asyncio.Task | None = None


def start() -> asyncio.Task:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="chicen-nugget-quota-watch")
    return _task


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        with suppress(asyncio.CancelledError):
            await _task
    _task = None


async def _loop() -> None:
    while True:
        try:
            await tick(
                settings=runtime.settings,
                db=runtime.db,
                agent=runtime.agent,
                client=runtime.client,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("quota watch tick failed")
        await asyncio.sleep(CHECK_SECONDS)


async def tick(
    *,
    settings: Any,
    db: Any,
    agent: Any,
    client: Any,
) -> bool:
    """Probe the primary provider once if an outage is known.

    A successful probe clears the cooldown so the next normal request uses the
    primary again, and DMs the owner once per UTC day. A failed probe refreshes
    the outage/cooldown state and keeps waiting.
    """

    if db is None or agent is None:
        return False
    kind = outage(db)
    if not kind or kind == KIND_DAILY:
        return False

    try:
        models = agent._models_for(False)
    except Exception:  # noqa: BLE001 - the probe is best-effort
        models = [getattr(settings, "ai_model", "")]
    model = models[0] if models else getattr(settings, "ai_model", "")
    if not model:
        return False

    try:
        await agent.llm.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001 - provider errors are expected
        error_kind = classify_quota_error(exc)
        if error_kind == KIND_DAILY:
            mark_unavailable(db, KIND_DAILY, str(exc))
        elif error_kind:
            mark_unavailable(db, error_kind, str(exc))
        logger.info(
            "quota watch: primary still unavailable (%s)",
            error_kind or type(exc).__name__,
        )
        return False

    # The primary answered. Let the next request use it immediately.
    clear_cooldown(db)
    return await notify_back(settings=settings, db=db, client=client)


async def notify_back(*, settings: Any, db: Any, client: Any) -> bool:
    """Send the one-per-day 'hacai is back' DM."""

    if db is None:
        return False
    key = back_notified_key()
    if db.kv_get(key):
        return False

    owner = getattr(settings, "owner_user_id", "")
    if not owner or client is None:
        db.kv_set(key, "1")
        return False
    try:
        await client.chat_postMessage(
            channel=owner,
            text=(
                "hack club ai is back. i'm off the fallback and replies have "
                "tools again. voice, images and memory search work too."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not send hacai back notice: %s", exc)
        return False
    db.kv_set(key, "1")
    return True

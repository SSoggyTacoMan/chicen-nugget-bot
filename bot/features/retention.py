"""Storage retention: old history, stale sandboxes, old backups."""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

from bot.ai.sandbox import SandboxManager
from bot.config import Settings
from bot.db import Database
from bot.utils.tz import tzinfo

logger = logging.getLogger(__name__)

_last_day = ""


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _manager(settings: Settings) -> SandboxManager:
    return SandboxManager(
        settings.sandbox_dir,
        max_workspaces=settings.sandbox_max_workspaces,
        exec_timeout=settings.sandbox_exec_timeout,
        max_exec_timeout=settings.sandbox_max_exec_timeout,
    )


def prune_now(db: Database, settings: Settings) -> dict[str, object]:
    counts = db.prune_history(settings.memory_retention_days)
    removed = _manager(settings).prune_stale(settings.sandbox_ttl_days)
    backups = 0
    cutoff = time.time() - 7 * 86400
    for path in settings.data_dir.glob("bot.db.bak.*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                backups += 1
        except OSError:
            continue
    if counts["messages"] or counts["vectors"]:
        db.vacuum()
    return {
        "messages": counts["messages"],
        "vectors": counts["vectors"],
        "usage_rows": counts["usage"],
        "sandboxes": len(removed),
        "backups": backups,
    }


def storage_report(settings: Settings) -> str:
    db_path = settings.db_path
    db_size = db_path.stat().st_size if db_path.exists() else 0
    manager = _manager(settings)
    sandboxes = manager.list()
    sandbox_size = _dir_size(settings.sandbox_dir)
    browser_size = _dir_size(settings.browser_dir)
    logs_size = _dir_size(settings.log_dir)
    data_size = _dir_size(settings.data_dir)
    try:
        free = shutil.disk_usage(settings.data_dir).free
    except OSError:
        free = 0
    return "\n".join(
        [
            "*storage*",
            f"• data: {_human(data_size)}",
            f"• bot.db: {_human(db_size)}",
            f"• sandboxes: {len(sandboxes)} · {_human(sandbox_size)}",
            f"• browser: {_human(browser_size)}",
            f"• logs: {_human(logs_size)}",
            f"• free disk: {_human(free)}",
            f"• retention: history {settings.memory_retention_days}d · sandboxes {settings.sandbox_ttl_days}d",
        ]
    )


async def tick() -> None:
    global _last_day
    settings = runtime_settings()
    if runtime_db() is None or settings is None:
        return
    today = datetime.now(tzinfo()).date().isoformat()
    if today == _last_day:
        return
    try:
        result = prune_now(runtime_db(), settings)
    except Exception:
        logger.exception("storage prune failed")
        return
    _last_day = today
    if any(result.values()):
        logger.info("storage prune: %s", result)
    try:
        free = shutil.disk_usage(settings.data_dir).free
    except OSError:
        return
    if free < settings.storage_min_free_gb * 1024**3:
        logger.warning("low disk: %s free", _human(free))


def runtime_db() -> Database | None:
    from bot.runtime import runtime

    return runtime.db


def runtime_settings() -> Settings | None:
    from bot.runtime import runtime

    return runtime.settings

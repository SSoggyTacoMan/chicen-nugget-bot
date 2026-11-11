from __future__ import annotations

import os
import platform
import resource
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bot.config import BASE_DIR, Settings
from bot.db import Database


def collect_stats(db: Database) -> dict[str, int]:
    return db.stats()


def format_stats(stats: dict[str, int]) -> str:
    lines = [
        "*workspace stats*",
        f"• members tracked: *{stats['members']}*",
        f"• joins: *{stats['joins_24h']}* in 24h · *{stats['joins_7d']}* in 7d",
        f"• leaves: *{stats['leaves_24h']}* in 24h",
        f"• tier 2 requests: *{stats['pending_access']}* pending · "
        f"*{stats['approved_access']}* approved · *{stats['denied_access']}* denied",
        f"• open polls: *{stats['open_polls']}* · active reminders: *{stats['reminders']}*",
    ]
    if "tasks" in stats:
        lines.append(f"• pending tasks: *{stats['tasks']}*")
    lines.append(f"• ai memory: *{stats['messages']}* turns · *{stats['notes']}* notes")
    return "\n".join(lines)


def format_uptime(started_at: float) -> str:
    if not started_at:
        return "just now"
    seconds = max(0, int(time.time() - started_at))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def get_git_commit(base_dir: Path = BASE_DIR) -> str:
    git_dir = base_dir / ".git"
    if git_dir.exists():
        try:
            head_file = git_dir / "HEAD"
            if head_file.exists():
                head_content = head_file.read_text().strip()
                if head_content.startswith("ref:"):
                    ref_name = head_content.split(":", 1)[1].strip()
                    ref_path = git_dir / ref_name
                    if ref_path.exists():
                        return ref_path.read_text().strip()[:7]
                    packed = git_dir / "packed-refs"
                    if packed.exists():
                        for line in packed.read_text().splitlines():
                            if line.endswith(ref_name):
                                return line.split()[0][:7]
                elif head_content:
                    return head_content[:7]
        except Exception:
            pass
    return os.getenv("GIT_COMMIT") or os.getenv("GIT_SHA") or "unknown"


def get_process_rss_mb() -> float:
    try:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return (rss / 1024 / 1024) if sys.platform == "darwin" else (rss / 1024)
    except Exception:
        return 0.0


def collect_system_diagnostics(
    settings: Settings, db: Database, started_at: float = 0.0
) -> dict[str, Any]:
    load_avg = "n/a"
    if hasattr(os, "getloadavg"):
        try:
            l1, l5, l15 = os.getloadavg()
            load_avg = f"{l1:.2f}, {l5:.2f}, {l15:.2f}"
        except Exception:
            pass

    disk_total, disk_used, disk_free, disk_percent = 0.0, 0.0, 0.0, 0.0
    try:
        usage = shutil.disk_usage(settings.data_dir if settings.data_dir.exists() else BASE_DIR)
        disk_total = usage.total / (1024**3)
        disk_used = usage.used / (1024**3)
        disk_free = usage.free / (1024**3)
        disk_percent = (usage.used / usage.total) * 100 if usage.total > 0 else 0.0
    except Exception:
        pass

    db_size_kb = 0.0
    try:
        if settings.db_path.exists():
            db_size_kb = settings.db_path.stat().st_size / 1024
    except Exception:
        pass

    return {
        "bot_name": settings.bot_name,
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python_version": f"Python {platform.python_version()}",
        "uptime": format_uptime(started_at),
        "load_avg": load_avg,
        "threads": threading.active_count(),
        "memory_rss_mb": get_process_rss_mb(),
        "disk_free_gb": disk_free,
        "disk_total_gb": disk_total,
        "disk_used_gb": disk_used,
        "disk_used_percent": disk_percent,
        "db_size_kb": db_size_kb,
        "git_commit": get_git_commit(BASE_DIR),
    }


def format_system_diagnostics(info: dict[str, Any]) -> str:
    return (
        f"*{info['bot_name']}: system diagnostics*\n"
        f"• host: `{info['platform']}`\n"
        f"• runtime: `{info['python_version']}` · uptime: *{info['uptime']}*\n"
        f"• load avg (1, 5, 15m): `{info['load_avg']}` · threads: `{info['threads']}`\n"
        f"• memory rss: *{info['memory_rss_mb']:.1f} MB*\n"
        f"• disk: *{info['disk_free_gb']:.1f} GB* free of {info['disk_total_gb']:.1f} GB ({info['disk_used_percent']:.1f}% used)\n"
        f"• database: `{info['db_size_kb']:.1f} KB` on disk\n"
        f"• git commit: `{info['git_commit']}`"
    )


def build_digest(db: Database, settings: Settings, tz: ZoneInfo) -> str:
    stats = collect_stats(db)
    lines = [
        f"*good morning - {settings.bot_name} daily digest*",
        f"_{datetime.now(tz).strftime('%A %d %B %Y, %H:%M %Z')}_",
        "",
        f"*joins:* {stats['joins_24h']} in the last 24h · {stats['joins_7d']} this week",
        f"*leaves:* {stats['leaves_24h']} in the last 24h",
        f"*tier 2:* {stats['pending_access']} pending",
        f"*polls:* {stats['open_polls']} open · *reminders:* {stats['reminders']} active",
    ]

    pending = db.pending_access()
    if pending:
        lines.append("")
        lines.append("*pending requests:*")
        for request in pending[:10]:
            reason = (request.get("reason") or "no reason given").replace("\n", " ")
            if len(reason) > 100:
                reason = reason[:97] + "..."
            lines.append(f"• `#{request['id']}` <@{request['user_id']}>: {reason}")

    upcoming = db.list_reminders()
    if upcoming:
        lines.append("")
        lines.append("*next reminders:*")
        for reminder in upcoming[:5]:
            when = datetime.fromtimestamp(reminder["next_run"], tz)
            body = reminder["text"].replace("\n", " ")
            if len(body) > 80:
                body = body[:77] + "..."
            lines.append(f"• {when.strftime('%a %H:%M')}: {body}")

    return "\n".join(lines)

"""Who is allowed to spend tokens on image generation.

`IMAGE_ALLOWED_USER_IDS` seeds the list from the environment. Admins can then
add or remove people at runtime; those changes live in the kv store so they
survive restarts without editing `.env`.
"""

from __future__ import annotations

from bot.config import Settings
from bot.db import Database

KEY = "config:image_users"


def extra(db: Database | None) -> set[str]:
    raw = db.kv_get(KEY) if db is not None else ""
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


def all_users(settings: Settings, db: Database | None) -> set[str]:
    return set(settings.image_user_ids) | extra(db)


def can_generate(settings: Settings, db: Database | None, user_id: str | None) -> bool:
    if settings.image_allowed(user_id):
        return True
    return bool(user_id) and user_id in extra(db)


def add(db: Database, user_id: str) -> set[str]:
    users = extra(db) | {user_id}
    db.kv_set(KEY, ",".join(sorted(users)))
    return users


def remove(db: Database, user_id: str) -> set[str]:
    users = extra(db) - {user_id}
    db.kv_set(KEY, ",".join(sorted(users)))
    return users


__all__ = ["KEY", "add", "all_users", "can_generate", "extra", "remove"]

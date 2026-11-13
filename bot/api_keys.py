"""Optional API tokens for services beyond the built-in integrations.

Tokens are stored in SQLite through the setup modal, or can be supplied as env
vars on the server. The generic ``api_request`` tool uses these presets so the
model can call a real API without inventing endpoints or auth headers.
"""

from __future__ import annotations

import os
from typing import Any

from bot.config import Settings
from bot.db import Database

API_SERVICES: dict[str, dict[str, Any]] = {
    "vercel": {
        "label": "Vercel",
        "base": "https://api.vercel.com",
        "auth": "bearer",
        "env": "VERCEL_TOKEN",
    },
    "cloudflare": {
        "label": "Cloudflare",
        "base": "https://api.cloudflare.com/client/v4",
        "auth": "bearer",
        "env": "CLOUDFLARE_API_TOKEN",
    },
    "notion": {
        "label": "Notion",
        "base": "https://api.notion.com/v1",
        "auth": "bearer",
        "env": "NOTION_TOKEN",
        "headers": {"Notion-Version": "2022-06-28"},
    },
    "linear": {
        "label": "Linear",
        "base": "https://api.linear.app",
        "auth": "raw",
        "env": "LINEAR_API_KEY",
    },
    "sentry": {
        "label": "Sentry",
        "base": "https://sentry.io/api/0",
        "auth": "bearer",
        "env": "SENTRY_AUTH_TOKEN",
    },
    "resend": {
        "label": "Resend",
        "base": "https://api.resend.com",
        "auth": "bearer",
        "env": "RESEND_API_KEY",
    },
    "openai": {
        "label": "OpenAI",
        "base": "https://api.openai.com/v1",
        "auth": "bearer",
        "env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "label": "Anthropic",
        "base": "https://api.anthropic.com/v1",
        "auth": "x-api-key",
        "env": "ANTHROPIC_API_KEY",
        "headers": {"anthropic-version": "2023-06-01"},
    },
}


def api_service(service: str) -> dict[str, Any] | None:
    return API_SERVICES.get((service or "").strip().lower())


def api_token(db: Database | None, settings: Settings, service: str) -> str:
    preset = api_service(service)
    if preset is None:
        return ""
    env_name = str(preset.get("env") or "")
    if env_name:
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    if db is not None:
        return (db.kv_get(f"config:api_token:{service}") or "").strip()
    return ""


def api_key_status(db: Database | None, settings: Settings) -> dict[str, bool]:
    return {
        name: bool(api_token(db, settings, name))
        for name in API_SERVICES
    }


def save_api_token(db: Database | None, service: str, token: str) -> None:
    if db is None or api_service(service) is None:
        return
    token = token.strip()
    if token:
        db.kv_set(f"config:api_token:{service}", token)
    else:
        db.kv_del(f"config:api_token:{service}")


def auth_headers(service: str, token: str) -> dict[str, str]:
    preset = api_service(service) or {}
    headers = dict(preset.get("headers") or {})
    kind = str(preset.get("auth") or "bearer")
    if kind == "raw":
        headers["Authorization"] = token
    elif kind == "x-api-key":
        headers["x-api-key"] = token
    else:
        headers["Authorization"] = f"Bearer {token}"
    return headers

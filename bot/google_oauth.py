"""Google OAuth helpers for chat-based setup.

The setup flow uses a desktop-client OAuth app and a loopback redirect. On a VPS
there is no browser on the server, so the owner opens the auth URL on their own
machine, approves access, then pastes the redirected URL (or just the `code`
parameter) back into Slack.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_REDIRECT_URI = "http://localhost:9876/oauth2callback"

SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
)


class GoogleOAuthError(RuntimeError):
    """A setup error safe to show the owner (never contains tokens)."""


def build_auth_url(client_id: str, redirect_uri: str, state: str = "") -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def parse_oauth_input(raw: str) -> tuple[str, str]:
    """Return (code, state) from a raw code or the full redirected URL.

    Google shows a browser error page after redirecting to localhost. The URL in
    the address bar still contains the code, so the owner can paste that.
    """
    value = (raw or "").strip().strip("<>").strip()
    if not value:
        return "", ""
    if "code=" in value:
        query = urlparse(value).query or value.partition("?")[2]
        params = parse_qs(query)
        code = (params.get("code") or [""])[0].strip()
        state = (params.get("state") or [""])[0].strip()
        return code, state
    return value, ""


def normalize_token_response(data: dict[str, Any]) -> dict[str, Any]:
    access_token = str(data.get("access_token") or "")
    if not access_token:
        raise GoogleOAuthError("Google did not return an access token.")
    try:
        expires_in = int(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    return {
        "access_token": access_token,
        "refresh_token": str(data.get("refresh_token") or ""),
        "expires_in": expires_in,
        "scope": str(data.get("scope") or ""),
        "token_type": str(data.get("token_type") or "Bearer"),
    }


async def _token_request(payload: dict[str, str]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            response = await http.post(GOOGLE_TOKEN_URL, data=payload)
    except Exception as exc:
        raise GoogleOAuthError(f"could not reach Google: {exc}") from exc
    if response.status_code != 200:
        detail = ""
        try:
            body = response.json()
            detail = str(body.get("error_description") or body.get("error") or "")
        except Exception:  # noqa: BLE001
            detail = response.text[:200]
        raise GoogleOAuthError(
            f"Google rejected the authorization ({response.status_code})"
            + (f": {detail}" if detail else ".")
        )
    try:
        return response.json()
    except Exception as exc:
        raise GoogleOAuthError("Google returned an unreadable token response.") from exc


async def exchange_code(
    *, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict[str, Any]:
    if not client_id or not client_secret:
        raise GoogleOAuthError("Google client id and secret are required.")
    if not code:
        raise GoogleOAuthError("The authorization code is missing.")
    data = await _token_request(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    )
    return normalize_token_response(data)


async def refresh_access_token(
    *, client_id: str, client_secret: str, refresh_token: str
) -> dict[str, Any]:
    if not (client_id and client_secret and refresh_token):
        raise GoogleOAuthError("Google refresh credentials are incomplete.")
    data = await _token_request(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )
    return normalize_token_response(data)


async def fetch_identity(access_token: str) -> dict[str, Any]:
    """Return the connected account's email and a basic calendar check."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as http:
            profile_resp = await http.get("https://www.googleapis.com/oauth2/v2/userinfo")
            if profile_resp.status_code != 200:
                raise GoogleOAuthError(
                    f"Google profile check failed ({profile_resp.status_code})."
                )
            profile = profile_resp.json() or {}
            calendar_resp = await http.get(
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                params={"maxResults": 1},
            )
    except GoogleOAuthError:
        raise
    except Exception as exc:
        raise GoogleOAuthError(f"Google API check failed: {exc}") from exc

    return {
        "email": str(profile.get("email") or ""),
        "name": str(profile.get("name") or ""),
        "calendar_ok": calendar_resp.status_code == 200,
    }

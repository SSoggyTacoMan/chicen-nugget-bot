import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from bot import integrations
from bot.db import Database
from bot.google_oauth import (
    GoogleOAuthError,
    build_auth_url,
    normalize_token_response,
    parse_oauth_input,
)


def test_google_auth_url_contains_required_params():
    url = build_auth_url("client-123", "http://localhost:9876/oauth2callback", "state-abc")
    assert "client_id=client-123" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=state-abc" in url
    assert "calendar" in url
    assert "gmail.modify" in url


def test_parse_oauth_input_accepts_url_or_code():
    raw = "http://localhost:9876/oauth2callback?state=abc&code=4/xyz-123"
    assert parse_oauth_input(raw) == ("4/xyz-123", "abc")
    assert parse_oauth_input("<http://localhost:9876/oauth2callback?code=4/one>") == (
        "4/one",
        "",
    )
    assert parse_oauth_input("  4/raw-code  ") == ("4/raw-code", "")
    assert parse_oauth_input("") == ("", "")


def test_normalize_token_response():
    data = normalize_token_response(
        {"access_token": "at", "refresh_token": "rt", "expires_in": "120"}
    )
    assert data["access_token"] == "at"
    assert data["refresh_token"] == "rt"
    assert data["expires_in"] == 120
    try:
        normalize_token_response({})
    except GoogleOAuthError as exc:
        assert "access token" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("missing access token should fail")


def test_exchange_code_uses_token_endpoint():
    from bot import google_oauth

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, data):
            assert url == google_oauth.GOOGLE_TOKEN_URL
            assert data["grant_type"] == "authorization_code"
            assert data["code"] == "4/code"
            return FakeResponse()

    async def run():
        with patch("bot.google_oauth.httpx.AsyncClient", FakeClient):
            tokens = await google_oauth.exchange_code(
                client_id="id",
                client_secret="secret",
                code="4/code",
                redirect_uri="http://localhost:9876/oauth2callback",
            )
        assert tokens["access_token"] == "at"

    asyncio.run(run())


def test_integrations_prefer_db_and_fall_back_to_env(tmp_path):
    db = Database(tmp_path / "integrations.db")
    db.init()
    settings = SimpleNamespace(
        google_client_id="env-client",
        google_client_secret="env-secret",
        google_refresh_token=None,
        google_access_token=None,
        google_redirect_uri="http://localhost:9876/oauth2callback",
        mac_bridge_url=None,
        mac_bridge_token=None,
        github_username="env-user",
        github_token=None,
        repo_url="https://github.com/env/repo",
        imap_host=None,
        imap_port=993,
        imap_user=None,
        imap_password=None,
        slack_owner_token=None,
        slack_owner_cookie=None,
    )

    google = integrations.google_credentials(db, settings)
    assert google["client_id"] == "env-client"

    db.kv_set("config:google_client_id", "db-client")
    google = integrations.google_credentials(db, settings)
    assert google["client_id"] == "db-client"
    assert google["client_secret"] == "env-secret"

    assert integrations.imap_config(db, settings)["port"] == "993"
    assert integrations.github_config(db, settings)["username"] == "env-user"

    db.kv_set("config:imap_host", "imap.example.com")
    db.kv_set("config:imap_user", "me@example.com")
    db.kv_set("config:imap_password", "app-password")
    assert integrations.imap_config(db, settings)["host"] == "imap.example.com"

    cleared = integrations.clear_integration(db, "email")
    assert cleared == 3
    assert integrations.imap_config(db, settings)["host"] == ""


def test_setup_modals_do_not_prefill_secrets():
    from bot.features import setup

    slack_modal = setup._integration_modal("slack")
    assert slack_modal is not None
    block_ids = {block.get("block_id") for block in slack_modal["blocks"]}
    assert {"token", "cookie"} <= block_ids
    for block in slack_modal["blocks"]:
        element = block.get("element") or {}
        if block.get("block_id") in {"token", "cookie"}:
            assert "initial_value" not in element

    google_modal = setup._google_credentials_modal()
    assert google_modal["callback_id"] == "setup_google_credentials"
    assert "http://localhost:9876/oauth2callback" in json.dumps(google_modal)


def test_setup_merge_semantics():
    from bot.features.setup import _merge

    assert _merge("", "current") == "current"
    assert _merge("new", "current") == "new"
    assert _merge("-", "current") == ""
    assert _merge("clear", "current") == ""


def test_owner_token_validation_rejects_wrong_identity():
    from bot.owner_posting import validate_owner_token

    class FakeClient:
        def __init__(self, token=None, headers=None):
            self.token = token
            self.headers = headers

        async def auth_test(self):
            if self.token == "xoxp-wrong":
                return {"user_id": "U_OTHER", "user": "other"}
            return {"user_id": "U_OWNER", "user": "owner"}

    settings = SimpleNamespace(owner_user_id="U_OWNER")

    async def run():
        with patch("bot.owner_posting.AsyncWebClient", FakeClient):
            wrong = await validate_owner_token(
                token="xoxp-wrong", cookie="", settings=settings
            )
            right = await validate_owner_token(
                token="xoxp-right", cookie="", settings=settings
            )
            bot_token = await validate_owner_token(
                token="xoxb-nope", cookie="", settings=settings
            )
            xoxc_without_cookie = await validate_owner_token(
                token="xoxc-nope", cookie="", settings=settings
            )
        assert "not the configured owner" in wrong.error
        assert right.client is not None
        assert right.user_id == "U_OWNER"
        assert "bot token" in bot_token.error
        assert "cookie" in xoxc_without_cookie.error

    asyncio.run(run())


def test_daily_briefing_collects_tasks_and_reminders(tmp_path):
    from bot.ai.tools import Toolbox, ToolContext

    db = Database(tmp_path / "briefing.db")
    db.init()
    db.add_task("U_OWNER", "ship the setup flow")
    db.add_reminder(
        "U_OWNER",
        "U_OWNER",
        "standup",
        datetime.now(UTC),
        None,
    )

    settings = MagicMock()
    box = Toolbox(settings)
    ctx = ToolContext(
        db=db,
        settings=settings,
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_OWNER",
        channel_id="D1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )

    result = asyncio.run(
        box.execute("daily_briefing", {"include": ["tasks", "reminders"]}, ctx)
    )
    data = json.loads(result)
    assert data["sections"]["tasks"][0]["title"] == "ship the setup flow"
    assert data["sections"]["reminders"][0]["text"] == "standup"


def test_gmail_archive_uses_modify_endpoint():
    from bot.ai.tools import Toolbox, ToolContext

    settings = MagicMock()
    settings.google_access_token = "token"
    settings.google_refresh_token = None
    settings.google_client_id = None
    settings.google_client_secret = None
    box = Toolbox(settings)
    ctx = ToolContext(
        db=MagicMock(),
        settings=settings,
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_OWNER",
        channel_id="D1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {}

    async def fake_post(self, url, *args, **kwargs):
        assert "/messages/abc123/modify" in url
        assert kwargs["json"] == {"removeLabelIds": ["INBOX"]}
        return FakeResponse()

    async def run():
        with patch.object(httpx.AsyncClient, "post", new=fake_post):
            result = await box.execute(
                "gmail", {"action": "archive", "message_id": "abc123"}, ctx
            )
        assert "archived" in result

    asyncio.run(run())


def test_calendar_freebusy_returns_busy_blocks():
    from bot.ai.tools import Toolbox, ToolContext

    settings = MagicMock()
    settings.google_access_token = "token"
    settings.google_refresh_token = None
    settings.google_client_id = None
    settings.google_client_secret = None
    box = Toolbox(settings)
    ctx = ToolContext(
        db=MagicMock(),
        settings=settings,
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_OWNER",
        channel_id="D1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "calendars": {
                    "primary": {
                        "busy": [{"start": "2026-11-12T10:00:00Z", "end": "2026-11-12T11:00:00Z"}]
                    }
                }
            }

    async def fake_post(self, url, *args, **kwargs):
        assert url.endswith("/freeBusy")
        return FakeResponse()

    async def run():
        with patch.object(httpx.AsyncClient, "post", new=fake_post):
            result = await box.execute("google_calendar", {"action": "freebusy"}, ctx)
        assert "2026-11-12T10:00:00Z" in result

    asyncio.run(run())


def test_setup_route_and_prompt():
    from bot.ai.agent import PROMPT_PATH
    from bot.features import router

    handler, rest = router.resolve("setup status")
    assert handler is router.cmd_setup
    assert rest == "status"

    text = PROMPT_PATH.read_text(encoding="utf-8").lower()
    assert "setup from chat" in text
    assert "integration_setup" in text
    assert "daily_briefing" in text


def _google_setup_runtime(tmp_path, monkeypatch, state: str = "state-1"):
    from bot.features import setup
    from bot.runtime import runtime

    db = Database(tmp_path / "google-setup.db")
    db.init()
    db.kv_set("config:google_client_id", "client-id")
    db.kv_set("config:google_client_secret", "client-secret")
    db.kv_set("config:google_oauth_state", state)
    settings = SimpleNamespace(
        google_client_id=None,
        google_client_secret=None,
        google_refresh_token=None,
        google_access_token=None,
        google_redirect_uri="http://localhost:9876/oauth2callback",
    )
    monkeypatch.setattr(runtime, "db", db)
    monkeypatch.setattr(runtime, "settings", settings)
    return setup, db


def test_google_code_exchange_saves_tokens(tmp_path, monkeypatch):
    setup, db = _google_setup_runtime(tmp_path, monkeypatch)

    async def fake_exchange(**kwargs):
        assert kwargs["code"] == "4/abc"
        assert kwargs["redirect_uri"] == "http://localhost:9876/oauth2callback"
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

    async def fake_identity(token):
        assert token == "at"
        return {"email": "me@example.com", "calendar_ok": True}

    monkeypatch.setattr(setup, "exchange_code", fake_exchange)
    monkeypatch.setattr(setup, "fetch_identity", fake_identity)

    raw = "http://localhost:9876/oauth2callback?state=state-1&code=4/abc"
    message = asyncio.run(setup._complete_google_code(AsyncMock(), "U_OWNER", raw))

    assert "connected as me@example.com" in message
    assert db.kv_get("config:google_access_token") == "at"
    assert db.kv_get("config:google_refresh_token") == "rt"
    assert db.kv_get("config:google_email") == "me@example.com"
    assert db.kv_get("config:google_oauth_state") is None
    assert db.kv_get("cache:google_access_token") == "at"


def test_google_code_rejects_stale_state(tmp_path, monkeypatch):
    setup, db = _google_setup_runtime(tmp_path, monkeypatch, state="new-state")
    raw = "http://localhost:9876/oauth2callback?state=old-state&code=4/abc"
    message = asyncio.run(setup._complete_google_code(AsyncMock(), "U_OWNER", raw))

    assert "older setup attempt" in message
    assert db.kv_get("config:google_access_token") is None


def test_status_understands_access_token_only(tmp_path):
    settings = SimpleNamespace(
        google_client_id=None,
        google_client_secret=None,
        google_refresh_token=None,
        google_access_token="temporary",
        google_redirect_uri="http://localhost:9876/oauth2callback",
        mac_bridge_url=None,
        mac_bridge_token=None,
        github_username=None,
        github_token=None,
        repo_url=None,
        imap_host=None,
        imap_port=993,
        imap_user=None,
        imap_password=None,
        slack_owner_token=None,
        slack_owner_cookie=None,
    )
    text = integrations.setup_status_text(None, settings)
    assert "access token configured" in text


def test_secret_config_writes_are_blocked(tmp_path):
    from bot.ai.tools import Toolbox, ToolContext

    db = Database(tmp_path / "secrets.db")
    db.init()
    settings = MagicMock()
    box = Toolbox(settings)
    ctx = ToolContext(
        db=db,
        settings=settings,
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_OWNER",
        channel_id="D1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )

    result = asyncio.run(
        box.execute(
            "bot_config",
            {"action": "set", "key": "github_token", "value": "ghp_secret"},
            ctx,
        )
    )
    assert "secrets can't be set in chat" in result
    assert "setup github" in result
    assert db.kv_get("config:github_token") is None

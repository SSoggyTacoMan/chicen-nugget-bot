from bot.ai.limits import RateLimiter
from bot.features.ai_commands import _parse_imagine, _summary_limit
from bot.features.nuggets import FACTS


def test_rate_limiter_blocks_and_recovers():
    limiter = RateLimiter(2, window_seconds=60)
    assert limiter.allow("U1", now=0) == (True, 0)
    assert limiter.allow("U1", now=1) == (True, 0)
    allowed, retry = limiter.allow("U1", now=2)
    assert allowed is False
    assert retry >= 58
    assert limiter.allow("U2", now=2) == (True, 0)
    assert limiter.allow("U1", now=61) == (True, 0)


def test_rate_limiter_disabled():
    limiter = RateLimiter(0)
    for _ in range(50):
        assert limiter.allow("U1") == (True, 0)


def test_parse_imagine_flags():
    assert _parse_imagine("a nugget astronaut --wide") == ("a nugget astronaut", "16:9")
    assert _parse_imagine("a nugget castle --tall") == ("a nugget castle", "9:16")
    assert _parse_imagine("a nugget --ratio 4:3") == ("a nugget", "4:3")
    assert _parse_imagine("just a nugget") == ("just a nugget", "1:1")


def test_summary_limit():
    assert _summary_limit(None) == 50
    assert _summary_limit("10") == 10
    assert _summary_limit("0") == 10
    assert _summary_limit("999") == 200
    assert _summary_limit("wat") == 50


def test_nugget_facts_exist():
    assert len(FACTS) >= 20
    assert all(fact.strip() for fact in FACTS)


def test_chunk_text_empty_on_blank():
    from bot.utils.text import chunk_text

    assert chunk_text("") == []
    assert chunk_text("   ") == []
    assert chunk_text(None) == []


def test_parse_slack_ref():
    from bot.ai.tools import parse_slack_ref, parse_slack_thread_ref

    # Standard Slack permalink
    cid, ts = parse_slack_ref("https://hackclub.slack.com/archives/C078VTK9A/p1712345678123456")
    assert cid == "C078VTK9A"
    assert ts == "1712345678.123456"

    # Thread permalink with query params
    cid, ts = parse_slack_ref("https://hackclub.slack.com/archives/C078VTK9A/p1712345699999999?thread_ts=1712345600.000000&cid=C078VTK9A")
    assert cid == "C078VTK9A"
    assert ts == "1712345699.999999"

    # Thread parser specifically extracts thread_ts when present
    t_cid, t_ts = parse_slack_thread_ref("https://hackclub.slack.com/archives/C078VTK9A/p1712345699999999?thread_ts=1712345600.000000&cid=C078VTK9A")
    assert t_cid == "C078VTK9A"
    assert t_ts == "1712345600.000000"

    # Raw channel:ts reference
    cid, ts = parse_slack_ref("C01234567:1699999999.000001")
    assert cid == "C01234567"
    assert ts == "1699999999.000001"

    # Channel mention
    cid, ts = parse_slack_ref("<#C078VTK9A|general>")
    assert cid == "C078VTK9A"
    assert ts is None

    # Channel ID alone
    cid, ts = parse_slack_ref("C078VTK9A")
    assert cid == "C078VTK9A"
    assert ts is None

    # Invalid ref
    cid, ts = parse_slack_ref("not-a-link")
    assert cid is None
    assert ts is None


def test_toolbox_has_expanded_tools():
    from unittest.mock import MagicMock
    from bot.ai.tools import Toolbox

    box = Toolbox(MagicMock())
    names = {t["function"]["name"] for t in box.definitions}
    assert "flaron" in names
    assert "fetch_slack_message" in names
    assert "summarize_thread" in names
    assert "ocr_document" in names
    assert "public_holidays" in names
    assert "search_web" in names
    assert "fetch_url" in names
    assert "check_emails" in names
    assert "hackatime_stats" in names
    assert "github_info" in names
    assert "run_code" in names
    assert "hacker_news" in names
    assert "http_status" in names
    assert "dog_fact" in names
    assert "taco_recipe" in names
    assert "urban_dict" in names
    assert "system_diagnostics" in names
    assert "manage_tasks" in names


def test_owner_only_tools_guard():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from bot.ai.tools import Toolbox, ToolContext

    box = Toolbox(MagicMock())
    ctx = ToolContext(
        db=MagicMock(),
        settings=MagicMock(),
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_GUEST",
        channel_id="C1",
        thread_ts=None,
        is_dm=True,
        is_owner=False,
        is_admin=False,
    )

    async def run():
        res1 = await box.execute("fetch_slack_message", {"ref": "C1:123.456"}, ctx)
        assert "only the owner/admin" in res1

        res2 = await box.execute("summarize_thread", {"ref": "C1:123.456"}, ctx)
        assert "only the owner/admin" in res2

        res3 = await box.execute("ocr_document", {"url": "https://example.com/doc.pdf"}, ctx)
        assert "only the owner/admin" in res3

        res4 = await box.execute("check_emails", {}, ctx)
        assert "only the owner/admin" in res4

        res5 = await box.execute("run_code", {"code": "print(1)"}, ctx)
        assert "only the owner/admin" in res5

        res6 = await box.execute("hacker_news", {}, ctx)
        assert "only the owner/admin" in res6

        res7 = await box.execute("http_status", {"url": "https://example.com"}, ctx)
        assert "only the owner/admin" in res7

        res8 = await box.execute("system_diagnostics", {}, ctx)
        assert "only the owner/admin" in res8

    asyncio.run(run())


def test_dog_and_taco_channel_tools():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from bot.ai.tools import Toolbox, ToolContext

    box = Toolbox(MagicMock())
    ctx = ToolContext(
        db=MagicMock(),
        settings=MagicMock(),
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_GUEST",
        channel_id="C1",
        thread_ts=None,
        is_dm=False,
        is_owner=False,
        is_admin=False,
    )

    async def run():
        taco = await box.execute("taco_recipe", {"theme": "spicy"}, ctx)
        assert ":taco:" in taco
        assert "Ghost Pepper Barbacoa" in taco

    asyncio.run(run())


def test_run_code_execution():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from bot.ai.tools import Toolbox, ToolContext

    box = Toolbox(MagicMock())
    ctx = ToolContext(
        db=MagicMock(),
        settings=MagicMock(),
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_OWNER",
        channel_id="C1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )

    async def run():
        out = await box.execute("run_code", {"code": "print(2 + 2)"}, ctx)
        assert out == "4"

    asyncio.run(run())


def test_email_helpers_and_unconfigured():
    import asyncio
    from email.message import EmailMessage
    from unittest.mock import AsyncMock, MagicMock
    from bot.ai.tools import Toolbox, ToolContext, _decode_mime, _extract_mime_body

    assert _decode_mime("=?utf-8?B?SGVsbG8gV29ybGQ=?=") == "Hello World"
    assert _decode_mime("plain text") == "plain text"

    msg = EmailMessage()
    msg["Subject"] = "Test"
    msg.set_content("Hello from test email!")
    assert _extract_mime_body(msg) == "Hello from test email!"

    # Unconfigured settings return clear message
    settings = MagicMock()
    settings.imap_host = None
    settings.imap_user = None
    settings.imap_password = None
    box = Toolbox(settings)
    ctx = ToolContext(
        db=MagicMock(),
        settings=settings,
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_OWNER",
        channel_id="C1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )
    res = asyncio.run(box.execute("check_emails", {}, ctx))
    assert "email credentials not configured" in res


def test_bot_config_tool(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from bot.ai.tools import Toolbox, ToolContext
    from bot.db import Database

    db = Database(str(tmp_path / "test.db"))
    db.init()
    settings = MagicMock()
    box = Toolbox(settings)

    ctx_guest = ToolContext(
        db=db,
        settings=settings,
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_GUEST",
        channel_id="C1",
        thread_ts=None,
        is_dm=False,
        is_owner=False,
        is_admin=False,
    )

    ctx_owner = ToolContext(
        db=db,
        settings=settings,
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_OWNER",
        channel_id="C1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )

    async def run():
        # Guest denied
        denied = await box.execute("bot_config", {"action": "list"}, ctx_guest)
        assert "only the owner/admin" in denied

        # Owner can list
        lst = await box.execute("bot_config", {"action": "list"}, ctx_owner)
        assert "current bot runtime settings" in lst
        assert "recaps" in lst

        # Turn off recaps
        res = await box.execute("bot_config", {"action": "set", "key": "recaps", "value": "off"}, ctx_owner)
        assert "daily recaps disabled" in res
        assert db.kv_get("config:recaps") == "false"

        # Check recaps setting
        get_res = await box.execute("bot_config", {"action": "get", "key": "recaps"}, ctx_owner)
        assert "currently `false`" in get_res

        # Reset recaps
        reset_res = await box.execute("bot_config", {"action": "reset", "key": "recaps"}, ctx_owner)
        assert "reset to default" in reset_res
        assert db.kv_get("config:recaps") is None

        # Set tier2 channel with slack mention format
        res_ch = await box.execute("bot_config", {"action": "set", "key": "tier2_channel", "value": "<#C012345678|tier2>"}, ctx_owner)
        assert "C012345678" in res_ch
        assert db.kv_get("config:access_channel_id") == "C012345678"

    asyncio.run(run())


def test_fetch_slack_images():
    import asyncio
    from unittest.mock import MagicMock, patch
    import httpx
    from bot.slack_api import fetch_slack_images

    event = {
        "files": [
            {
                "mimetype": "image/png",
                "url_private": "https://files.slack.com/img.png",
                "size": 1024,
            },
            {
                "mimetype": "image/svg+xml",
                "url_private": "https://files.slack.com/img.svg",
                "size": 500,
            },
            {
                "mimetype": "text/plain",
                "url_private": "https://files.slack.com/doc.txt",
                "size": 100,
            },
        ]
    }

    async def mock_get(url, *args, **kwargs):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.content = b"fake_png_data"
        return resp

    async def run():
        with patch.object(httpx.AsyncClient, "get", new=mock_get):
            urls = await fetch_slack_images(event, "xoxb-test")
            assert len(urls) == 1
            assert urls[0].startswith("data:image/png;base64,")

    asyncio.run(run())


def test_agent_reply_with_images(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from bot.ai.agent import Agent
    from bot.db import Database

    db = Database(str(tmp_path / "agent_test.db"))
    db.init()
    settings = MagicMock()
    settings.bot_name = "nugget"
    settings.ai_enabled = True
    settings.ai_api_key = "test-key"
    settings.ai_base_url = "https://ai.hackclub.com/proxy/v1"
    settings.ai_model = "test-model"
    settings.ai_fallback_model = ""
    settings.imagine_model = "test-imagine"
    settings.ai_gate_model = "test-gate"
    settings.ai_max_context_chars = 1000
    settings.ai_history_limit = 5
    settings.owner_user_id = "U_OWNER"
    settings.repo_url = "https://github.com/repo"
    settings.is_admin.return_value = False

    agent = Agent(settings, db, client=AsyncMock(), user_client=None, bot_user_id="U_BOT")

    captured_messages = []

    async def fake_chat(messages, *args, **kwargs):
        captured_messages.extend(messages)
        res = MagicMock()
        choice = MagicMock()
        choice.message.content = "I see a cute dog!"
        choice.message.tool_calls = []
        res.choices = [choice]
        return res

    agent._chat = fake_chat

    async def run():
        ans = await agent.reply(
            conversation="dm:U1",
            user_id="U1",
            text="what is this?",
            channel_id="C1",
            thread_ts=None,
            is_dm=True,
            images=["data:image/png;base64,ZmFrZQ=="],
        )
        assert ans == "I see a cute dog!"
        user_msg = [m for m in captured_messages if m.get("role") == "user"][-1]
        assert isinstance(user_msg["content"], list)
        assert any(p.get("type") == "image_url" for p in user_msg["content"])

    asyncio.run(run())


def test_fetch_slack_images_from_text_urls():
    import asyncio
    from unittest.mock import MagicMock, patch
    import httpx
    from bot.slack_api import fetch_slack_images

    event = {
        "text": "check out <https://example.com/cat.png> and https://images.org/dog.jpg"
    }

    async def mock_get(url, *args, **kwargs):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.content = b"fake_png_data"
        resp.headers = {"content-type": "image/png"}
        return resp

    async def run():
        with patch.object(httpx.AsyncClient, "get", new=mock_get):
            urls = await fetch_slack_images(event, "xoxb-test")
            assert len(urls) == 2
            assert all(u.startswith("data:image/") for u in urls)

    asyncio.run(run())


def test_cmd_config(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, patch
    from bot.db import Database
    from bot.features.commands import cmd_config
    from bot.runtime import runtime

    db = Database(str(tmp_path / "cmd_cfg.db"))
    db.init()
    runtime.db = db

    captured = []

    async def mock_say(client, command, text, ephemeral=True):
        captured.append(text)

    async def run():
        with patch("bot.features.commands._say", new=mock_say):
            # Non-admin
            await cmd_config(AsyncMock(), {"user_id": "U_GUEST", "text": "list", "channel_id": "C1"}, AsyncMock())
            assert "only admins" in captured[-1]

            # Admin list
            await cmd_config(AsyncMock(), {"user_id": runtime.settings.owner_user_id or "U_ADMIN", "text": "list", "channel_id": "C1"}, AsyncMock())
            assert "bot runtime settings" in captured[-1]

            # Set recap off
            await cmd_config(AsyncMock(), {"user_id": runtime.settings.owner_user_id or "U_ADMIN", "text": "recaps off", "channel_id": "C1"}, AsyncMock())
            assert "daily recaps disabled" in captured[-1]
            assert db.kv_get("config:recaps") == "false"

            # Get recap
            await cmd_config(AsyncMock(), {"user_id": runtime.settings.owner_user_id or "U_ADMIN", "text": "get recaps", "channel_id": "C1"}, AsyncMock())
            assert "`false`" in captured[-1]

            # Reset recap
            await cmd_config(AsyncMock(), {"user_id": runtime.settings.owner_user_id or "U_ADMIN", "text": "reset recaps", "channel_id": "C1"}, AsyncMock())
            assert "reset *recaps* to default" in captured[-1]
            assert db.kv_get("config:recaps") is None

    asyncio.run(run())





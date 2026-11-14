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
    assert "github_write" in names
    assert "run_code" in names
    assert "hacker_news" in names
    assert "http_status" in names
    assert "dog_fact" in names
    assert "taco_recipe" in names
    assert "urban_dict" in names
    assert "system_diagnostics" in names
    assert "manage_tasks" in names
    assert "inspect_code" in names
    assert "clone_repo" in names
    assert "sandbox" in names
    assert "manage_channel" in names
    assert "api_request" in names
    assert "browser" in names


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

        res9 = await box.execute("inspect_code", {"action": "list"}, ctx)
        assert "only the owner/admin" in res9

        res10 = await box.execute("clone_repo", {"repo_url": "test/repo"}, ctx)
        assert "only the owner/admin" in res10

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

        # Set admin channel to private channel
        res_adm = await box.execute("bot_config", {"action": "set", "key": "admin_channel", "value": "<#C99999999|admin-reviews>"}, ctx_owner)
        assert "C99999999" in res_adm
        assert db.kv_get("config:admin_channel_id") == "C99999999"

        # Set admin channel to dm
        res_dm = await box.execute("bot_config", {"action": "set", "key": "admin_channel", "value": "dm"}, ctx_owner)
        assert "admin DMs" in res_dm
        assert db.kv_get("config:admin_channel_id") == "dm"

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
    settings.ai_assistant_model = "assistant-model"
    settings.ai_assistant_fallback_model = ""
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

            # Per-channel recap toggle via cmd_config
            await cmd_config(AsyncMock(), {"user_id": runtime.settings.owner_user_id or "U_ADMIN", "text": "recaps <#C12345678> off", "channel_id": "C1"}, AsyncMock())
            assert "daily recaps disabled for <#C12345678>" in captured[-1]
            assert db.kv_get("config:recaps:C12345678") == "false"

    asyncio.run(run())


def test_thread_continuation_image_handling(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.ai.gate import GateDecision
    from bot.db import Database
    from bot.features.agent import _handle_thread_continuation
    from bot.runtime import runtime

    db = Database(str(tmp_path / "thread_test.db"))
    db.init()
    db.mark_thread("C_TEST", "100.000")
    runtime.db = db

    client = AsyncMock()
    # Mock gate
    mock_gate = MagicMock()
    old_gate = runtime.gate
    runtime.gate = mock_gate

    async def run():
        with patch("bot.features.agent._user_name", return_value="alice"), \
             patch("bot.features.agent._channel_name", return_value="general"):
            # Case 1: Image only, no text -> ignored, fetch_slack_images not even called
            with patch("bot.features.agent.fetch_slack_images") as mock_fetch, \
                 patch("bot.features.agent._handle_channel") as mock_handle:
                await _handle_thread_continuation(
                    {"channel": "C_TEST", "thread_ts": "100.000", "user": "U_USER", "text": ""},
                    client,
                )
                mock_fetch.assert_not_called()
                mock_handle.assert_not_called()

            # Case 2: Image with text, gate suppresses -> no reply
            mock_gate.decide = AsyncMock(return_value=GateDecision(reply=False, kind="chatter"))
            with patch("bot.features.agent.fetch_slack_images", return_value=["data:image/png;base64,abc"]) as mock_fetch, \
                 patch("bot.features.agent._handle_channel") as mock_handle:
                await _handle_thread_continuation(
                    {"channel": "C_TEST", "thread_ts": "100.000", "user": "U_USER", "text": "check this out"},
                    client,
                )
                mock_fetch.assert_called_once()
                mock_gate.decide.assert_called_once()
                mock_handle.assert_not_called()

            # Case 3: Image with text, gate allows -> handled with image and jev hint
            mock_gate.decide = AsyncMock(return_value=GateDecision(reply=True, kind="question"))
            with patch("bot.features.agent.fetch_slack_images", return_value=["data:image/png;base64,abc"]) as mock_fetch, \
                 patch("bot.features.agent._handle_channel") as mock_handle:
                await _handle_thread_continuation(
                    {"channel": "C_TEST", "thread_ts": "100.000", "user": "U_USER", "text": "what is this error?"},
                    client,
                )
                mock_fetch.assert_called_once()
                mock_handle.assert_called_once()
                _, kwargs = mock_handle.call_args
                assert kwargs["images"] == ["data:image/png;base64,abc"]
                assert kwargs["text"] == "what is this error?"

    try:
        asyncio.run(run())
    finally:
        runtime.gate = old_gate


def test_security_hardening(tmp_path):
    import asyncio
    import os
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx

    from bot.ai.tools import Toolbox, ToolContext
    from bot.slack_api import ssrf_filter
    from bot.utils.text import disarm_broadcasts, escape_mrkdwn

    # 1. Broadcast ping disarming & mrkdwn escaping
    raw = "hi <!channel> <!everyone> <!here> <!subteam^S1234|@team> <https://evil.com|click>"
    disarmed = disarm_broadcasts(raw)
    assert "<!channel>" not in disarmed
    assert "<!everyone>" not in disarmed
    assert "<!here>" not in disarmed
    assert "<!subteam^S1234" not in disarmed
    assert "@channel" in disarmed

    escaped = escape_mrkdwn("<!channel> <https://evil.com|safe>")
    assert "&lt;!channel&gt;" in escaped
    assert "&lt;https://evil.com|safe&gt;" in escaped

    # 2. SSRF redirect filter & private host check
    async def run_ssrf_and_run_code():
        req_private = httpx.Request("GET", "http://169.254.169.254/latest/meta-data/")
        try:
            await ssrf_filter(req_private)
            assert False, "should block metadata IP"
        except ValueError as e:
            assert "blocked" in str(e)

        req_loopback = httpx.Request("GET", "http://127.0.0.1:8080/admin")
        try:
            await ssrf_filter(req_loopback)
            assert False, "should block loopback IP"
        except ValueError as e:
            assert "blocked" in str(e)

        # 3. run_code rejects non-DM contexts and scrubs env secrets
        box = Toolbox(MagicMock())
        channel_ctx = ToolContext(
            db=MagicMock(),
            settings=MagicMock(),
            client=AsyncMock(),
            user_client=None,
            bot_user_id="U_BOT",
            user_id="U_OWNER",
            channel_id="C1",
            thread_ts=None,
            is_dm=False,
            is_owner=True,
            is_admin=True,
        )
        res_channel = await box.execute("run_code", {"code": "print(1)"}, channel_ctx)
        assert "direct messages" in res_channel

        dm_ctx = ToolContext(
            db=MagicMock(),
            settings=MagicMock(),
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
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-secret", "AI_API_KEY": "sk-secret"}):
            res_env = await box.execute(
                "run_code",
                {"code": "import os; print('TOKEN' in str(os.environ.keys()), os.getenv('SLACK_BOT_TOKEN'))"},
                dm_ctx,
            )
            assert "None" in res_env

        # 4. bot_config rejects unknown keys
        res_cfg = await box.execute("bot_config", {"action": "set", "key": "bad_key", "value": "1"}, dm_ctx)
        assert "unknown setting" in res_cfg

    asyncio.run(run_ssrf_and_run_code())


def test_inspect_code_tool():
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
        channel_id="D1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )

    async def run():
        lst = await box.execute("inspect_code", {"action": "list", "path": "prompts"}, ctx)
        assert "prompts/system.md" in lst

        rd = await box.execute("inspect_code", {"action": "read", "path": "prompts/system.md", "limit": 5}, ctx)
        assert "1: # who you are" in rd

        srch = await box.execute("inspect_code", {"action": "search", "pattern": "TOOL_RESULT"}, ctx)
        assert "MAX_TOOL_RESULT" in srch

        sec = await box.execute("inspect_code", {"action": "read", "path": ".env"}, ctx)
        assert "sensitive file" in sec or "file not found" in sec

        esc = await box.execute("inspect_code", {"action": "read", "path": "../../etc/passwd"}, ctx)
        assert "escapes repository root" in esc

    asyncio.run(run())


def test_clone_repo_tool(tmp_path):
    import asyncio
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.ai.tools import Toolbox, ToolContext

    box = Toolbox(MagicMock())
    ctx = ToolContext(
        db=MagicMock(),
        settings=MagicMock(),
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

    created_dirs = []

    async def fake_subprocess_exec(*cmd, **kwargs):
        dest = Path(cmd[-1])
        created_dirs.append(dest.parent)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "README.md").write_text("# Demo Repo\nhello world\n", encoding="utf-8")
        (dest / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    async def run():
        # Bad URL rejection
        bad = await box.execute("clone_repo", {"repo_url": "--upload-pack=evil"}, ctx)
        assert "invalid repo_url" in bad

        priv = await box.execute("clone_repo", {"repo_url": "http://127.0.0.1/repo"}, ctx)
        assert "private" in priv or "invalid" in priv

        with patch("bot.ai.tools.asyncio.create_subprocess_exec", side_effect=fake_subprocess_exec):
            res_list = await box.execute("clone_repo", {"repo_url": "owner/demo", "action": "list"}, ctx)
            assert "README.md" in res_list
            assert "app.py" in res_list

            res_read = await box.execute("clone_repo", {"repo_url": "owner/demo", "action": "read", "path": "app.py"}, ctx)
            assert "1: def greet():" in res_read

            res_search = await box.execute("clone_repo", {"repo_url": "owner/demo", "action": "search", "pattern": "hello world"}, ctx)
            assert "README.md:2: hello world" in res_search

            res_esc = await box.execute("clone_repo", {"repo_url": "owner/demo", "action": "read", "path": "../secret"}, ctx)
            assert "escapes" in res_esc

        assert len(created_dirs) == 4
        for d in created_dirs:
            assert not d.exists()

    asyncio.run(run())


def _sandbox_ctx(tmp_path, **overrides):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from bot.ai.tools import ToolContext

    settings = SimpleNamespace(
        data_dir=tmp_path,
        sandbox_dir=tmp_path / "sandboxes",
        sandbox_max_workspaces=5,
        sandbox_exec_timeout=20,
        sandbox_max_exec_timeout=60,
    )
    for key, value in overrides.pop("settings", {}).items():
        setattr(settings, key, value)
    defaults = dict(
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
    defaults.update(overrides)
    return ToolContext(**defaults)


def test_sandbox_create_exec_files_delete(tmp_path):
    import asyncio
    import json

    from bot.ai.tools import Toolbox

    box = Toolbox(_sandbox_ctx(tmp_path).settings)
    ctx = _sandbox_ctx(tmp_path)

    async def run():
        created = await box.execute("sandbox", {"action": "create", "name": "Demo Repo"}, ctx)
        assert "demo-repo" in created

        ran = await box.execute(
            "sandbox",
            {"action": "exec", "name": "demo-repo", "command": "printf 'hello\\n' > note.txt && echo built"},
            ctx,
        )
        payload = json.loads(ran)
        assert payload["exit_code"] == 0
        assert "built" in payload["stdout"]

        read = await box.execute("sandbox", {"action": "read", "name": "demo-repo", "path": "note.txt"}, ctx)
        assert "1: hello" in read

        listed = await box.execute("sandbox", {"action": "list_files", "name": "demo-repo"}, ctx)
        assert "note.txt" in listed

        found = await box.execute("sandbox", {"action": "search", "name": "demo-repo", "pattern": "hello"}, ctx)
        assert "note.txt:1: hello" in found

        info = await box.execute("sandbox", {"action": "info", "name": "demo-repo"}, ctx)
        assert json.loads(info)["sandbox"] == "demo-repo"

        all_boxes = json.loads(await box.execute("sandbox", {"action": "list"}, ctx))
        assert [item["name"] for item in all_boxes["sandboxes"]] == ["demo-repo"]

        deleted = await box.execute("sandbox", {"action": "delete", "name": "demo-repo"}, ctx)
        assert json.loads(deleted)["deleted"] == "demo-repo"
        assert not (tmp_path / "sandboxes" / "demo-repo").exists()
        after = json.loads(await box.execute("sandbox", {"action": "list"}, ctx))
        assert after["sandboxes"] == []

    asyncio.run(run())


def test_sandbox_background_jobs(tmp_path):
    import asyncio
    import json

    from bot.ai.tools import Toolbox

    box = Toolbox(_sandbox_ctx(tmp_path).settings)
    ctx = _sandbox_ctx(tmp_path)

    async def run():
        await box.execute("sandbox", {"action": "create", "name": "jobs"}, ctx)
        started = json.loads(
            await box.execute(
                "sandbox",
                {
                    "action": "exec",
                    "name": "jobs",
                    "command": "echo background-ok",
                    "background": True,
                },
                ctx,
            )
        )
        job_id = started["job_id"]
        status = {}
        for _ in range(80):
            status = json.loads(
                await box.execute(
                    "sandbox", {"action": "job", "job_id": job_id}, ctx
                )
            )
            if status["status"] == "done":
                break
            await asyncio.sleep(0.05)
        assert status["status"] == "done"
        assert status["exit_code"] == 0
        assert "background-ok" in status["output"]

        listed = json.loads(await box.execute("sandbox", {"action": "jobs"}, ctx))
        assert any(item["job_id"] == job_id for item in listed["jobs"])

    asyncio.run(run())


def test_sandbox_write_edit_and_upload(tmp_path):
    import asyncio

    from bot.ai.tools import Toolbox

    box = Toolbox(_sandbox_ctx(tmp_path).settings)
    ctx = _sandbox_ctx(tmp_path)

    async def run():
        await box.execute("sandbox", {"action": "create", "name": "editor"}, ctx)
        wrote = await box.execute(
            "sandbox",
            {
                "action": "write",
                "name": "editor",
                "path": "src/hello.py",
                "content": "print('hello')\n",
            },
            ctx,
        )
        assert "wrote" in wrote

        appended = await box.execute(
            "sandbox",
            {
                "action": "write",
                "name": "editor",
                "path": "src/hello.py",
                "content": "print('again')\n",
                "append": True,
            },
            ctx,
        )
        assert "appended" in appended

        edited = await box.execute(
            "sandbox",
            {
                "action": "edit",
                "name": "editor",
                "path": "src/hello.py",
                "find": "hello",
                "replace": "hi",
            },
            ctx,
        )
        assert "replaced 1" in edited

        read = await box.execute(
            "sandbox", {"action": "read", "name": "editor", "path": "src/hello.py"}, ctx
        )
        assert "print('hi')" in read
        assert "print('again')" in read

        upload = await box.execute(
            "sandbox",
            {"action": "upload", "name": "editor", "path": "src/hello.py"},
            ctx,
        )
        assert "uploaded hello.py" in upload
        ctx.client.files_upload_v2.assert_called_once()
        assert ctx.client.files_upload_v2.call_args[1]["file"].startswith(b"print('hi')")

        escaped = await box.execute(
            "sandbox",
            {
                "action": "write",
                "name": "editor",
                "path": "../outside.txt",
                "content": "nope",
            },
            ctx,
        )
        assert "escapes the sandbox" in escaped

    asyncio.run(run())


def test_sandbox_rejects_escape_non_owner_and_non_dm(tmp_path):
    import asyncio

    from bot.ai.tools import Toolbox

    box = Toolbox(_sandbox_ctx(tmp_path).settings)

    async def run():
        ctx = _sandbox_ctx(tmp_path)
        await box.execute("sandbox", {"action": "create", "name": "box"}, ctx)

        escaped = await box.execute(
            "sandbox", {"action": "read", "name": "box", "path": "../outside"}, ctx
        )
        assert "escapes the sandbox" in escaped

        bad_name = await box.execute(
            "sandbox", {"action": "create", "name": "../evil"}, ctx
        )
        assert "evil" not in bad_name or "error" in bad_name

        channel_ctx = _sandbox_ctx(tmp_path, channel_id="C1", is_dm=False)
        denied_dm = await box.execute("sandbox", {"action": "list"}, channel_ctx)
        assert "direct messages" in denied_dm

        stranger_ctx = _sandbox_ctx(tmp_path, user_id="U_RANDO", is_owner=False, is_admin=False)
        denied_owner = await box.execute("sandbox", {"action": "list"}, stranger_ctx)
        assert "owner/admin" in denied_owner

    asyncio.run(run())


def test_sandbox_clone_persists(tmp_path):
    import asyncio
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.ai.tools import Toolbox

    box = Toolbox(_sandbox_ctx(tmp_path).settings)
    ctx = _sandbox_ctx(tmp_path)

    async def fake_subprocess_exec(*cmd, **kwargs):
        dest = Path(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "README.md").write_text("# Demo\nhello from repo\n", encoding="utf-8")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    async def run():
        with patch("bot.ai.tools.asyncio.create_subprocess_exec", side_effect=fake_subprocess_exec):
            created = await box.execute(
                "sandbox", {"action": "create", "repo_url": "owner/demo"}, ctx
            )
        assert "demo" in created

        # Still there after the clone call returned, unlike clone_repo.
        listed = await box.execute("sandbox", {"action": "list_files", "name": "demo"}, ctx)
        assert "README.md" in listed
        read = await box.execute("sandbox", {"action": "read", "name": "demo", "path": "README.md"}, ctx)
        assert "hello from repo" in read

        await box.execute("sandbox", {"action": "delete", "name": "demo"}, ctx)
        assert not (tmp_path / "sandboxes" / "demo").exists()

    asyncio.run(run())


def test_sandbox_clone_failure_cleans_up(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.ai.tools import Toolbox

    box = Toolbox(_sandbox_ctx(tmp_path).settings)
    ctx = _sandbox_ctx(tmp_path)

    async def failing_clone(*cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = 128
        proc.communicate = AsyncMock(return_value=(b"", b"fatal: not found"))
        return proc

    async def run():
        with patch("bot.ai.tools.asyncio.create_subprocess_exec", side_effect=failing_clone):
            result = await box.execute(
                "sandbox", {"action": "create", "name": "broken", "repo_url": "owner/nope"}, ctx
            )
        assert "clone failed" in result
        assert not (tmp_path / "sandboxes" / "broken").exists()

    asyncio.run(run())


def test_sandbox_max_workspaces(tmp_path):
    import asyncio

    from bot.ai.tools import Toolbox

    box = Toolbox(_sandbox_ctx(tmp_path).settings)
    ctx = _sandbox_ctx(tmp_path, settings={"sandbox_max_workspaces": 1})

    async def run():
        first = await box.execute("sandbox", {"action": "create", "name": "one"}, ctx)
        assert "one" in first
        second = await box.execute("sandbox", {"action": "create", "name": "two"}, ctx)
        assert "too many sandboxes" in second

    asyncio.run(run())


def test_sandbox_defaults_to_latest_workspace(tmp_path):
    import asyncio
    import json

    from bot.ai.tools import Toolbox

    box = Toolbox(_sandbox_ctx(tmp_path).settings)
    ctx = _sandbox_ctx(tmp_path)

    async def run():
        empty = await box.execute("sandbox", {"action": "exec", "command": "echo hi"}, ctx)
        assert "create one first" in empty

        await box.execute("sandbox", {"action": "create", "name": "first"}, ctx)
        await box.execute("sandbox", {"action": "create", "name": "second"}, ctx)

        # No name means the newest sandbox, so "run it again" just works.
        ran = await box.execute(
            "sandbox", {"action": "exec", "command": "pwd && echo latest"}, ctx
        )
        payload = json.loads(ran)
        assert payload["exit_code"] == 0
        assert "second" in payload["stdout"]
        assert "latest" in payload["stdout"]

        # Delete must always be explicit.
        refused = await box.execute("sandbox", {"action": "delete"}, ctx)
        assert "name is required" in refused
        assert (tmp_path / "sandboxes" / "second").exists()

    asyncio.run(run())


def test_thread_chatter_suppression_and_speaker_tags(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.ai.agent import fit_history
    from bot.ai.gate import GateDecision
    from bot.db import Database
    from bot.features.agent import _handle_thread_continuation, _last_thread
    from bot.runtime import runtime

    # 1. fit_history adds speaker tags when user_id is present
    kept = fit_history(
        [
            {"role": "user", "user_id": "U1", "text": "hello"},
            {"role": "assistant", "user_id": None, "text": "hi"},
            {"role": "user", "user_id": "U2", "text": "what's up"},
        ],
        "",
        max_chars=500,
    )
    assert kept == [
        {"role": "user", "content": "<@U1>: hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "<@U2>: what's up"},
    ]

    # 2. Thread suppression of short fragments and multi-human chatter
    db = Database(str(tmp_path / "chatter_test.db"))
    db.init()
    db.mark_thread("C_CHAT", "500.000")
    db.add_message("ch:C_CHAT:500.000", "user", "U_ALICE", "is this novabot?")
    db.add_message("ch:C_CHAT:500.000", "assistant", None, "nope")
    db.add_message("ch:C_CHAT:500.000", "user", "U_BOB", "i copied it")
    runtime.db = db

    client = AsyncMock()
    mock_gate = MagicMock()
    mock_gate.decide = AsyncMock(return_value=GateDecision(reply=True, kind="chatter"))
    old_gate = runtime.gate
    runtime.gate = mock_gate
    _last_thread.clear()

    async def run():
        with patch("bot.features.agent._user_name", return_value="alice"), \
             patch("bot.features.agent._channel_name", return_value="general"), \
             patch("bot.features.agent.fetch_slack_images", AsyncMock(return_value=[])), \
             patch("bot.features.agent._handle_channel", AsyncMock(return_value=True)) as mock_handle:
            # Multi-human thread without '?' and without naming bot -> suppressed before gate
            await _handle_thread_continuation(
                {"channel": "C_CHAT", "thread_ts": "500.000", "user": "U_ALICE", "text": "the code is messy"},
                client,
            )
            mock_handle.assert_not_called()

            # Explicitly naming the bot bypasses multi-human suppression
            await _handle_thread_continuation(
                {"channel": "C_CHAT", "thread_ts": "500.000", "user": "U_ALICE", "text": "hey nugget what do you think"},
                client,
            )
            mock_handle.assert_called_once()

    try:
        asyncio.run(run())
    finally:
        runtime.gate = old_gate


def test_named_mention_uses_agent_without_at():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.features.agent import _looks_named, on_message
    from bot.runtime import runtime

    assert _looks_named("hey nugget what's up")
    assert _looks_named("nugget?")
    assert _looks_named("chicen nugget you around")
    assert not _looks_named("i ate a chicken nugget")
    assert not _looks_named("the nuggets won")

    old_settings, old_bot, old_user = runtime.settings, runtime.bot_user_id, runtime.user_id
    runtime.settings = MagicMock()
    runtime.settings.ai_allowed.return_value = True
    runtime.settings.slack_bot_token = "xoxb-test"
    runtime.bot_user_id = "UBOT"
    runtime.user_id = "UBOI"
    client = AsyncMock()

    async def run():
        with patch("bot.features.agent._handle_channel", AsyncMock(return_value=True)) as mock_handle, \
             patch("bot.features.agent._handle_thread_continuation", AsyncMock()) as mock_thread, \
             patch("bot.features.agent._maybe_ambient", AsyncMock()) as mock_ambient, \
             patch("bot.features.agent.fetch_slack_images", AsyncMock(return_value=[])), \
             patch("bot.features.agent.transcribe_slack_audio", AsyncMock(return_value=[])):
            base = {"channel": "C1", "channel_type": "channel", "user": "U1", "ts": "1.0"}
            await on_message({**base, "text": "hey nugget what's up"}, client)
            mock_handle.assert_called_once()
            assert mock_handle.call_args[1]["trigger"] == "named"
            mock_thread.assert_not_called()
            mock_ambient.assert_not_called()

            # A real @mention belongs to app_mention, not this path.
            await on_message({**base, "ts": "2.0", "text": "<@UBOT> hey"}, client)
            assert mock_handle.call_count == 1

            # If Boi is mentioned too, Boi owns it.
            await on_message({**base, "ts": "3.0", "text": "<@UBOI> nugget hey"}, client)
            assert mock_handle.call_count == 1

    try:
        asyncio.run(run())
    finally:
        runtime.settings, runtime.bot_user_id, runtime.user_id = old_settings, old_bot, old_user


def test_bot_mpim_mentions_and_thread_followups(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.db import Database
    from bot.features.agent import _handle_mpim, on_app_mention, on_message
    from bot.runtime import runtime

    db = Database(str(tmp_path / "mpim.db"))
    db.init()
    db.mark_thread("G1", "1.0", owner="bot")
    old_db, old_settings = runtime.db, runtime.settings
    old_bot, old_user = runtime.bot_user_id, runtime.user_id
    runtime.db = db
    runtime.settings = MagicMock()
    runtime.settings.ai_allowed.return_value = False  # MPIMs bypass AI_CHANNELS
    runtime.settings.slack_bot_token = "xoxb-test"
    runtime.bot_user_id = "UBOT"
    runtime.user_id = "UBOI"
    client = AsyncMock()

    async def run():
        event = {
            "channel": "G1",
            "channel_type": "mpim",
            "user": "U1",
            "text": "<@UBOT> what's up",
            "ts": "1.1",
        }
        with patch("bot.features.agent._handle_channel", AsyncMock(return_value=True)) as mock_handle, \
             patch("bot.features.agent.fetch_slack_images", AsyncMock(return_value=[])), \
             patch("bot.features.agent.transcribe_slack_audio", AsyncMock(return_value=[])):
            await on_app_mention(event, client)
        mock_handle.assert_called_once()
        assert mock_handle.call_args[1]["trigger"] == "mpim_mention"

        with patch("bot.features.agent._handle_mpim", AsyncMock()) as mock_mpim:
            await on_message({**event, "text": "just chatter"}, client)
        mock_mpim.assert_called_once()

        with patch("bot.features.agent._handle_thread_continuation", AsyncMock()) as mock_thread:
            await _handle_mpim({**event, "thread_ts": "1.0", "text": "follow up"}, client)
        mock_thread.assert_called_once()

        with patch("bot.features.agent._handle_thread_continuation", AsyncMock()) as mock_thread:
            await _handle_mpim({**event, "text": "no thread followup"}, client)
        mock_thread.assert_not_called()

    try:
        asyncio.run(run())
    finally:
        runtime.db, runtime.settings = old_db, old_settings
        runtime.bot_user_id, runtime.user_id = old_bot, old_user


def test_bot_dm_threads_stay_in_thread():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.features.agent import _handle_dm
    from bot.runtime import runtime

    old_agent, old_settings = runtime.agent, runtime.settings
    runtime.agent = MagicMock()
    runtime.agent.reply = AsyncMock(return_value="ok")
    runtime.settings = MagicMock()
    runtime.settings.owner_user_id = "U_OWNER"
    runtime.settings.ai_allow_admins = False
    runtime.settings.ai_enabled = True
    runtime.settings.slack_bot_token = "xoxb-test"
    client = AsyncMock()

    async def run():
        with patch("bot.features.agent.fetch_slack_images", AsyncMock(return_value=[])), \
             patch("bot.features.agent.transcribe_slack_audio", AsyncMock(return_value=[])), \
             patch("bot.features.agent.react", AsyncMock()), \
             patch("bot.features.agent.unreact", AsyncMock()), \
             patch("bot.features.agent.post", AsyncMock()) as mock_post:
            top_level = {
                "channel": "D1", "user": "U_OWNER", "text": "top", "ts": "10.0",
            }
            threaded = {
                "channel": "D1", "user": "U_OWNER", "text": "thread", "ts": "10.2", "thread_ts": "10.0",
            }
            await _handle_dm(top_level, client)
            await _handle_dm(threaded, client)

            replies = runtime.agent.reply.call_args_list
            assert replies[0][1]["thread_ts"] is None
            assert replies[1][1]["thread_ts"] == "10.0"

            posts = mock_post.call_args_list
            assert posts[0][1]["thread_ts"] is None
            assert posts[1][1]["thread_ts"] == "10.0"

    try:
        asyncio.run(run())
    finally:
        runtime.agent, runtime.settings = old_agent, old_settings


def test_thread_followup_ignores_plain_statements(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.ai.gate import GateDecision
    from bot.db import Database
    from bot.features.agent import _handle_thread_continuation
    from bot.runtime import runtime

    db = Database(str(tmp_path / "statement_thread.db"))
    db.init()
    db.mark_thread("C1", "1.0")
    db.add_message("ch:C1:1.0", "user", "U1", "i have 61 hours")
    runtime.db = db

    old_gate, old_settings = runtime.gate, runtime.settings
    runtime.gate = MagicMock()
    runtime.gate.decide = AsyncMock(return_value=GateDecision(reply=True, kind="help"))
    runtime.settings = MagicMock()
    runtime.settings.ai_allowed.return_value = True
    runtime.settings.ai_thread_cooldown = 0
    runtime.settings.bot_name = "Chicen Nugget Bot"
    client = AsyncMock()

    async def run():
        with patch("bot.features.agent._user_name", AsyncMock(return_value="maaren")), \
             patch("bot.features.agent._channel_name", AsyncMock(return_value="general")), \
             patch("bot.features.agent.fetch_slack_images", AsyncMock(return_value=[])), \
             patch("bot.features.agent.transcribe_slack_audio", AsyncMock(return_value=[])), \
             patch("bot.features.agent._handle_channel", AsyncMock(return_value=True)) as mock_handle:
            await _handle_thread_continuation(
                {
                    "channel": "C1",
                    "thread_ts": "1.0",
                    "user": "U1",
                    "text": "i have 61 hours and cant do shi with it",
                    "ts": "1.1",
                },
                client,
            )
            mock_handle.assert_not_called()

            await _handle_thread_continuation(
                {
                    "channel": "C1",
                    "thread_ts": "1.0",
                    "user": "U1",
                    "text": "can you check why my build fails",
                    "ts": "1.2",
                },
                client,
            )
            mock_handle.assert_called_once()

    try:
        asyncio.run(run())
    finally:
        runtime.gate, runtime.settings = old_gate, old_settings


def test_markdown_notes_memory(tmp_path):
    import asyncio
    import json
    from unittest.mock import AsyncMock, MagicMock

    from bot.ai.tools import Toolbox, ToolContext
    from bot.db import Database

    db = Database(str(tmp_path / "notes_test.db"))
    db.init()
    settings = MagicMock()
    settings.data_dir = tmp_path

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

    async def run():
        # 1. remember with topic
        res = await box.execute(
            "remember",
            {"text": "never serve cold fries", "topic": "rules", "description": "standing kitchen rules"},
            ctx,
        )
        assert "saved note #1 to notes/rules.md" in res
        rules_file = tmp_path / "notes" / "rules.md"
        assert rules_file.exists()
        content = rules_file.read_text(encoding="utf-8")
        assert "standing kitchen rules" in content
        assert "never serve cold fries" in content

        # 2. remember append
        res2 = await box.execute(
            "remember",
            {"text": "always season with salt", "topic": "rules"},
            ctx,
        )
        assert "saved note #2 to notes/rules.md" in res2
        content2 = rules_file.read_text(encoding="utf-8")
        assert "- never serve cold fries" in content2
        assert "- always season with salt" in content2

        # 3. recall specific topic
        read_topic = await box.execute("recall", {"topic": "rules"}, ctx)
        assert "content of notes/rules.md:" in read_topic
        assert "always season with salt" in read_topic

        # 4. recall with search query matching both markdown and sqlite
        search_res = await box.execute("recall", {"query": "season"}, ctx)
        parsed = json.loads(search_res)
        assert any("always season with salt" in m for m in parsed["markdown_matches"])
        assert any("always season with salt" in n["text"] for n in parsed["notes"])

        # 5. recall empty query returns index
        index_res = await box.execute("recall", {}, ctx)
        index_parsed = json.loads(index_res)
        assert any("notes/rules.md: standing kitchen rules" in line for line in index_parsed["notes_files"])

    asyncio.run(run())


def test_send_dm_resolves_bare_id_and_me():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bot.ai.tools import Toolbox, ToolContext

    settings = MagicMock()
    settings.owner_user_id = "U0OWNER01"
    box = Toolbox(settings)

    client = AsyncMock()
    client.users_info = AsyncMock(return_value={"user": {"id": "U0OWNER01", "name": "owner"}})
    open_resp = MagicMock()
    open_resp.get = lambda k, d=None: {"channel": {"id": "D0CGFPAS0SD"}}.get(k, d)
    client.conversations_open = AsyncMock(return_value=open_resp)
    client.chat_postMessage = AsyncMock(return_value=MagicMock(data={"ok": True, "ts": "123.456"}))

    ctx = ToolContext(
        db=MagicMock(),
        settings=settings,
        client=client,
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U0OWNER01",
        channel_id="C1",
        thread_ts=None,
        is_dm=False,
        is_owner=True,
        is_admin=True,
    )

    async def run():
        # Bare user id without needing users_list
        res1 = await box.execute("send_dm", {"user": "U0OWNER01", "text": "yo **test**"}, ctx)
        assert "sent a DM to <@U0OWNER01>" in res1
        client.users_list.assert_not_called()
        assert client.chat_postMessage.call_args[1]["channel"] == "D0CGFPAS0SD"
        assert client.chat_postMessage.call_args[1]["text"] == "yo *test*"

        # 'me' alias
        res2 = await box.execute("send_dm", {"user": "me", "text": "ping"}, ctx)
        assert "sent a DM to <@U0OWNER01>" in res2

    asyncio.run(run())


def test_send_dm_and_post_message_as_user():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bot.ai.tools import Toolbox, ToolContext
    from bot.slack_api import _display_cache

    settings = MagicMock()
    settings.owner_user_id = "U0OWNER01"
    box = Toolbox(settings)

    bot_client = AsyncMock()
    user_client = AsyncMock()
    owner_client = AsyncMock()

    async def fake_users_info(user):
        if user == "U0OWNER01":
            return {
                "user": {
                    "id": "U0OWNER01",
                    "name": "maaren",
                    "profile": {
                        "display_name": "Maaren",
                        "image_72": "https://example.com/owner.png",
                    },
                }
            }
        return {"user": {"id": user, "name": "target"}}

    bot_client.users_info = AsyncMock(side_effect=fake_users_info)
    bot_client.conversations_open = AsyncMock(return_value={"channel": {"id": "D111111111"}})
    bot_client.chat_postMessage = AsyncMock(return_value=MagicMock(data={"ok": True, "ts": "111.000"}))

    user_client.conversations_open = AsyncMock(return_value={"channel": {"id": "D222222222"}})
    user_client.chat_postMessage = AsyncMock(return_value=MagicMock(data={"ok": True, "ts": "222.000"}))

    owner_client.conversations_open = AsyncMock(return_value={"channel": {"id": "D333333333"}})
    owner_client.chat_postMessage = AsyncMock(return_value=MagicMock(data={"ok": True, "ts": "333.000"}))

    def make_ctx(*, native: bool) -> ToolContext:
        return ToolContext(
            db=MagicMock(),
            settings=settings,
            client=bot_client,
            user_client=user_client,
            bot_user_id="U_BOT",
            assistant_user_id="U_BOT",
            user_id="U0OWNER01",
            channel_id="C123456789",
            thread_ts=None,
            is_dm=True,
            is_owner=True,
            is_admin=True,
            bot_client=bot_client,
            owner_client=owner_client if native else None,
        )

    async def run():
        _display_cache.clear()
        ctx = make_ctx(native=False)

        # 1. send_dm default (as bot)
        res_dm_bot = await box.execute("send_dm", {"user": "U123456789", "text": "hello from bot"}, ctx)
        assert "sent a DM to <@U123456789> as the bot app." in res_dm_bot
        bot_client.chat_postMessage.assert_called_once()
        _, kwargs = bot_client.chat_postMessage.call_args
        assert "username" not in kwargs and "icon_url" not in kwargs
        user_client.chat_postMessage.assert_not_called()
        owner_client.chat_postMessage.assert_not_called()

        # 2. send_dm as_user=True without an owner token: bot app wears the
        #    owner's name + avatar via chat:write.customize.
        bot_client.chat_postMessage.reset_mock()
        res_dm_user = await box.execute(
            "send_dm", {"user": "U123456789", "text": "hello from owner", "as_user": True}, ctx
        )
        assert "your name and avatar" in res_dm_user
        bot_client.chat_postMessage.assert_called_once()
        _, kwargs_dm = bot_client.chat_postMessage.call_args
        assert kwargs_dm["username"] == "Maaren"
        assert kwargs_dm["icon_url"] == "https://example.com/owner.png"
        assert kwargs_dm["text"].endswith("_Sent by <@U_BOT>_")
        assert kwargs_dm["blocks"][-1]["type"] == "context"
        assert "Sent by <@U_BOT>" in kwargs_dm["blocks"][-1]["elements"][0]["text"]
        owner_client.chat_postMessage.assert_not_called()

        # 3. post_message default (as bot)
        bot_client.chat_postMessage.reset_mock()
        res_post_bot = await box.execute(
            "post_message", {"channel": "C123456789", "text": "bot announcement"}, ctx
        )
        assert "posted to <#C123456789>" in res_post_bot
        assert "as the bot app" in res_post_bot
        _, kwargs_bot_post = bot_client.chat_postMessage.call_args
        assert "username" not in kwargs_bot_post

        # 4. post_message as_user=True without an owner token (customized bot post)
        bot_client.chat_postMessage.reset_mock()
        res_post_user = await box.execute(
            "post_message", {"channel": "C123456789", "text": "owner announcement", "as_user": True}, ctx
        )
        assert "posted to <#C123456789>" in res_post_user
        assert "your name and avatar" in res_post_user
        _, kwargs_post = bot_client.chat_postMessage.call_args
        assert kwargs_post["username"] == "Maaren"
        assert "_Sent by <@U_BOT>_" in kwargs_post["text"]
        assert kwargs_post["thread_ts"] is None

        # 5. with the owner's own token, post natively through their profile
        bot_client.chat_postMessage.reset_mock()
        native_ctx = make_ctx(native=True)
        res_native = await box.execute(
            "post_message", {"channel": "C123456789", "text": "native announcement", "as_user": True},
            native_ctx,
        )
        assert "native post" in res_native
        owner_client.chat_postMessage.assert_called_once()
        _, kwargs_native = owner_client.chat_postMessage.call_args
        assert "username" not in kwargs_native and "icon_url" not in kwargs_native
        assert "_Sent by <@U_BOT>_" in kwargs_native["text"]
        bot_client.chat_postMessage.assert_not_called()
        user_client.chat_postMessage.assert_not_called()

        # 6. profile lookup failure is a clear error, not a silent fallback
        bot_client.chat_postMessage.reset_mock()
        _display_cache.clear()
        bot_client.users_info = AsyncMock(side_effect=RuntimeError("missing_scope"))
        err = await box.execute(
            "post_message", {"channel": "C123456789", "text": "hi", "as_user": True}, ctx
        )
        assert "chat:write.customize" in err
        bot_client.chat_postMessage.assert_not_called()
        user_client.chat_postMessage.assert_not_called()

    asyncio.run(run())


def test_slack_status_and_dnd_tools():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bot.ai.tools import Toolbox, ToolContext

    settings = MagicMock()
    settings.owner_user_id = "U_OWNER"
    box = Toolbox(settings)

    user_client = AsyncMock()
    user_client.users_profile_get = AsyncMock(
        return_value={"profile": {"status_text": "coding", "status_emoji": ":technologist:", "status_expiration": 12345}}
    )
    user_client.users_profile_set = AsyncMock(return_value={"ok": True})
    user_client.dnd_info = AsyncMock(
        return_value={"dnd_enabled": True, "snooze_enabled": True, "snooze_endtime": 1234567, "snooze_remaining": 60}
    )
    user_client.dnd_setSnooze = AsyncMock(return_value={"ok": True, "snooze_endtime": 9999999})
    user_client.dnd_endSnooze = AsyncMock(return_value={"ok": True})

    ctx = ToolContext(
        db=MagicMock(),
        settings=settings,
        client=AsyncMock(),
        user_client=user_client,
        bot_user_id="U_BOT",
        user_id="U_OWNER",
        channel_id="D1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )

    async def run():
        # status get
        res_get = await box.execute("slack_status", {"action": "get"}, ctx)
        assert "coding" in res_get
        assert ":technologist:" in res_get

        # status set
        res_set = await box.execute("slack_status", {"action": "set", "text": "away", "emoji": ":palm_tree:", "minutes": 30}, ctx)
        assert "updated Slack status: :palm_tree: away" in res_set
        user_client.users_profile_set.assert_called()

        # status clear
        res_clear = await box.execute("slack_status", {"action": "clear"}, ctx)
        assert "cleared Slack status." in res_clear

        # dnd info
        res_dnd_info = await box.execute("slack_dnd", {"action": "info"}, ctx)
        assert "snooze_enabled" in res_dnd_info

        # dnd snooze
        res_snooze = await box.execute("slack_dnd", {"action": "snooze", "minutes": 45}, ctx)
        assert "snoozed notifications for 45 minute(s)" in res_snooze
        user_client.dnd_setSnooze.assert_called_with(num_minutes=45)

        # dnd end
        res_end = await box.execute("slack_dnd", {"action": "end"}, ctx)
        assert "ended DND snooze." in res_end
        user_client.dnd_endSnooze.assert_called_once()

    asyncio.run(run())


def test_agent_active_identity_injection(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bot.ai.agent import Agent
    from bot.db import Database

    db = Database(str(tmp_path / "ident_test.db"))
    db.init()

    settings = MagicMock()
    settings.bot_name = "Chicen Nugget"
    settings.ai_enabled = True
    settings.ai_api_key = "test-key"
    settings.ai_base_url = "https://ai.example.com"
    settings.ai_model = "test-model"
    settings.ai_fallback_model = ""
    settings.ai_assistant_model = "assistant-model"
    settings.ai_assistant_fallback_model = ""
    settings.imagine_model = "test-img"
    settings.ai_gate_model = "test-gate"
    settings.ai_max_context_chars = 1000
    settings.ai_history_limit = 5
    settings.owner_user_id = "U_OWNER"
    settings.repo_url = "https://github.com/repo"
    settings.is_admin.return_value = False

    bot_client = AsyncMock()
    bot_client.conversations_info = AsyncMock(return_value={"channel": {"name": "general", "is_private": False}})
    user_client = AsyncMock()
    user_client.conversations_info = AsyncMock(return_value={"channel": {"name": "general", "is_private": False}})

    agent = Agent(
        settings,
        db,
        client=bot_client,
        user_client=user_client,
        bot_user_id="U0BOT0001",
        user_id="U0BOI0001",
    )

    captured_system_messages = []

    async def fake_chat(messages, *args, **kwargs):
        for m in messages:
            if m.get("role") == "system":
                captured_system_messages.append(m["content"])
        choice = MagicMock()
        choice.message.content = "pong"
        choice.message.tool_calls = []
        res = MagicMock()
        res.choices = [choice]
        return res

    agent._chat = fake_chat

    async def run():
        # Call via bot_client
        await agent.reply(
            conversation="ch:C1:1.0",
            user_id="U_OWNER",
            text="hello bot",
            channel_id="C1",
            thread_ts="1.0",
            is_dm=False,
            client=bot_client,
        )
        assert "Chicen Nugget Bot (workspace bot app)" in captured_system_messages[-1]
        assert "U0BOT0001" in captured_system_messages[-1]

        # Call via user_client (Boi)
        await agent.reply(
            conversation="user_dm:U_OWNER",
            user_id="U_OWNER",
            text="hello boi",
            channel_id="D1",
            thread_ts=None,
            is_dm=True,
            client=user_client,
        )
        assert "Chicen Nugget Boi (personal assistant user account)" in captured_system_messages[-1]
        assert "U0BOI0001" in captured_system_messages[-1]

    asyncio.run(run())


def test_agent_model_selection_per_identity(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from bot.ai.agent import Agent
    from bot.db import Database

    db = Database(str(tmp_path / "models_test.db"))
    db.init()

    settings = MagicMock()
    settings.ai_api_key = "test-key"
    settings.ai_base_url = "https://ai.example.com"
    settings.ai_model = "bot-model"
    settings.ai_fallback_model = "bot-fallback"
    settings.ai_assistant_model = "boi-model"
    settings.ai_assistant_fallback_model = "boi-fallback"
    settings.is_admin.return_value = False

    agent = Agent(settings, db, client=AsyncMock(), user_client=None, bot_user_id="U_BOT")

    assert agent._models_for(False) == ["bot-model", "bot-fallback"]
    assert agent._models_for(True) == [
        "boi-model",
        "boi-fallback",
        "bot-model",
        "bot-fallback",
    ]

    # Boi can share the bot's model without duplicate attempts.
    settings.ai_assistant_model = "bot-model"
    settings.ai_assistant_fallback_model = ""
    assert agent._models_for(True) == ["bot-model", "bot-fallback"]


def test_all_users_cache_and_ratelimit_fallback():
    import asyncio
    from unittest.mock import AsyncMock

    from bot.slack_api import _users_cache, all_users

    client = AsyncMock()
    client.token = "xoxb-cache-test"
    _users_cache.pop("xoxb-cache-test", None)

    client.users_list = AsyncMock(
        return_value={"members": [{"id": "U1", "name": "alice"}], "response_metadata": {}}
    )

    async def run():
        first = await all_users(client)
        assert first == [{"id": "U1", "name": "alice"}]
        assert client.users_list.call_count == 1

        # Second call within TTL uses cache
        second = await all_users(client)
        assert second == first
        assert client.users_list.call_count == 1

        # Simulate expired cache + ratelimited API -> falls back to stale cache
        _users_cache["xoxb-cache-test"] = (0.0, first)
        client.users_list = AsyncMock(side_effect=RuntimeError("ratelimited"))
        third = await all_users(client)
        assert third == first

    asyncio.run(run())


def test_forget_tool_and_memory_injection(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bot.ai.agent import Agent
    from bot.ai.tools import Toolbox, ToolContext, get_user_memory_prompt
    from bot.db import Database

    db = Database(str(tmp_path / "memory_test.db"))
    db.init()
    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.owner_user_id = "U_OWNER"
    settings.is_admin = lambda u: u == "U_ADMIN"
    settings.bot_name = "nugget"
    settings.ai_enabled = True
    settings.ai_api_key = "key"
    settings.ai_base_url = "https://ai.example.com"
    settings.ai_model = "test-model"
    settings.ai_fallback_model = ""
    settings.ai_assistant_model = "assistant-model"
    settings.ai_assistant_fallback_model = ""
    settings.imagine_model = "test-img"
    settings.ai_gate_model = "test-gate"
    settings.ai_max_context_chars = 1000
    settings.ai_history_limit = 5
    settings.repo_url = "https://github.com/repo"

    box = Toolbox(settings)

    ctx_owner = ToolContext(
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

    async def run():
        # Guest gets "none" memory
        assert get_user_memory_prompt(db, settings, "U_GUEST") == "none"

        # Owner starts with "none stored yet"
        assert get_user_memory_prompt(db, settings, "U_OWNER") == "none stored yet"

        # 1. Store notes via remember
        await box.execute("remember", {"text": "likes iced coffee", "topic": "prefs"}, ctx_owner)
        await box.execute("remember", {"text": "working on slackbot", "topic": "projects"}, ctx_owner)

        mem = get_user_memory_prompt(db, settings, "U_OWNER")
        assert "prefs" in mem
        assert "projects" in mem
        assert "likes iced coffee" in mem
        assert "working on slackbot" in mem

        # 2. Forget by query
        forget_query = await box.execute("forget", {"query": "iced coffee"}, ctx_owner)
        assert "database notes matching 'iced coffee'" in forget_query
        assert "likes iced coffee" not in get_user_memory_prompt(db, settings, "U_OWNER")

        # 3. Forget by topic
        forget_topic = await box.execute("forget", {"topic": "projects"}, ctx_owner)
        assert "projects.md" in forget_topic
        assert "projects" not in get_user_memory_prompt(db, settings, "U_OWNER")

        # 4. Forget by note_id
        await box.execute("remember", {"text": "temporary rule", "topic": "rules"}, ctx_owner)
        latest_notes = db.user_notes("U_OWNER")
        target_id = latest_notes[0]["id"]
        forget_id = await box.execute("forget", {"note_id": target_id}, ctx_owner)
        assert f"note #{target_id}" in forget_id
        assert db.get_note(target_id) is None

        # 5. Guest denied forget
        guest_forget = await box.execute("forget", {"query": "anything"}, ctx_guest)
        assert "only the owner/admin" in guest_forget

        # 6. Verify Agent.reply memory injection
        client = AsyncMock()
        client.conversations_info = AsyncMock(return_value={"channel": {"name": "general", "is_private": False}})
        agent = Agent(settings, db, client=client, user_client=None, bot_user_id="U_BOT")
        captured_prompts = []

        async def fake_chat(messages, *args, **kwargs):
            for m in messages:
                if m.get("role") == "system":
                    captured_prompts.append(m["content"])
            res = MagicMock()
            choice = MagicMock()
            choice.message.content = "ok"
            choice.message.tool_calls = []
            res.choices = [choice]
            return res

        agent._chat = fake_chat

        # Owner turn gets memory prompt
        db.add_note("U_OWNER", "durable test memory")
        await agent.reply(
            conversation="dm:U_OWNER",
            user_id="U_OWNER",
            text="hello",
            channel_id="D1",
            thread_ts=None,
            is_dm=True,
        )
        assert "durable test memory" in captured_prompts[-1]

        # Guest turn in public channel gets "none" (no leak)
        await agent.reply(
            conversation="ch:C1:1.0",
            user_id="U_GUEST",
            text="hello",
            channel_id="C1",
            thread_ts="1.0",
            is_dm=False,
        )
        assert "durable test memory" not in captured_prompts[-1]
        assert "none\n- long-term memory" in captured_prompts[-1]

    asyncio.run(run())


def test_bot_config_channel_recaps_tool(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bot.ai.tools import Toolbox, ToolContext
    from bot.db import Database

    db = Database(str(tmp_path / "cfg_tool_test.db"))
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
        channel_id="C_MAIN",
        thread_ts=None,
        is_dm=False,
        is_owner=True,
        is_admin=True,
    )

    async def run():
        # Disable recaps for specific channel
        res = await box.execute("bot_config", {"action": "set", "key": "recaps", "channel": "C_PERSONAL", "value": "off"}, ctx)
        assert "daily recaps disabled for <#C_PERSONAL>" in res
        assert db.kv_get("config:recaps:C_PERSONAL") == "false"

        # Query channel recap
        res_get = await box.execute("bot_config", {"action": "get", "key": "recaps", "channel": "C_PERSONAL"}, ctx)
        assert "`false`" in res_get

        # Reset channel recap
        res_rst = await box.execute("bot_config", {"action": "reset", "key": "recaps", "channel": "C_PERSONAL"}, ctx)
        assert "reset to default" in res_rst
        assert db.kv_get("config:recaps:C_PERSONAL") is None

        # Set recap_channel_id
        res_cid = await box.execute("bot_config", {"action": "set", "key": "recap_channel", "value": "<#C_RECAP_NEW>"}, ctx)
        assert "C_RECAP_NEW" in res_cid
        assert db.kv_get("config:recap_channel_id") == "C_RECAP_NEW"

    asyncio.run(run())


def test_personal_operator_tools(tmp_path):
    import asyncio
    import json
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx

    from bot.ai.tools import Toolbox, ToolContext
    from bot.db import Database

    db = Database(str(tmp_path / "operator_test.db"))
    db.init()
    settings = MagicMock()
    settings.owner_user_id = "U_OWNER"
    settings.google_access_token = None
    settings.google_refresh_token = None
    settings.mac_bridge_url = None
    settings.mac_bridge_token = None
    box = Toolbox(settings)

    bot_client = AsyncMock()
    user_client = AsyncMock()
    bot_client.chat_postMessage = AsyncMock(return_value=MagicMock(data={"ok": True, "ts": "123.456"}))
    user_client.reactions_add = AsyncMock(return_value={"ok": True})
    user_client.reactions_remove = AsyncMock(return_value={"ok": True})
    user_client.search_messages = AsyncMock(
        return_value={"messages": {"matches": [{"text": "hey <@U_OWNER>", "user": "U2", "ts": "123.000"}]}}
    )

    ctx_owner = ToolContext(
        db=db,
        settings=settings,
        client=bot_client,
        user_client=user_client,
        bot_user_id="U_BOT",
        user_id="U_OWNER",
        channel_id="C_MAIN",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
        bot_client=bot_client,
    )
    ctx_guest = ToolContext(
        db=db,
        settings=settings,
        client=bot_client,
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_GUEST",
        channel_id="C_MAIN",
        thread_ts=None,
        is_dm=False,
        is_owner=False,
        is_admin=False,
        bot_client=bot_client,
    )

    async def run():
        # 1. post_message with thread_ts
        post_res = await box.execute(
            "post_message",
            {"channel": "C_MAIN", "text": "thread reply", "thread_ts": "100.200"},
            ctx_owner,
        )
        assert "thread 100.200" in post_res
        bot_client.chat_postMessage.assert_called_with(
            channel="C_MAIN",
            text="thread reply",
            blocks=None,
            thread_ts="100.200",
            unfurl_links=False,
            unfurl_media=False,
        )

        # 2. slack_react
        guest_react = await box.execute(
            "slack_react",
            {"channel": "C_MAIN", "timestamp": "123.456", "emoji": "eyes"},
            ctx_guest,
        )
        assert "only the owner/admin" in guest_react

        react_add = await box.execute(
            "slack_react",
            {"action": "add", "channel": "C_MAIN", "timestamp": "123.456", "emoji": ":eyes:", "as_user": True},
            ctx_owner,
        )
        assert "added :eyes: to 123.456 in <#C_MAIN> as Boi" in react_add
        user_client.reactions_add.assert_called_with(channel="C_MAIN", timestamp="123.456", name="eyes")

        react_rm = await box.execute(
            "slack_react",
            {"action": "remove", "channel": "C_MAIN", "timestamp": "123.456", "emoji": "eyes", "as_user": True},
            ctx_owner,
        )
        assert "removed :eyes: from 123.456 in <#C_MAIN> as Boi" in react_rm
        user_client.reactions_remove.assert_called_with(channel="C_MAIN", timestamp="123.456", name="eyes")

        # 3. slack_activity
        act_res = await box.execute("slack_activity", {}, ctx_owner)
        assert "hey <@U_OWNER>" in act_res

        # 4. google unconfigured
        cal_err = await box.execute("google_calendar", {"action": "list"}, ctx_owner)
        assert "Google credentials not configured" in cal_err
        gmail_err = await box.execute("gmail", {"action": "list"}, ctx_owner)
        assert "Google credentials not configured" in gmail_err
        drive_err = await box.execute("google_drive", {"action": "list"}, ctx_owner)
        assert "Google credentials not configured" in drive_err

        # 5. google mock calls with token
        db.kv_set("config:google_access_token", "ya29.test_token_secret_123456")

        async def fake_google_get(self, url, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            url_str = str(url)
            if "calendar/v3/calendars/primary/events" in url_str:
                resp.json.return_value = {
                    "items": [{"id": "ev1", "summary": "Sync", "start": {"dateTime": "2026-11-12T10:00:00Z"}}]
                }
            elif "gmail/v1/users/me/messages/m1" in url_str:
                resp.json.return_value = {
                    "id": "m1",
                    "payload": {"headers": [{"name": "Subject", "value": "Test Subject"}, {"name": "From", "value": "alice@example.com"}]},
                    "snippet": "Test body snippet",
                }
            elif "gmail/v1/users/me/messages" in url_str:
                resp.json.return_value = {"messages": [{"id": "m1"}]}
            elif "drive/v3/files" in url_str and "export" not in url_str:
                resp.json.return_value = {"files": [{"id": "f1", "name": "Notes.txt", "mimeType": "text/plain"}]}
            return resp

        with patch.object(httpx.AsyncClient, "get", new=fake_google_get):
            cal_res = await box.execute("google_calendar", {"action": "list"}, ctx_owner)
            assert "Sync" in cal_res

            gmail_res = await box.execute("gmail", {"action": "list"}, ctx_owner)
            assert "Test Subject" in gmail_res

            drive_res = await box.execute("google_drive", {"action": "list"}, ctx_owner)
            assert "Notes.txt" in drive_res

        # 6. config masking for google tokens
        cfg_get = await box.execute("bot_config", {"action": "get", "key": "google_access_token"}, ctx_owner)
        assert "ya29...3456" in cfg_get
        assert "secret" not in cfg_get

        # 7. mac_control guest rejected
        guest_mac = await box.execute("mac_control", {"action": "status"}, ctx_guest)
        assert "only the owner/admin" in guest_mac

        # 8. mac_control mock local / bridge
        async def fake_subproc(*cmd, stdin=None, stdout=None, stderr=None):
            proc = MagicMock()
            proc.returncode = 0
            if cmd[0] == "pbpaste":
                proc.communicate = AsyncMock(return_value=(b"clipboard content", b""))
            elif cmd[0] == "osascript" and "frontmost" in cmd[2]:
                proc.communicate = AsyncMock(return_value=(b"Slack", b""))
            elif cmd[0] == "pmset":
                proc.communicate = AsyncMock(return_value=(b"Now drawing from 'Battery Power' - 85%", b""))
            elif cmd[0] == "uptime":
                proc.communicate = AsyncMock(return_value=(b"up 5 days", b""))
            else:
                proc.communicate = AsyncMock(return_value=(b"ok", b""))
            return proc

        with patch("bot.ai.tools.asyncio.create_subprocess_exec", side_effect=fake_subproc):
            mac_front = await box.execute("mac_control", {"action": "frontmost"}, ctx_owner)
            assert mac_front == "Slack"

            mac_clip = await box.execute("mac_control", {"action": "clipboard"}, ctx_owner)
            assert mac_clip == "clipboard content"

            mac_status = await box.execute("mac_control", {"action": "status"}, ctx_owner)
            status_data = json.loads(mac_status)
            assert "Battery Power" in status_data["battery"]

            mac_vol = await box.execute("mac_control", {"action": "volume", "level": 65}, ctx_owner)
            assert "65%" in mac_vol

        # 9. remind tool with standalone time and 12am
        settings.max_reminders = 50
        rem1 = await box.execute("remind", {"when": "tomorrow at 12am", "text": "respond to the g2g email"}, ctx_owner)
        assert "reminder *#" in rem1

        rem2 = await box.execute("remind", {"when": "tomorrow 00:00", "text": "check email"}, ctx_owner)
        assert "reminder *#" in rem2

        rem3 = await box.execute("remind", {"when": "in 85m", "text": "ping"}, ctx_owner)
        assert "reminder *#" in rem3

        # 10. bot_config list shows per-channel recaps
        await box.execute("bot_config", {"action": "set", "key": "recaps", "channel": "C999", "value": "off"}, ctx_owner)
        cfg_list = await box.execute("bot_config", {"action": "list"}, ctx_owner)
        assert "recaps (<#C999>)*: `false`" in cfg_list

    asyncio.run(run())


def test_slack_app_manifest_scopes():
    import json
    from pathlib import Path

    manifest_path = Path(__file__).resolve().parents[1] / "slack-app-manifest.json"
    data = json.loads(manifest_path.read_text())
    user_scopes = data["oauth_config"]["scopes"]["user"]
    bot_scopes = data["oauth_config"]["scopes"]["bot"]

    for required in ["chat:write", "files:read", "reactions:write"]:
        assert required in user_scopes
    for required in ["chat:write", "chat:write.customize", "files:read", "reactions:write"]:
        assert required in bot_scopes


def test_transcribe_slack_audio():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx

    from bot.slack_api import _is_audio_file, transcribe_slack_audio

    # Test _is_audio_file classification
    assert _is_audio_file({"mimetype": "audio/webm", "filetype": "webm"})
    assert _is_audio_file({"subtype": "slack_audio", "filetype": "webm"})
    assert _is_audio_file({"audio_wave_samples": [1, 2, 3]})
    assert _is_audio_file({"filetype": "m4a", "mimetype": "audio/mp4"})
    assert not _is_audio_file({"filetype": "png", "mimetype": "image/png"})
    assert not _is_audio_file({"filetype": "webm", "mimetype": "video/webm"})

    settings = MagicMock()
    settings.ai_api_key = "test-api-key"
    settings.ai_base_url = "https://ai.example.com/v1"
    settings.ai_model = "test-model"

    async def run():
        # Case 1: File already has Slack transcription text
        event_with_tr = {
            "files": [
                {
                    "mimetype": "audio/webm",
                    "filetype": "webm",
                    "transcription": {"text": "hello from slack native transcription"},
                }
            ]
        }
        res_native = await transcribe_slack_audio(event_with_tr, "xoxb-token", settings)
        assert res_native == ["hello from slack native transcription"]

        # Case 2: File needs whisper transcription
        event_raw_audio = {
            "files": [
                {
                    "name": "voice.webm",
                    "mimetype": "audio/webm",
                    "filetype": "webm",
                    "url_private_download": "https://slack.com/files/voice.webm",
                    "size": 1024,
                }
            ]
        }

        async def fake_audio_http_get(self, url, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.content = b"RIFFfakeaudiobytes"
            return resp

        async def fake_audio_http_post(self, url, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {"text": "transcribed voice note message"}
            return resp

        with (
            patch.object(httpx.AsyncClient, "get", new=fake_audio_http_get),
            patch.object(httpx.AsyncClient, "post", new=fake_audio_http_post),
        ):
            res_whisper = await transcribe_slack_audio(event_raw_audio, "xoxb-token", settings)
            assert res_whisper == ["transcribed voice note message"]

    asyncio.run(run())


def test_github_info_runs():
    import asyncio
    import json
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx

    from bot.ai.tools import Toolbox, ToolContext

    settings = MagicMock()
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

    async def run():
        async def fake_gh_get(self, url, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "workflow_runs": [
                    {
                        "id": 123456,
                        "name": "deploy",
                        "status": "completed",
                        "conclusion": "success",
                        "head_branch": "main",
                        "event": "push",
                        "created_at": "2026-11-12T10:00:00Z",
                        "html_url": "https://github.com/owner/repo/actions/runs/123456",
                    }
                ]
            }
            return resp

        with patch.object(httpx.AsyncClient, "get", new=fake_gh_get):
            res = await box.execute("github_info", {"action": "runs", "target": "owner/repo"}, ctx)
            data = json.loads(res)
            assert len(data) == 1
            assert data[0]["name"] == "deploy"
            assert data[0]["conclusion"] == "success"

    asyncio.run(run())


def test_cmd_accounts():
    import asyncio
    from unittest.mock import AsyncMock

    from bot.features.commands import cmd_accounts

    async def run():
        ack = AsyncMock()
        client = AsyncMock()
        command = {"channel_id": "C123", "user_id": "U123"}
        await cmd_accounts(ack, command, client)
        ack.assert_called_once()
        client.chat_postEphemeral.assert_called_once()
        _, kwargs = client.chat_postEphemeral.call_args
        assert "Bot app" in kwargs["text"]
        assert "Boi" in kwargs["text"]
        assert "Jev" in kwargs["text"]

    asyncio.run(run())


def test_unsubscribe_gag():
    import asyncio
    from unittest.mock import AsyncMock, patch

    from bot.features.agent import _last_unsubscribe, on_message
    from bot.runtime import runtime

    old_bot_user_id = runtime.bot_user_id
    runtime.bot_user_id = "UBOT"
    _last_unsubscribe.clear()
    client = AsyncMock()

    base = {
        "channel": "C_THREAD",
        "channel_type": "channel",
        "thread_ts": "100.000",
        "ts": "101.000",
        "user": "U_ALICE",
    }

    async def run():
        with patch("bot.features.agent.post", AsyncMock()) as mock_post, \
             patch("bot.features.agent._handle_thread_continuation", AsyncMock()) as mock_thread, \
             patch("bot.features.agent._maybe_ambient", AsyncMock()):
            await on_message({**base, "text": "UNSUBSCRIBE"}, client)
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert args[1] == "C_THREAD"
            assert "<@U_ALICE> RESUBSCRIBE" in args[2]
            assert "(use \"turn off notifications for replies\" instead)" in args[2]
            assert "See how" in args[2]
            assert kwargs["thread_ts"] == "100.000"
            mock_thread.assert_not_called()

            # Cooldown keeps a spammer from turning it into a loop.
            await on_message({**base, "text": "unsubscribe"}, client)
            assert mock_post.call_count == 1

            # Not a thread reply, or not a bare unsubscribe, is left to the AI.
            await on_message({**base, "ts": "100.000", "text": "unsubscribe"}, client)
            await on_message({**base, "ts": "102.000", "text": "unsubscribe from this"}, client)
            assert mock_post.call_count == 1

            # Bot messages never trigger it.
            await on_message({**base, "text": "unsubscribe", "bot_id": "B1"}, client)
            assert mock_post.call_count == 1

    try:
        asyncio.run(run())
    finally:
        runtime.bot_user_id = old_bot_user_id
        _last_unsubscribe.clear()


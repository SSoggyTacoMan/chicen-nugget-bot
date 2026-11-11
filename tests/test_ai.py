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
    assert "inspect_code" in names
    assert "clone_repo" in names


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
    settings.owner_user_id = "U091GMQ14KV"
    box = Toolbox(settings)

    client = AsyncMock()
    client.users_info = AsyncMock(return_value={"user": {"id": "U091GMQ14KV", "name": "maaren"}})
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
        user_id="U091GMQ14KV",
        channel_id="C1",
        thread_ts=None,
        is_dm=False,
        is_owner=True,
        is_admin=True,
    )

    async def run():
        # Bare user id without needing users_list
        res1 = await box.execute("send_dm", {"user": "U091GMQ14KV", "text": "yo **test**"}, ctx)
        assert "sent a DM to <@U091GMQ14KV>" in res1
        client.users_list.assert_not_called()
        assert client.chat_postMessage.call_args[1]["channel"] == "D0CGFPAS0SD"
        assert client.chat_postMessage.call_args[1]["text"] == "yo *test*"

        # 'me' alias
        res2 = await box.execute("send_dm", {"user": "me", "text": "ping"}, ctx)
        assert "sent a DM to <@U091GMQ14KV>" in res2

    asyncio.run(run())


def test_send_dm_and_post_message_as_user():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bot.ai.tools import Toolbox, ToolContext

    settings = MagicMock()
    settings.owner_user_id = "U091GMQ14KV"
    box = Toolbox(settings)

    bot_client = AsyncMock()
    user_client = AsyncMock()

    bot_client.users_info = AsyncMock(return_value={"user": {"id": "U123456789", "name": "target"}})
    bot_client.conversations_open = AsyncMock(return_value={"channel": {"id": "D111111111"}})
    bot_client.chat_postMessage = AsyncMock(return_value=MagicMock(data={"ok": True, "ts": "111.000"}))

    user_client.conversations_open = AsyncMock(return_value={"channel": {"id": "D222222222"}})
    user_client.chat_postMessage = AsyncMock(return_value=MagicMock(data={"ok": True, "ts": "222.000"}))

    ctx = ToolContext(
        db=MagicMock(),
        settings=settings,
        client=bot_client,
        user_client=user_client,
        bot_user_id="U_BOT",
        user_id="U091GMQ14KV",
        channel_id="C123456789",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
        bot_client=bot_client,
    )

    async def run():
        # 1. send_dm default (as bot)
        res_dm_bot = await box.execute("send_dm", {"user": "U123456789", "text": "hello from bot"}, ctx)
        assert "sent a DM to <@U123456789> as Bot app." in res_dm_bot
        bot_client.chat_postMessage.assert_called_once()
        user_client.chat_postMessage.assert_not_called()

        # 2. send_dm as_user=True (as user assistant)
        res_dm_user = await box.execute(
            "send_dm", {"user": "U123456789", "text": "hello from boi", "as_user": True}, ctx
        )
        assert "sent a DM to <@U123456789> as Boi (user account)." in res_dm_user
        user_client.chat_postMessage.assert_called_once()

        # 3. post_message default (as bot)
        res_post_bot = await box.execute(
            "post_message", {"channel": "C123456789", "text": "bot announcement"}, ctx
        )
        assert "posted to <#C123456789>" in res_post_bot
        assert "as Bot app" in res_post_bot

        # 4. post_message as_user=True (as user assistant)
        res_post_user = await box.execute(
            "post_message", {"channel": "C123456789", "text": "boi announcement", "as_user": True}, ctx
        )
        assert "posted to <#C123456789>" in res_post_user
        assert "as Boi (user account)" in res_post_user

        # 5. Missing user_client error when as_user=True
        ctx_no_user = ToolContext(
            db=MagicMock(),
            settings=settings,
            client=bot_client,
            user_client=None,
            bot_user_id="U_BOT",
            user_id="U091GMQ14KV",
            channel_id="C123456789",
            thread_ts=None,
            is_dm=True,
            is_owner=True,
            is_admin=True,
            bot_client=bot_client,
        )
        err = await box.execute("post_message", {"channel": "C123456789", "text": "hi", "as_user": True}, ctx_no_user)
        assert "user client is not configured" in err

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
        bot_user_id="U0CGC8SJ8R0",
        user_id="U0CGW7W9FLL",
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
        assert "U0CGC8SJ8R0" in captured_system_messages[-1]

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
        assert "U0CGW7W9FLL" in captured_system_messages[-1]

    asyncio.run(run())


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

    asyncio.run(run())




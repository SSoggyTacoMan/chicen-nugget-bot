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





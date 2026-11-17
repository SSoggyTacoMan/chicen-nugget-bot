from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from bot.ai.thread_summary import (
    ThreadDigest,
    fetch_thread,
    format_digest,
    render_lines,
    summarize_thread,
)
from bot.runtime import runtime


class FakeSlack:
    def __init__(self, pages: list[dict], permalink: str | None = None) -> None:
        self.pages = pages
        self.permalink = permalink
        self.calls: list[dict] = []

    async def conversations_replies(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls) - 1
        return self.pages[min(index, len(self.pages) - 1)]

    async def chat_getPermalink(self, channel: str, message_ts: str):
        return {"ok": True, "permalink": self.permalink}


def page(messages: list[dict], *, more: bool, cursor: str = "") -> dict:
    return {
        "ok": True,
        "messages": messages,
        "has_more": more,
        "response_metadata": {"next_cursor": cursor},
    }


def msg(ts: str, user: str, text: str) -> dict:
    return {"ts": ts, "user": user, "text": text}


def test_fetch_thread_walks_every_page():
    client = FakeSlack(
        [
            page([msg("1.0", "U1", "first"), msg("1.1", "U2", "second")], more=True, cursor="c1"),
            page([msg("1.2", "U1", "third")], more=False),
        ]
    )
    messages, truncated = asyncio.run(fetch_thread(client, "C1", "1.0"))
    assert [m["text"] for m in messages] == ["first", "second", "third"]
    assert truncated is False
    assert client.calls[0] == {"channel": "C1", "ts": "1.0", "limit": 200}
    assert client.calls[1]["cursor"] == "c1"


def test_fetch_thread_stops_at_the_cap():
    pages = [
        page([msg("1.0", "U1", "one"), msg("1.1", "U2", "two")], more=True, cursor="c1"),
        page([msg("1.2", "U1", "three")], more=False),
    ]
    client = FakeSlack(pages)
    messages, truncated = asyncio.run(fetch_thread(client, "C1", "1.0", max_messages=2))
    assert [m["text"] for m in messages] == ["two", "three"]
    assert truncated is True
    assert len(client.calls) == 2


def test_render_lines_drops_joins_and_keeps_quotes():
    messages = [
        msg("1.0", "U1", "root"),
        {"ts": "1.1", "subtype": "channel_join", "text": "joined"},
        {
            "ts": "1.2",
            "user": "U2",
            "text": "explain this",
            "attachments": [{"is_share": True, "author_name": "pj", "text": "the original"}],
        },
    ]
    lines, people, first, last = render_lines(messages)
    assert len(lines) == 2
    assert people == 2
    assert "the original" in lines[1]
    assert first == "1.0" and last == "1.2"


def test_format_digest_reports_stats_and_link():
    digest = ThreadDigest(
        channel="C1",
        ts="1.0",
        messages=1204,
        participants=63,
        first_ts="1.0",
        last_ts="4000.0",
        truncated=True,
        transcript="body",
        permalink="https://example.slack.com/archives/C1/p1",
    )
    text = format_digest(digest)
    assert "1,204 messages" in text
    assert "63 people" in text
    assert "truncated" in text
    assert "https://example.slack.com/archives/C1/p1" in text
    assert "body" in text


def test_summarize_thread_compresses_long_threads():
    old_agent, old_settings = runtime.agent, runtime.settings
    agent = MagicMock()
    agent.complete = AsyncMock(return_value="slice notes")
    runtime.agent = agent
    runtime.settings = MagicMock()
    runtime.settings.ai_enabled = True
    runtime.settings.thread_summary_chunk_chars = 3000

    lines = [msg(f"1.{i}", f"U{i % 3}", "x" * 180) for i in range(80)]
    client = FakeSlack(
        [page(lines, more=False)],
        permalink="https://example.slack.com/archives/C1/p1",
    )

    async def run():
        return await summarize_thread(client, "C1", "1.0", user_id="U1")

    try:
        digest = asyncio.run(run())
    finally:
        runtime.agent, runtime.settings = old_agent, old_settings

    assert digest.summary
    assert digest.transcript
    assert digest.permalink.endswith("/p1")
    # one call per slice plus the final reduce
    assert agent.complete.await_count > 1
    assert "slice notes" in agent.complete.await_args_list[-1].kwargs["text"]


def test_summarize_thread_hands_back_small_transcripts():
    old_agent, old_settings = runtime.agent, runtime.settings
    agent = MagicMock()
    agent.complete = AsyncMock(return_value="should not run")
    runtime.agent = agent
    runtime.settings = MagicMock()
    runtime.settings.ai_enabled = True
    runtime.settings.thread_summary_chunk_chars = 3000

    client = FakeSlack([page([msg("1.0", "U1", "hi"), msg("1.1", "U2", "yo")], more=False)])

    async def run():
        return await summarize_thread(client, "C1", "1.0")

    try:
        digest = asyncio.run(run())
    finally:
        runtime.agent, runtime.settings = old_agent, old_settings

    assert digest.summary == ""
    assert "U1" in digest.transcript and "U2" in digest.transcript
    agent.complete.assert_not_called()


def _tool_context(**overrides):
    from bot.ai.tools import ToolContext

    settings = MagicMock()
    settings.thread_summary_max_messages = 2000
    settings.thread_summary_allowed = MagicMock(return_value=False)
    values = {
        "db": MagicMock(),
        "settings": settings,
        "client": AsyncMock(),
        "user_client": None,
        "bot_user_id": "UBOT",
        "user_id": "UGUEST",
        "channel_id": "C1",
        "thread_ts": "1.0",
        "is_dm": False,
        "is_owner": False,
        "is_admin": False,
    }
    values.update(overrides)
    return ToolContext(**values)


def test_summarize_tool_is_gated_by_channel_allowlist():
    from unittest.mock import patch

    from bot.ai.tools import Toolbox

    box = Toolbox(MagicMock())
    digest = ThreadDigest(
        channel="C1",
        ts="1.0",
        messages=1204,
        participants=63,
        first_ts="1.0",
        last_ts="4000.0",
        truncated=True,
        summary="the tldr",
    )

    async def run():
        ctx = _tool_context()
        ctx.settings.thread_summary_allowed = MagicMock(return_value=True)
        with patch("bot.ai.tools.summarize_thread", AsyncMock(return_value=digest)):
            allowed = await box.execute("summarize_thread", {}, ctx)
        ctx.settings.thread_summary_allowed = MagicMock(return_value=False)
        with patch("bot.ai.tools.summarize_thread", AsyncMock(return_value=digest)):
            denied = await box.execute("summarize_thread", {}, ctx)
        return allowed, denied

    allowed, denied = asyncio.run(run())
    assert "the tldr" in allowed and "1,204 messages" in allowed
    assert "only the owner/admin" in denied
    assert "AI_THREAD_SUMMARY_CHANNELS" in denied


def test_extra_tools_do_not_widen_the_guest_toolbox():
    from bot.ai.tools import Toolbox

    box = Toolbox(MagicMock())
    base = {
        item["function"]["name"] for item in box.definitions_for(is_owner=False)
    }
    extra = {
        item["function"]["name"]
        for item in box.definitions_for(
            is_owner=False, extra_tools=frozenset({"summarize_thread"})
        )
    }
    assert "summarize_thread" not in base
    assert "summarize_thread" in extra
    assert extra - base == {"summarize_thread"}
    assert "send_dm" not in extra


def test_question_finds_a_needle_in_the_middle_of_a_huge_thread():
    old_agent, old_settings = runtime.agent, runtime.settings
    agent = MagicMock()

    async def complete(**kwargs):
        if "answering one question" in kwargs["system"]:
            return "the vault code is 4417, said by <@U9>."
        if "scanning one slice" in kwargs["system"]:
            return "NOTHING"
        return "tldr"

    agent.complete = AsyncMock(side_effect=complete)
    runtime.agent = agent
    runtime.settings = MagicMock()
    runtime.settings.ai_enabled = True
    runtime.settings.thread_summary_chunk_chars = 14000

    messages = [
        msg(f"17{i:08d}.000001", f"U{i % 9}", "normal chatter " + "x" * 190)
        for i in range(1500)
    ]
    messages[750] = msg("1700000750.000001", "U9", "the vault code is 4417, do not share")
    pages = []
    for start in range(0, len(messages), 200):
        more = start + 200 < len(messages)
        pages.append(
            page(
                messages[start : start + 200],
                more=more,
                cursor=str(start + 200) if more else "",
            )
        )
    client = FakeSlack(
        pages,
        permalink="https://example.slack.com/archives/C1/p1700000000000001",
    )

    async def run():
        return await summarize_thread(
            client, "C1", "1700000000000001", question="what is the vault code?"
        )

    try:
        digest = asyncio.run(run())
    finally:
        runtime.agent, runtime.settings = old_agent, old_settings

    assert digest.hits
    assert "4417" in digest.hits[0].text
    assert digest.hits[0].link and "/p1700000750000001" in digest.hits[0].link
    # the middle of the transcript was dropped, but the answer is still there
    assert "4417" not in digest.transcript
    text = format_digest(digest)
    assert "direct hits" in text and "4417" in text
    assert "question: what is the vault code?" in text
    reduce_call = agent.complete.await_args_list[-1]
    assert "what is the vault code?" in reduce_call.kwargs["text"]
    assert "4417" in reduce_call.kwargs["text"]


def test_summarize_tool_forwards_the_question():
    from unittest.mock import patch

    from bot.ai.tools import Toolbox

    box = Toolbox(MagicMock())
    digest = ThreadDigest(
        channel="C1",
        ts="1.0",
        messages=2,
        participants=2,
        first_ts="1.0",
        last_ts="1.1",
        truncated=False,
        transcript="a\nb",
    )
    ctx = _tool_context()
    ctx.settings.thread_summary_allowed = MagicMock(return_value=True)
    core = AsyncMock(return_value=digest)

    async def run():
        with patch("bot.ai.tools.summarize_thread", core):
            return await box.execute(
                "summarize_thread", {"question": "who has the code?"}, ctx
            )

    text = asyncio.run(run())
    assert "thread in <#C1>" in text
    assert core.await_args.kwargs["question"] == "who has the code?"

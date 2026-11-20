from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from bot.ai.agent import Agent, looks_like_pseudo_tool_call, tool_call_signature
from bot.db import Database


def _settings():
    settings = MagicMock()
    settings.bot_name = "nugget"
    settings.ai_enabled = True
    settings.ai_api_key = "test-key"
    settings.ai_base_url = "https://ai.hackclub.com/proxy/v1"
    settings.ai_model = "test-model"
    settings.ai_fallback_model = ""
    settings.ai_assistant_model = "assistant-model"
    settings.ai_assistant_fallback_model = ""
    settings.ai_gate_model = "test-gate"
    settings.ai_max_context_chars = 1000
    settings.ai_history_limit = 5
    settings.owner_user_id = "U_OWNER"
    settings.repo_url = "https://github.com/repo"
    settings.thread_summary_max_messages = 2000
    settings.thread_summary_allowed.return_value = False
    settings.is_admin.return_value = False
    settings.is_moderator.return_value = False
    return settings


def _agent(tmp_path):
    db = Database(str(tmp_path / "loop.db"))
    db.init()
    return Agent(
        _settings(), db, client=AsyncMock(), user_client=None, bot_user_id="U_BOT"
    )


def _response(content="", tool_calls=None):
    res = MagicMock()
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls or []
    res.choices = [choice]
    res.usage = None
    return res


def _tool_call(name, arguments, call_id="call_1"):
    call = MagicMock()
    call.id = call_id
    call.function.name = name
    call.function.arguments = arguments
    return call


def _reply(agent):
    return asyncio.run(
        agent.reply(
            conversation="dm:U_OWNER",
            user_id="U_OWNER",
            text="is confession 42253 in #confessions yet",
            channel_id="D1",
            thread_ts=None,
            is_dm=True,
        )
    )


def test_workspace_questions_never_use_the_tools_off_fast_path(tmp_path):
    agent = _agent(tmp_path)
    # These need a tool. Running them on the fast path (tools off) is what made
    # the bot narrate "calls search_slack" instead of searching.
    assert not agent._simple_chat("is confession 42253 in #confessions yet")
    assert not agent._simple_chat("is 42253 posted yet")
    assert not agent._simple_chat("tldr")
    assert not agent._simple_chat("did anyone figure out the deploy")
    # Real small talk still gets the cheap path.
    assert agent._simple_chat("hey, what's up?")
    assert agent._simple_chat("who are you?")
    assert agent._simple_chat("thanks!")
    assert agent._simple_chat("i got the internship")


def test_pseudo_tool_call_detection():
    names = frozenset({"search_slack", "summarize_thread"})
    assert looks_like_pseudo_tool_call("(calls search_slack for 42253)", names)
    assert looks_like_pseudo_tool_call("i'll rerun summarize_thread now", names)
    assert looks_like_pseudo_tool_call("i'll rerun it and check the channel", frozenset())
    assert looks_like_pseudo_tool_call(
        "i don't have a result from that search yet", frozenset()
    )
    assert not looks_like_pseudo_tool_call("i searched slack already", names)
    assert not looks_like_pseudo_tool_call("calls someone else", names)
    assert not looks_like_pseudo_tool_call("", names)


def test_tool_call_signature_is_order_independent():
    one = tool_call_signature("search_slack", '{"query": "42253", "count": 5}')
    two = tool_call_signature("search_slack", '{"count": 5, "query": "42253"}')
    assert one == two
    assert tool_call_signature("search_slack", "{bad") != tool_call_signature(
        "search_slack", "{}"
    )


def test_repeated_tool_call_forces_a_real_answer(tmp_path):
    agent = _agent(tmp_path)
    agent.toolbox.execute = AsyncMock(return_value="no slack results for '42253'")
    seen = {"rounds": 0}

    async def fake_chat(messages, *args, **kwargs):
        seen["rounds"] += 1
        if kwargs.get("tools") is False:
            return _response("nothing matching 42253 in #confessions."), "primary"
        call = _tool_call(
            "search_slack",
            '{"query": "42253", "channel": "#confessions"}',
            call_id=f"call_{seen['rounds']}",
        )
        return _response("", [call]), "primary"

    agent._chat = fake_chat
    answer = _reply(agent)

    assert answer == "nothing matching 42253 in #confessions."
    assert "stuck in a tool loop" not in answer
    assert agent.toolbox.execute.await_count == 2
    # two executions, one repeat-detection round, then the tools-off answer
    assert seen["rounds"] == 4


def test_tool_budget_exhaustion_still_answers(tmp_path):
    agent = _agent(tmp_path)
    agent.toolbox.execute = AsyncMock(return_value="still nothing")
    seen = {"rounds": 0}

    async def fake_chat(messages, *args, **kwargs):
        seen["rounds"] += 1
        if kwargs.get("tools") is False:
            return _response("here is what i found."), "primary"
        call = _tool_call(
            "search_slack",
            f'{{"query": "q{seen["rounds"]}"}}',
            call_id=f"call_{seen['rounds']}",
        )
        return _response("", [call]), "primary"

    agent._chat = fake_chat
    answer = _reply(agent)

    assert answer == "here is what i found."
    assert agent.toolbox.execute.await_count == 6
    assert seen["rounds"] == 7


def test_fast_path_turns_tools_on_after_a_narrated_call(tmp_path):
    db = Database(str(tmp_path / "fast.db"))
    db.init()
    user_client = MagicMock()
    agent = Agent(
        _settings(), db, client=user_client, user_client=user_client, bot_user_id="U_BOT"
    )
    agent.toolbox.execute = AsyncMock()
    flags = []

    async def fake_chat(messages, *args, **kwargs):
        flags.append(kwargs.get("tools"))
        if len(flags) == 1:
            return _response("(calls search_slack for 42253)"), "primary"
        return _response("nothing matching 42253."), "primary"

    agent._chat = fake_chat
    answer = asyncio.run(
        agent.reply(
            conversation="dm:U_OWNER",
            user_id="U_OWNER",
            text="and?",
            channel_id="D1",
            thread_ts=None,
            is_dm=True,
            client=user_client,
        )
    )

    assert answer == "nothing matching 42253."
    # First round is the cheap tools-off path; the retry must have tools.
    assert flags == [False, True]
    agent.toolbox.execute.assert_not_awaited()


def test_narrated_tool_call_is_nudged_not_sent(tmp_path):
    agent = _agent(tmp_path)
    agent.toolbox.execute = AsyncMock()
    seen = {"rounds": 0}

    async def fake_chat(messages, *args, **kwargs):
        seen["rounds"] += 1
        if seen["rounds"] == 1:
            return (
                _response('(calls search_slack for "42253" in #confessions)'),
                "primary",
            )
        return _response("nothing matching 42253 in #confessions."), "primary"

    agent._chat = fake_chat
    answer = _reply(agent)

    assert answer == "nothing matching 42253 in #confessions."
    assert seen["rounds"] == 2
    agent.toolbox.execute.assert_not_awaited()


def test_dm_never_stays_silent_when_the_model_blanks(tmp_path):
    agent = _agent(tmp_path)

    async def fake_chat(messages, *args, **kwargs):
        return _response(""), "primary"

    agent._chat = fake_chat
    answer = asyncio.run(
        agent.reply(
            conversation="dm:U1",
            user_id="U1",
            text="hello",
            channel_id="D1",
            thread_ts=None,
            is_dm=True,
        )
    )
    assert answer
    assert answer not in {"[NO_REPLY]", "NO_REPLY"}

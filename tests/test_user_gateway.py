import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from bot.runtime import runtime
from bot.user_gateway import UserGateway, _last_thread


def test_user_gateway_init():
    gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=AsyncMock())
    assert gw.token == "xoxc-test"
    assert gw.cookie == "xoxd-test"
    assert gw.user_id == "U_ALT"
    assert not gw._running


def test_user_gateway_typing_context():
    async def run():
        mock_ws = AsyncMock()
        mock_ws.closed = False
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT")
        gw._ws = mock_ws

        async with gw.typing("C_TEST", thread_ts="123.456"):
            await asyncio.sleep(0.05)

        assert mock_ws.send_json.called
        call_args = mock_ws.send_json.call_args[0][0]
        assert call_args["type"] == "typing"
        assert call_args["channel"] == "C_TEST"
        assert call_args["thread_ts"] == "123.456"

    asyncio.run(run())


def test_user_gateway_handle_reconnect_and_ping():
    async def run():
        mock_ws = AsyncMock()
        mock_ws.closed = False
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT")
        gw._ws = mock_ws

        # reconnect_url
        await gw._handle_event({"type": "reconnect_url", "url": "wss://reconnect.slack.com/fast"})
        assert gw._reconnect_url == "wss://reconnect.slack.com/fast"

        # ping frame
        await gw._handle_event({"type": "ping", "id": 1234})
        mock_ws.send_json.assert_called_with({"type": "pong", "reply_to": 1234})

    asyncio.run(run())


def test_user_gateway_handle_event_filters():
    async def run():
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT")
        runtime.bot_user_id = "U_BOT"

        # Non-message
        await gw._handle_event({"type": "presence_change"})

        # Subtype bot_message
        await gw._handle_event({"type": "message", "subtype": "bot_message", "channel": "D1", "user": "U_OWNER"})

        # Bot id present
        await gw._handle_event({"type": "message", "bot_id": "B123", "channel": "D1", "user": "U_OTHER"})

        # Bot account message loop suppression
        await gw._handle_event({"type": "message", "channel": "D1", "user": "U_BOT"})

        # Own message
        await gw._handle_event({"type": "message", "channel": "D1", "user": "U_ALT"})

        # Non-DM and not mentioning alt
        await gw._handle_event({"type": "message", "channel": "C1", "user": "U_OWNER", "text": "hello"})

    asyncio.run(run())


def test_user_gateway_deduplication():
    async def run():
        mock_client = AsyncMock()
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=mock_client)
        mock_agent = AsyncMock()
        mock_agent.reply = AsyncMock(return_value="pong")
        runtime.agent = mock_agent

        old_settings = runtime.settings
        mock_settings = MagicMock()
        mock_settings.owner_user_id = "U_OWNER"
        mock_settings.ai_enabled = True
        mock_settings.ai_allowed.return_value = True
        runtime.settings = mock_settings

        event = {
            "type": "message",
            "channel": "D_TEST",
            "user": "U_OWNER",
            "text": "ping",
            "ts": "555.000",
        }

        try:
            with patch("bot.user_gateway.fetch_slack_images", AsyncMock(return_value=[])):
                await gw._handle_event(event)
                assert mock_agent.reply.call_count == 1

                # Replay identical event ts
                await gw._handle_event(event)
                assert mock_agent.reply.call_count == 1
        finally:
            runtime.settings = old_settings

    asyncio.run(run())


def test_user_gateway_channel_mention():
    async def run():
        mock_client = AsyncMock()
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=mock_client)
        mock_agent = AsyncMock()
        mock_agent.reply = AsyncMock(return_value="channel mention response")
        runtime.agent = mock_agent

        mock_db = MagicMock()
        runtime.db = mock_db

        old_settings = runtime.settings
        mock_settings = MagicMock()
        mock_settings.owner_user_id = "U_OWNER"
        mock_settings.ai_enabled = True
        mock_settings.ai_allowed.return_value = True
        runtime.settings = mock_settings

        event = {
            "type": "message",
            "channel": "C_TEST",
            "user": "U_OWNER",
            "text": "<@U_ALT> follow up question here",
            "ts": "200.002",
            "thread_ts": "200.000",
        }

        try:
            with patch("bot.user_gateway.fetch_slack_images", AsyncMock(return_value=[])):
                await gw._handle_event(event)

            mock_agent.reply.assert_called_once()
            call_kwargs = mock_agent.reply.call_args[1]
            assert call_kwargs["trigger"] == "mention"
            assert call_kwargs["thread_ts"] == "200.000"
            mock_db.mark_thread.assert_called_with("C_TEST", "200.000", owner="user")
            mock_client.chat_postMessage.assert_called_once()
            assert mock_client.chat_postMessage.call_args[1]["unfurl_links"] is False
        finally:
            runtime.settings = old_settings

    asyncio.run(run())


def test_user_gateway_mpim_mention():
    async def run():
        mock_client = AsyncMock()
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=mock_client)
        mock_agent = AsyncMock()
        mock_agent.reply = AsyncMock(return_value="group dm reply")
        runtime.agent = mock_agent

        mock_db = MagicMock()
        runtime.db = mock_db

        old_settings = runtime.settings
        mock_settings = MagicMock()
        mock_settings.owner_user_id = "U_OWNER"
        mock_settings.is_admin.return_value = False
        mock_settings.ai_enabled = True
        # Group DMs are allowed even when AI_CHANNELS is restrictive.
        mock_settings.ai_allowed.return_value = False
        runtime.settings = mock_settings

        event = {
            "type": "message",
            "channel": "G_TEST",
            "channel_type": "mpim",
            "user": "U_GUEST",
            "text": "<@U_ALT> can you check this",
            "ts": "210.002",
            "thread_ts": "210.000",
        }

        try:
            with patch("bot.user_gateway.fetch_slack_images", AsyncMock(return_value=[])):
                await gw._handle_event(event)

            mock_agent.reply.assert_called_once()
            call_kwargs = mock_agent.reply.call_args[1]
            assert call_kwargs["trigger"] == "mpim_mention"
            assert call_kwargs["conversation"] == "user_mpim:G_TEST"
            assert call_kwargs["is_mpim"] is True
            assert call_kwargs["thread_ts"] == "210.000"
            mock_db.mark_thread.assert_called_with("G_TEST", "210.000", owner="user")
            mock_client.chat_postMessage.assert_called_once()
            assert mock_client.chat_postMessage.call_args[1]["thread_ts"] == "210.000"
        finally:
            runtime.settings = old_settings

    asyncio.run(run())


def test_user_gateway_honors_explicit_no_reply():
    async def run():
        mock_client = AsyncMock()
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=mock_client)
        mock_agent = AsyncMock()
        mock_agent.reply = AsyncMock(return_value="got it.")
        runtime.agent = mock_agent

        old_settings = runtime.settings
        mock_settings = MagicMock()
        mock_settings.owner_user_id = "U_OWNER"
        mock_settings.is_admin.return_value = False
        mock_settings.ai_enabled = True
        runtime.settings = mock_settings

        event = {
            "type": "message",
            "channel": "D_TEST",
            "user": "U_OWNER",
            "text": "pls dont respond to this chicken",
            "ts": "120.000",
        }

        try:
            with patch("bot.user_gateway.fetch_slack_images", AsyncMock(return_value=[])):
                await gw._handle_event(event)
            mock_agent.reply.assert_not_called()
            mock_client.chat_postMessage.assert_not_called()
        finally:
            runtime.settings = old_settings

    asyncio.run(run())


def test_user_gateway_handle_dm_success():
    async def run():
        mock_client = AsyncMock()
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=mock_client)
        mock_ws = AsyncMock()
        mock_ws.closed = False
        gw._ws = mock_ws

        mock_agent = AsyncMock()
        mock_agent.reply = AsyncMock(return_value="hello back <!channel>")
        runtime.agent = mock_agent

        old_settings = runtime.settings
        mock_settings = MagicMock()
        mock_settings.owner_user_id = "U_OWNER"
        mock_settings.is_admin.return_value = False
        mock_settings.ai_enabled = True
        mock_settings.ai_allowed.return_value = True
        runtime.settings = mock_settings

        event = {
            "type": "message",
            "channel": "D_TEST",
            "user": "U_OWNER",
            "text": "hey what's up",
            "ts": "100.000",
        }

        try:
            with patch("bot.user_gateway.fetch_slack_images", AsyncMock(return_value=[])):
                await gw._handle_event(event)

            mock_agent.reply.assert_called_once()
            mock_client.chat_postMessage.assert_called_once()
            call_kw = mock_client.chat_postMessage.call_args[1]
            assert call_kw["channel"] == "D_TEST"
            assert "@channel" in call_kw["text"]
            assert "<!channel>" not in call_kw["text"]
            assert call_kw["unfurl_links"] is False
            assert call_kw["unfurl_media"] is False
        finally:
            runtime.settings = old_settings

    asyncio.run(run())


def test_user_gateway_dm_threads_stay_in_thread():
    async def run():
        mock_client = AsyncMock()
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=mock_client)

        mock_agent = AsyncMock()
        mock_agent.reply = AsyncMock(return_value="thread reply")
        runtime.agent = mock_agent

        old_settings = runtime.settings
        mock_settings = MagicMock()
        mock_settings.owner_user_id = "U_OWNER"
        mock_settings.is_admin.return_value = False
        mock_settings.ai_enabled = True
        mock_settings.ai_allowed.return_value = True
        runtime.settings = mock_settings

        top_level = {
            "type": "message",
            "channel": "D_TEST",
            "user": "U_OWNER",
            "text": "top level dm",
            "ts": "100.000",
        }
        threaded = {
            "type": "message",
            "channel": "D_TEST",
            "user": "U_OWNER",
            "text": "in a dm thread",
            "ts": "100.002",
            "thread_ts": "100.000",
        }

        try:
            with patch("bot.user_gateway.fetch_slack_images", AsyncMock(return_value=[])):
                await gw._handle_event(top_level)
                await gw._handle_event(threaded)

            reply_calls = mock_agent.reply.call_args_list
            assert reply_calls[0][1]["thread_ts"] is None
            assert reply_calls[1][1]["thread_ts"] == "100.000"

            posts = mock_client.chat_postMessage.call_args_list
            assert posts[0][1]["thread_ts"] is None
            assert posts[1][1]["thread_ts"] == "100.000"
        finally:
            runtime.settings = old_settings

    asyncio.run(run())


def test_user_gateway_ignores_bot_owned_thread(tmp_path):
    from bot.db import Database

    async def run():
        db = Database(str(tmp_path / "gw_threads.db"))
        db.init()
        db.mark_thread("C_TEST", "200.000", owner="bot")

        mock_client = AsyncMock()
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=mock_client)
        mock_agent = AsyncMock()

        old_agent = runtime.agent
        old_db = runtime.db
        old_settings = runtime.settings
        runtime.agent = mock_agent
        runtime.db = db

        mock_settings = MagicMock()
        mock_settings.ai_enabled = True
        mock_settings.ai_allowed.return_value = True
        runtime.settings = mock_settings

        event = {
            "type": "message",
            "channel": "C_TEST",
            "user": "U_OWNER",
            "text": "follow up question here",
            "ts": "200.002",
            "thread_ts": "200.000",
        }
        try:
            with patch("bot.user_gateway.fetch_slack_images", AsyncMock(return_value=[])):
                await gw._handle_event(event)
            mock_agent.reply.assert_not_called()
        finally:
            runtime.agent = old_agent
            runtime.db = old_db
            runtime.settings = old_settings

    asyncio.run(run())


def test_user_gateway_ignores_unmentioned_channel_messages(tmp_path):
    from bot.db import Database

    async def run():
        db = Database(str(tmp_path / "gw_unknown_thread.db"))
        db.init()
        old_db, old_user_client = runtime.db, runtime.user_client
        runtime.db = db
        runtime.user_client = None
        _last_thread.clear()

        mock_client = AsyncMock()
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=mock_client)
        mock_agent = AsyncMock()
        runtime.agent = mock_agent

        old_settings = runtime.settings
        mock_settings = MagicMock()
        mock_settings.ai_enabled = True
        mock_settings.ai_allowed.return_value = True
        runtime.settings = mock_settings

        event = {
            "type": "message",
            "channel": "C_TEST",
            "user": "U_ALICE",
            "text": "regular channel chatter or thread message",
            "ts": "300.002",
            "thread_ts": "300.000",
        }

        try:
            with patch("bot.user_gateway.fetch_slack_images", AsyncMock(return_value=[])):
                await gw._handle_event(event)
            mock_agent.reply.assert_not_called()
        finally:
            runtime.db = old_db
            runtime.user_client = old_user_client
            runtime.settings = old_settings

    asyncio.run(run())


def _followup_thread(messages):
    """A thread reply event plus the replies slack would return for it."""
    return (
        {
            "type": "message",
            "channel": "C_TEST",
            "user": "U_OWNER",
            "text": messages[-1]["text"],
            "ts": messages[-1]["ts"],
            "thread_ts": messages[0]["ts"],
        },
        {"messages": messages},
    )


def test_user_gateway_follows_up_in_its_own_thread(tmp_path):
    from bot.db import Database

    async def run():
        db = Database(str(tmp_path / "gw_followup.db"))
        db.init()
        db.mark_thread("C_TEST", "500.000", owner="user")
        _last_thread.clear()

        event, replies = _followup_thread(
            [
                {"user": "U_OWNER", "text": "<@U_ALT> advice?", "ts": "500.000"},
                {"user": "U_ALT", "text": "what's the situation?", "ts": "500.001"},
                {
                    "user": "U_OWNER",
                    "text": "should i buy gta 6? i have 500 hours on gta v so i know i will like it",
                    "ts": "500.002",
                },
            ]
        )
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(return_value=replies)
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=mock_client)
        mock_agent = AsyncMock()
        mock_agent.reply = AsyncMock(return_value="buy it")
        runtime.agent = mock_agent

        old_db, old_user_client, old_settings = runtime.db, runtime.user_client, runtime.settings
        runtime.db = db
        runtime.user_client = None
        mock_settings = MagicMock()
        mock_settings.ai_enabled = True
        mock_settings.ai_allowed.return_value = True
        mock_settings.ai_thread_cooldown = 0
        mock_settings.bot_name = "Chicen Nugget Bot"
        runtime.settings = mock_settings

        try:
            with patch("bot.user_gateway.fetch_slack_images", AsyncMock(return_value=[])):
                await gw._handle_event(event)

            mock_agent.reply.assert_called_once()
            kwargs = mock_agent.reply.call_args[1]
            assert kwargs["trigger"] == "thread_followup"
            assert kwargs["thread_ts"] == "500.000"
            assert "advice?" in kwargs["context"]
            mock_client.chat_postMessage.assert_called_once()
            assert mock_client.chat_postMessage.call_args[1]["thread_ts"] == "500.000"
            assert db.is_thread("C_TEST", "500.000", owner="user")
        finally:
            runtime.db = old_db
            runtime.user_client = old_user_client
            runtime.settings = old_settings

    asyncio.run(run())


def test_user_gateway_thread_followup_respects_cooldown(tmp_path):
    from bot.db import Database

    async def run():
        db = Database(str(tmp_path / "gw_cooldown.db"))
        db.init()
        db.mark_thread("C_TEST", "510.000", owner="user")

        event, replies = _followup_thread(
            [
                {"user": "U_ALT", "text": "what's the situation?", "ts": "510.000"},
                {"user": "U_OWNER", "text": "can you check this build error for me", "ts": "510.002"},
            ]
        )
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(return_value=replies)
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=mock_client)
        mock_agent = AsyncMock()
        mock_agent.reply = AsyncMock(return_value="on it")
        runtime.agent = mock_agent

        old_db, old_user_client, old_settings = runtime.db, runtime.user_client, runtime.settings
        runtime.db = db
        runtime.user_client = None
        mock_settings = MagicMock()
        mock_settings.ai_enabled = True
        mock_settings.ai_allowed.return_value = True
        mock_settings.ai_thread_cooldown = 120
        mock_settings.bot_name = "Chicen Nugget Bot"
        runtime.settings = mock_settings

        try:
            with patch("bot.user_gateway.fetch_slack_images", AsyncMock(return_value=[])):
                await gw._handle_event(event)
                mock_agent.reply.assert_called_once()

                # Same thread again inside the cooldown window stays quiet.
                mock_agent.reply.reset_mock()
                await gw._handle_event({**event, "ts": "510.003", "text": "any update on that?"})
            mock_agent.reply.assert_not_called()
        finally:
            _last_thread.clear()
            runtime.db = old_db
            runtime.user_client = old_user_client
            runtime.settings = old_settings

    asyncio.run(run())


def test_user_gateway_followup_steps_back_when_bot_spoke_last(tmp_path):
    from bot.db import Database

    async def run():
        db = Database(str(tmp_path / "gw_dual.db"))
        db.init()
        db.mark_thread("C_TEST", "800.000", owner="bot")
        db.mark_thread("C_TEST", "800.000", owner="user")
        _last_thread.clear()

        event = {
            "type": "message",
            "channel": "C_TEST",
            "user": "U_OWNER",
            "text": "can you check the diff too",
            "ts": "800.003",
            "thread_ts": "800.000",
        }
        replies = {
            "messages": [
                {"user": "U_ALT", "text": "here's the answer", "ts": "800.000"},
                {"user": "U_BOT", "text": "want me to check the diff?", "ts": "800.002"},
            ]
        }
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(return_value=replies)
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=mock_client)
        mock_agent = AsyncMock()
        runtime.agent = mock_agent

        old_db, old_user_client, old_settings = runtime.db, runtime.user_client, runtime.settings
        old_bot = runtime.bot_user_id
        runtime.db = db
        runtime.user_client = None
        runtime.bot_user_id = "U_BOT"
        mock_settings = MagicMock()
        mock_settings.ai_enabled = True
        mock_settings.ai_allowed.return_value = True
        mock_settings.ai_thread_cooldown = 0
        runtime.settings = mock_settings

        try:
            with patch("bot.user_gateway.fetch_slack_images", AsyncMock(return_value=[])):
                await gw._handle_event(event)
            mock_agent.reply.assert_not_called()
        finally:
            runtime.db = old_db
            runtime.user_client = old_user_client
            runtime.settings = old_settings
            runtime.bot_user_id = old_bot

    asyncio.run(run())


def test_user_gateway_mention_gets_channel_context():
    async def run():
        mock_client = AsyncMock()
        mock_client.conversations_history = AsyncMock(
            return_value={
                "messages": [
                    {
                        "user": "U_OWNER",
                        "text": "<!channel> sorry for ping but should i buy gta 6?",
                        "ts": "600.000",
                    }
                ]
            }
        )
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT", user_client=mock_client)
        mock_agent = AsyncMock()
        mock_agent.reply = AsyncMock(return_value="yes")
        runtime.agent = mock_agent

        old_db, old_user_client, old_settings = runtime.db, runtime.user_client, runtime.settings
        runtime.db = None
        runtime.user_client = None
        mock_settings = MagicMock()
        mock_settings.ai_enabled = True
        mock_settings.ai_allowed.return_value = True
        runtime.settings = mock_settings

        event = {
            "type": "message",
            "channel": "C_TEST",
            "user": "U_OWNER",
            "text": f"<@{gw.user_id}> advice?",
            "ts": "600.001",
        }

        try:
            with patch("bot.user_gateway.fetch_slack_images", AsyncMock(return_value=[])):
                await gw._handle_event(event)

            mock_agent.reply.assert_called_once()
            kwargs = mock_agent.reply.call_args[1]
            assert kwargs["trigger"] == "mention"
            assert "gta 6" in kwargs["context"]
        finally:
            runtime.db = old_db
            runtime.user_client = old_user_client
            runtime.settings = old_settings

    asyncio.run(run())

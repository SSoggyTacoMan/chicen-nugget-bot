import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from bot.runtime import runtime
from bot.user_gateway import UserGateway


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


def test_user_gateway_ignores_unmentioned_channel_messages():
    async def run():
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
            runtime.settings = old_settings

    asyncio.run(run())

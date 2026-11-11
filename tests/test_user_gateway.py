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


def test_user_gateway_handle_event_filters():
    async def run():
        gw = UserGateway(token="xoxc-test", cookie="xoxd-test", user_id="U_ALT")

        # Non-message
        await gw._handle_event({"type": "presence_change"})

        # Subtype bot_message
        await gw._handle_event({"type": "message", "subtype": "bot_message", "channel": "D1", "user": "U_OWNER"})

        # Own message
        await gw._handle_event({"type": "message", "channel": "D1", "user": "U_ALT"})

        # Non-DM and not mentioning alt
        await gw._handle_event({"type": "message", "channel": "C1", "user": "U_OWNER", "text": "hello"})

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
        finally:
            runtime.settings = old_settings

    asyncio.run(run())

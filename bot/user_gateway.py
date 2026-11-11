"""Real-time gateway client for the personal assistant user account.

Maintains a client WebSocket connection to Slack for:
1. Real-time typing indicators in DMs and threads ("User is typing...").
2. Direct message handling, mentions, and thread continuations for the personal AI assistant identity.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from collections import deque
from contextlib import asynccontextmanager, suppress
from typing import Any

import aiohttp
from slack_sdk.web.async_client import AsyncWebClient

from bot.ai.limits import user_retry_after
from bot.runtime import runtime
from bot.slack_api import fetch_slack_images
from bot.utils.text import chunk_text, disarm_broadcasts, format_mrkdwn, strip_mentions

logger = logging.getLogger(__name__)


class UserGateway:
    def __init__(
        self,
        token: str,
        cookie: str | None = None,
        user_id: str = "",
        user_client: AsyncWebClient | None = None,
    ) -> None:
        self.token = token
        self.cookie = cookie
        self.user_id = user_id
        self.user_client = user_client
        self._running = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._msg_id = 1
        self._task: asyncio.Task | None = None
        self._seen_ts: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._reconnect_url: str | None = None
        self._last_thread: dict[tuple[str, str], float] = {}

    def _next_id(self) -> int:
        mid = self._msg_id
        self._msg_id += 1
        return mid

    async def send_typing(self, channel: str, thread_ts: str | None = None) -> None:
        """Send a typing indicator frame over the active websocket."""
        if not self._ws or self._ws.closed:
            return
        payload: dict[str, Any] = {
            "id": self._next_id(),
            "type": "typing",
            "channel": channel,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            await self._ws.send_json(payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("failed to send typing indicator: %s", exc)

    async def _presence_loop(self) -> None:
        """Keep the user account presence active while connected."""
        while self._running and self._ws and not self._ws.closed:
            if self.user_client:
                with suppress(Exception):
                    await self.user_client.users_setPresence(presence="auto")
            with suppress(Exception):
                await self._ws.send_json({"id": self._next_id(), "type": "ping"})
            try:
                await asyncio.sleep(45.0)
            except asyncio.CancelledError:
                break

    @asynccontextmanager
    async def typing(self, channel: str, thread_ts: str | None = None):
        """Context manager keeping the typing indicator active every ~2.5s."""
        stop_event = asyncio.Event()

        async def _typing_loop() -> None:
            while not stop_event.is_set():
                await self.send_typing(channel, thread_ts)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=2.5)
                except TimeoutError:
                    pass

        loop_task = asyncio.create_task(_typing_loop())
        try:
            yield
        finally:
            stop_event.set()
            loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await loop_task

    async def start(self) -> None:
        """Run the gateway connection loop."""
        self._running = True
        backoff = 1.0
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        if self.cookie:
            headers["Cookie"] = f"d={self.cookie}"

        while self._running:
            try:
                ws_url = self._reconnect_url
                self._reconnect_url = None

                async with aiohttp.ClientSession(headers=headers) as session:
                    self._session = session
                    if not ws_url:
                        async with session.post(
                            "https://slack.com/api/client.getWebSocketURL",
                            data={"token": self.token},
                        ) as resp:
                            data = await resp.json()

                        if not data.get("ok"):
                            logger.warning("failed to fetch gateway websocket URL: %s", data.get("error"))
                            await asyncio.sleep(backoff)
                            backoff = min(backoff * 2, 30.0)
                            continue

                        primary_ws = data.get("primary_websocket_url")
                        routing_context = data.get("routing_context")
                        params = {
                            "token": self.token,
                            "sync_desync": "1",
                            "slack_client": "desktop",
                            "start_args": "?agent=client&org_wide_aware=true&connect_only=true",
                            "no_query_on_subscribe": "1",
                            "flannel": "3",
                            "lazy_channels": "1",
                            "gateway_server": routing_context or "",
                            "batch_presence_aware": "1",
                        }
                        ws_url = f"{primary_ws}?{urllib.parse.urlencode(params)}"

                    logger.info("connecting to user gateway websocket...")

                    async with session.ws_connect(ws_url, heartbeat=20.0) as ws:
                        self._ws = ws
                        backoff = 1.0
                        logger.info("user gateway connected as %s", self.user_id)

                        # Set presence active
                        if self.user_client:
                            with suppress(Exception):
                                await self.user_client.users_setPresence(presence="auto")

                        presence_task = asyncio.create_task(self._presence_loop())
                        try:
                            async for msg in ws:
                                if not self._running:
                                    break
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    try:
                                        event = msg.json()
                                        asyncio.create_task(self._handle_event(event))
                                    except Exception as exc:  # noqa: BLE001
                                        logger.debug("error handling gateway frame: %s", exc)
                                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                    break
                        finally:
                            presence_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await presence_task
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                if self._running:
                    logger.warning("user gateway connection error: %s (reconnecting in %.1fs)", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

        self._running = False
        self._ws = None
        self._session = None

    async def stop(self) -> None:
        """Stop the gateway connection."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        event_type = event.get("type")

        # Fast reconnect URL cache
        if event_type == "reconnect_url":
            url = event.get("url")
            if url:
                self._reconnect_url = url
            return

        # Handle ping frame from gateway
        if event_type == "ping":
            pong: dict[str, Any] = {"type": "pong"}
            if "id" in event:
                pong["reply_to"] = event["id"]
            if self._ws and not self._ws.closed:
                with suppress(Exception):
                    await self._ws.send_json(pong)
            return

        if event_type != "message":
            return

        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            return

        # Ignore other bots and loopbacks between identities
        if event.get("bot_id"):
            return

        sender_id = event.get("user")
        if not sender_id or sender_id == self.user_id or (runtime.bot_user_id and sender_id == runtime.bot_user_id):
            return

        # Message deduplication
        msg_ts = event.get("ts")
        if msg_ts:
            if msg_ts in self._seen_ts:
                return
            self._seen_ts.add(msg_ts)
            self._seen_order.append(msg_ts)
            if len(self._seen_order) > 1000:
                old = self._seen_order.popleft()
                self._seen_ts.discard(old)

        channel = event.get("channel") or ""
        text = (event.get("text") or "").strip()
        is_dm = channel.startswith("D")
        is_mention = bool(self.user_id and f"<@{self.user_id}>" in text)
        thread_ts = event.get("thread_ts")
        root_ts = thread_ts or event.get("ts")
        is_thread_continuation = bool(
            thread_ts and runtime.db and runtime.db.is_thread(channel, thread_ts)
        )

        if not (is_dm or is_mention or is_thread_continuation):
            return

        settings = runtime.settings
        # If bot account was explicitly tagged, let the bot account handle it
        if runtime.bot_user_id and f"<@{runtime.bot_user_id}>" in text:
            return

        if is_thread_continuation and not is_mention and not is_dm:
            has_files = bool(event.get("files") or event.get("file") or event.get("subtype") == "file_share")
            if not has_files and len(text) < 4:
                return
            if runtime.db and runtime.db.kv_get("config:thread_followups") == "false":
                return
            if not has_files and len(text.split()) < 3 and "?" not in text:
                return
            cooldown = getattr(settings, "ai_thread_cooldown", 0)
            if isinstance(cooldown, (int, float)) and cooldown > 0:
                key = (channel, root_ts)
                if time.monotonic() - self._last_thread.get(key, 0.0) < cooldown:
                    return
                self._last_thread[key] = time.monotonic()

        # Permission check
        if is_dm:
            allowed = sender_id == settings.owner_user_id or (
                settings.ai_allow_admins and settings.is_admin(sender_id)
            )
            if not allowed:
                return
        else:
            if not settings.ai_allowed(channel):
                return

        if runtime.agent is None or not settings.ai_enabled:
            return

        retry = user_retry_after(sender_id)
        if retry:
            if self.user_client:
                with suppress(Exception):
                    await self.user_client.chat_postMessage(
                        channel=channel,
                        text=f"slow down a lil, try again in {retry}s :3",
                        thread_ts=root_ts if not is_dm else None,
                        unfurl_links=False,
                        unfurl_media=False,
                    )
            return

        images = await fetch_slack_images(event, self.token)
        clean_text = strip_mentions(text).strip()
        if not clean_text and images:
            clean_text = "What is in this image?"
        if not clean_text and not images:
            return

        if is_dm:
            trigger = "dm"
        elif is_thread_continuation and not is_mention:
            trigger = "thread_followup"
        else:
            trigger = "mention"

        conv_key = f"user_dm:{sender_id}" if is_dm else f"user_ch:{channel}:{root_ts}"
        try:
            async with (
                runtime.lock(conv_key),
                self.typing(channel, thread_ts=root_ts if not is_dm else None),
            ):
                answer = await runtime.agent.reply(
                    conversation=conv_key,
                    user_id=sender_id,
                    text=clean_text,
                    channel_id=channel,
                    thread_ts=root_ts if not is_dm else None,
                    is_dm=is_dm,
                    trigger=trigger,
                    images=images,
                    client=self.user_client,
                )
        except Exception:
            logger.exception("user gateway agent turn failed")
            answer = "something broke while thinking. check my logs?"

        if not answer or answer.strip() in {"[NO_REPLY]", "NO_REPLY"}:
            return

        if not is_dm and runtime.db and root_ts:
            runtime.db.mark_thread(channel, root_ts)

        if self.user_client:
            for chunk in chunk_text(disarm_broadcasts(format_mrkdwn(answer))):
                try:
                    await self.user_client.chat_postMessage(
                        channel=channel,
                        text=chunk,
                        thread_ts=root_ts if not is_dm else None,
                        unfurl_links=False,
                        unfurl_media=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("failed to post user reply to %s: %s", channel, exc)

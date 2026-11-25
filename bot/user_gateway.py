"""Real-time gateway client for the personal assistant user account.

Maintains a client WebSocket connection to Slack for:
1. Real-time typing indicators in DMs and threads ("User is typing...").
2. Direct message handling and explicit mentions for the personal AI assistant identity.

The "own slack account, always online, typing indicator" pattern is inspired by
noodle (https://github.com/MatthiasLubbertsen/noodle).
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
from bot.features.followup import BOI_RE, conversation_lines, looks_like_followup
from bot.runtime import runtime
from bot.slack_api import fetch_slack_images, message_text, transcribe_slack_audio
from bot.utils.text import (
    asks_for_silence,
    chat_chunks,
    chunk_options,
    disarm_broadcasts,
    format_mrkdwn,
    has_owner_signature,
    incoming_text,
    is_ignored_message,
)

logger = logging.getLogger(__name__)

# (channel, thread_ts) -> monotonic time of boi's last unprompted thread reply.
_last_thread: dict[tuple[str, str], float] = {}
_THREAD_TRACKING_MAX = 500
# A missing entry must not look like "just replied" on a freshly booted host,
# where time.monotonic() is small.
_NEVER = float("-inf")


def _prune_tracking() -> None:
    """Drop stale cooldowns so a long-running process doesn't grow forever."""
    if len(_last_thread) <= _THREAD_TRACKING_MAX:
        return
    cutoff = time.monotonic() - 3600.0
    for key, stamp in list(_last_thread.items()):
        if stamp < cutoff:
            _last_thread.pop(key, None)


def _looks_named(text: str) -> bool:
    """True when a message addresses boi by name instead of @mentioning it."""
    return bool(BOI_RE.search(text or ""))


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
        self._event_tasks: set[asyncio.Task] = set()

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
            await asyncio.sleep(45.0)

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
                                        event_task = asyncio.create_task(self._handle_event(event))
                                        self._event_tasks.add(event_task)
                                        event_task.add_done_callback(self._event_tasks.discard)
                                    except Exception as exc:  # noqa: BLE001
                                        logger.debug("error handling gateway frame: %s", exc)
                                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                    break
                        finally:
                            presence_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await presence_task
            except asyncio.CancelledError:
                self._running = False
                self._ws = None
                self._session = None
                raise
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
        if has_owner_signature(text) and sender_id == runtime.settings.owner_user_id:
            return
        if is_ignored_message(text):
            return
        is_dm = channel.startswith("D")
        is_mpim = (event.get("channel_type") or "") == "mpim"
        is_mention = bool(self.user_id and f"<@{self.user_id}>" in text)
        thread_ts = event.get("thread_ts")
        root_ts = thread_ts or event.get("ts")
        # DMs and group DMs stay flat unless the message itself is in a thread.
        # Channel mentions reply in a thread.
        reply_thread_ts = (
            None if (is_dm or is_mpim) and not thread_ts else root_ts
        )

        settings = runtime.settings
        # If bot account was explicitly tagged, let the bot account handle it
        if runtime.bot_user_id and f"<@{runtime.bot_user_id}>" in text:
            return

        if not (is_dm or is_mention):
            followup_lines = await self._thread_followup(event, is_mpim=is_mpim)
            if followup_lines is None:
                return
            followup = True
        else:
            followup = False
            followup_lines = None

        # Permission check
        if is_dm:
            allowed = sender_id == settings.owner_user_id or (
                settings.ai_allow_admins and settings.is_admin(sender_id)
            )
            if not allowed:
                return
        elif not is_mpim and not settings.ai_allowed(channel):
            return

        if runtime.agent is None or not settings.ai_enabled:
            return

        retry = user_retry_after(sender_id)
        if retry:
            # A follow-up that trips the rate limit just stays quiet, like the
            # bot app does; only asked-for replies get the nudge.
            if not followup and self.user_client:
                with suppress(Exception):
                    await self.user_client.chat_postMessage(
                        channel=channel,
                        text=f"slow down a lil, try again in {retry}s :3",
                        thread_ts=reply_thread_ts,
                        unfurl_links=False,
                        unfurl_media=False,
                    )
            return

        images = await fetch_slack_images(event, self.token, cookie=self.cookie)
        voice_transcripts = await transcribe_slack_audio(
            event, self.token, settings, cookie=self.cookie
        )
        clean_text = incoming_text(
            message_text(event),
            bot_user_id=runtime.bot_user_id or "",
            assistant_user_id=self.user_id or runtime.user_id or "",
        )
        if voice_transcripts:
            voice_block = "\n".join(voice_transcripts)
            clean_text = (
                f"{clean_text}\n\n[Voice message]: {voice_block}".strip()
                if clean_text
                else f"[Voice message]: {voice_block}"
            )
        if not clean_text and images:
            clean_text = "What is in this image?"
        if not clean_text and not images:
            return
        if asks_for_silence(clean_text):
            return

        trigger = "dm" if is_dm else ("mpim_mention" if is_mpim else "mention")
        if followup:
            trigger = "thread_followup"
        if is_dm:
            conv_key = f"user_dm:{sender_id}"
        elif is_mpim:
            conv_key = f"user_mpim:{channel}"
        else:
            conv_key = f"user_ch:{channel}:{root_ts}"

        context_lines = followup_lines
        if context_lines is None and not is_dm:
            # Mentions can be one word ("advice?"), so hand the model the
            # messages they are answering.
            read, _humans, _last = await conversation_lines(
                self.user_client,
                channel,
                thread_ts=thread_ts,
                current_ts=event.get("ts"),
                agent_user_id=self.user_id or runtime.user_id or "",
                limit=20 if thread_ts else 12,
                keep=4,
            )
            context_lines = read
        context = "\n".join((context_lines or [])[-4:])
        try:
            async with (
                runtime.lock(conv_key),
                self.typing(channel, thread_ts=reply_thread_ts),
            ):
                answer = await runtime.agent.reply(
                    conversation=conv_key,
                    user_id=sender_id,
                    text=clean_text,
                    channel_id=channel,
                    thread_ts=reply_thread_ts,
                    is_dm=is_dm,
                    is_mpim=is_mpim,
                    trigger=trigger,
                    jev="",
                    images=images,
                    context=context,
                    public_veto=bool(not is_dm and not is_mpim),
                    client=self.user_client,
                    message_ts=event.get("ts"),
                )
        except Exception:
            logger.exception("user gateway agent turn failed")
            answer = "something broke while thinking. check my logs?"

        if not answer or answer.strip() in {"[NO_REPLY]", "NO_REPLY"}:
            return

        if not is_dm and runtime.db and root_ts:
            runtime.db.mark_thread(channel, root_ts, owner="user")

        if self.user_client:
            max_fragments, delay = chunk_options(runtime.settings)
            chunks = chat_chunks(
                disarm_broadcasts(format_mrkdwn(answer)),
                max_fragments=max_fragments,
            )
            for index, chunk in enumerate(chunks):
                try:
                    await self.user_client.chat_postMessage(
                        channel=channel,
                        text=chunk,
                        thread_ts=reply_thread_ts,
                        unfurl_links=False,
                        unfurl_media=False,
                    )
                    if index + 1 < len(chunks) and delay > 0:
                        await asyncio.sleep(delay)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("failed to post user reply to %s: %s", channel, exc)
        if followup and thread_ts:
            _last_thread[(channel, thread_ts)] = time.monotonic()
            _prune_tracking()

    async def _thread_followup(
        self, event: dict[str, Any], *, is_mpim: bool
    ) -> list[str] | None:
        """Keep an unmentioned thread reply flowing after boi already answered.

        Returns the thread's recent lines when the message reads as addressed to
        boi, or None when it should stay quiet. This mirrors the bot app's
        `_handle_thread_continuation`, which only ever looks at its own threads.
        """
        settings = runtime.settings
        channel = event.get("channel") or ""
        thread_ts = event.get("thread_ts")
        ts = event.get("ts")
        text = (event.get("text") or "").strip()
        if not channel or not thread_ts or not ts or thread_ts == ts or not text:
            return None
        if is_mpim:
            return None  # group DMs stay mention-only
        if not settings.ai_allowed(channel):
            return None
        db = runtime.db
        if db is None or not db.is_thread(channel, thread_ts, owner="user"):
            return None
        if db.kv_get("config:thread_followups") == "false":
            return None
        lines, humans, boi_last = await conversation_lines(
            self.user_client,
            channel,
            thread_ts=thread_ts,
            current_ts=ts,
            agent_user_id=self.user_id or runtime.user_id or "",
        )
        named = _looks_named(text)
        if (
            not boi_last
            and not named
            and db.is_thread(channel, thread_ts, owner="bot")
        ):
            # The bot app spoke last in a thread both identities joined, so the
            # follow-up belongs to it instead of doubling up.
            return None
        if not looks_like_followup(
            text=text,
            bot_last=boi_last,
            humans=humans,
            has_images=bool(event.get("files")),
            named=named,
        ):
            return None
        cooldown = settings.ai_thread_cooldown
        key = (channel, thread_ts)
        if (
            isinstance(cooldown, (int, float))
            and cooldown > 0
            and not named
            and time.monotonic() - _last_thread.get(key, _NEVER) < cooldown
        ):
            logger.info("boi thread followup cooled down in %s", channel)
            return None
        logger.info("boi following up in %s/%s", channel, thread_ts)
        return lines

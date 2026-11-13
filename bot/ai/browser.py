"""Headless Chromium sessions for the AI agent.

This is deliberately a small browser, not a general automation framework:
open a page, read it, click, fill, press Enter, screenshot, close. It is only
exposed to the owner/admin in DMs, and every request is checked against the
same private-host guard as `fetch_url`.

Playwright is optional at import time so the bot still starts on a machine
without the browser installed. The tool returns a setup error instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bot.slack_api import is_private_host

try:
    from playwright.async_api import async_playwright
except Exception:  # noqa: BLE001 - optional dependency
    async_playwright = None

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_TEXT = 7000


class BrowserError(Exception):
    """A browser action the caller can fix."""


@dataclass
class BrowserSession:
    page: Any
    context: Any
    created: float
    last_used: float


class BrowserManager:
    def __init__(
        self,
        root: Path,
        *,
        headless: bool = True,
        timeout: int = 30,
        max_sessions: int = 2,
        idle_timeout: int = 900,
    ) -> None:
        self.root = Path(root).expanduser()
        self.headless = headless
        self.timeout = max(5, int(timeout))
        self.max_sessions = max(1, int(max_sessions))
        self.idle_timeout = max(60, int(idle_timeout))
        self._playwright: Any = None
        self._browser: Any = None
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()
        self._host_cache: dict[str, tuple[float, bool]] = {}

    # ------------------------------------------------------------------ setup
    async def _ensure_browser(self) -> None:
        if async_playwright is None:
            raise BrowserError(
                "browser automation is not installed. run `pip install playwright` "
                "and `playwright install chromium` on the server."
            )
        if self._browser is not None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless
            )
        except Exception as exc:
            try:
                await self._playwright.stop()
            except Exception:
                logger.debug("failed to stop playwright after launch error", exc_info=True)
            self._playwright = None
            raise BrowserError(
                f"couldn't start chromium: {exc}. run `playwright install --with-deps chromium`."
            ) from exc

    async def _host_blocked(self, hostname: str) -> bool:
        host = (hostname or "").lower().strip(".")
        if not host:
            return True
        now = time.monotonic()
        cached = self._host_cache.get(host)
        if cached and now - cached[0] < 600:
            return cached[1]
        try:
            blocked = await is_private_host(host)
        except Exception:  # noqa: BLE001 - fail closed
            blocked = True
        self._host_cache[host] = (now, blocked)
        return blocked

    async def validate_url(self, url: str) -> str:
        value = (url or "").strip()
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise BrowserError("url must be a public http:// or https:// page")
        if await self._host_blocked(parsed.hostname):
            raise BrowserError("that url points at a private or local address")
        return value

    async def _guard_route(self, route: Any) -> None:
        parsed = urlparse(route.request.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            await route.abort()
            return
        if await self._host_blocked(parsed.hostname):
            await route.abort()
            return
        await route.continue_()

    # --------------------------------------------------------------- sessions
    async def _prune(self) -> None:
        now = time.monotonic()
        stale = [
            key
            for key, session in self._sessions.items()
            if now - session.last_used > self.idle_timeout
        ]
        for key in stale:
            await self._close(key)

    async def _close(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        if session is None:
            return
        try:
            await session.context.close()
        except Exception:
            logger.debug("failed to close browser context", exc_info=True)

    async def _session(self, key: str, *, create: bool = False) -> BrowserSession:
        async with self._lock:
            if create:
                await self._ensure_browser()
            await self._prune()
            session = self._sessions.get(key)
            if session is not None:
                session.last_used = time.monotonic()
                return session
            if not create:
                raise BrowserError("no browser page is open. use action='open' first.")
            if len(self._sessions) >= self.max_sessions:
                oldest = min(
                    self._sessions.items(), key=lambda item: item[1].last_used
                )[0]
                await self._close(oldest)
            context = await self._browser.new_context(
                user_agent=USER_AGENT,
                accept_downloads=False,
            )
            await context.route("**/*", self._guard_route)
            page = await context.new_page()
            page.set_default_timeout(self.timeout * 1000)
            now = time.monotonic()
            session = BrowserSession(
                page=page, context=context, created=now, last_used=now
            )
            self._sessions[key] = session
            return session

    async def open(self, key: str, url: str) -> dict[str, Any]:
        target = await self.validate_url(url)
        session = await self._session(key, create=True)
        try:
            await session.page.goto(
                target, wait_until="domcontentloaded", timeout=self.timeout * 1000
            )
            try:
                await session.page.wait_for_load_state(
                    "networkidle", timeout=min(5000, self.timeout * 1000)
                )
            except Exception:
                logger.debug("page never reached network idle", exc_info=True)
        except Exception as exc:
            raise BrowserError(f"couldn't open that page: {exc}") from exc
        session.last_used = time.monotonic()
        return await self.read(key)

    async def read(self, key: str) -> dict[str, Any]:
        session = await self._session(key)
        try:
            title = await session.page.title()
            url = session.page.url
            text = await session.page.inner_text("body")
        except Exception as exc:
            raise BrowserError(f"couldn't read the page: {exc}") from exc
        session.last_used = time.monotonic()
        return {
            "title": title,
            "url": url,
            "text": (text or "").strip()[:MAX_TEXT],
        }

    async def click(self, key: str, selector: str) -> dict[str, Any]:
        session = await self._session(key)
        try:
            await session.page.click(selector, timeout=self.timeout * 1000)
        except Exception as exc:
            raise BrowserError(f"couldn't click {selector!r}: {exc}") from exc
        session.last_used = time.monotonic()
        return await self.read(key)

    async def fill(self, key: str, selector: str, text: str) -> dict[str, Any]:
        session = await self._session(key)
        try:
            await session.page.fill(selector, text, timeout=self.timeout * 1000)
        except Exception as exc:
            raise BrowserError(f"couldn't fill {selector!r}: {exc}") from exc
        session.last_used = time.monotonic()
        return await self.read(key)

    async def press(self, key: str, key_name: str) -> dict[str, Any]:
        session = await self._session(key)
        try:
            await session.page.keyboard.press(key_name)
        except Exception as exc:
            raise BrowserError(f"couldn't press {key_name!r}: {exc}") from exc
        session.last_used = time.monotonic()
        return await self.read(key)

    async def wait_for(self, key: str, selector: str) -> dict[str, Any]:
        session = await self._session(key)
        try:
            await session.page.wait_for_selector(
                selector, timeout=self.timeout * 1000
            )
        except Exception as exc:
            raise BrowserError(f"couldn't find {selector!r}: {exc}") from exc
        session.last_used = time.monotonic()
        return await self.read(key)

    async def back(self, key: str) -> dict[str, Any]:
        session = await self._session(key)
        await session.page.go_back(wait_until="domcontentloaded")
        session.last_used = time.monotonic()
        return await self.read(key)

    async def forward(self, key: str) -> dict[str, Any]:
        session = await self._session(key)
        await session.page.go_forward(wait_until="domcontentloaded")
        session.last_used = time.monotonic()
        return await self.read(key)

    async def reload(self, key: str) -> dict[str, Any]:
        session = await self._session(key)
        await session.page.reload(wait_until="domcontentloaded")
        session.last_used = time.monotonic()
        return await self.read(key)

    async def screenshot(self, key: str, *, full_page: bool = False) -> bytes:
        session = await self._session(key)
        try:
            data = await session.page.screenshot(full_page=full_page)
        except Exception as exc:
            raise BrowserError(f"couldn't take a screenshot: {exc}") from exc
        session.last_used = time.monotonic()
        return data

    async def close(self, key: str) -> str:
        async with self._lock:
            existed = key in self._sessions
            await self._close(key)
        return "closed the browser session." if existed else "no browser session to close."

    def has_session(self, key: str) -> bool:
        return key in self._sessions

    async def shutdown(self) -> None:
        async with self._lock:
            for key in list(self._sessions):
                await self._close(key)
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    logger.debug("failed to close browser", exc_info=True)
                self._browser = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    logger.debug("failed to stop playwright", exc_info=True)
                self._playwright = None

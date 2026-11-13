from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.ai.browser import BrowserError, BrowserManager
from bot.ai.tools import Toolbox, ToolContext


class FakeKeyboard:
    def __init__(self):
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.title_text = "Example"
        self.body = "hello browser"
        self.keyboard = FakeKeyboard()
        self.goto_calls: list[str] = []
        self.clicked: list[str] = []
        self.filled: list[tuple[str, str]] = []

    def set_default_timeout(self, timeout: int) -> None:
        self.timeout = timeout

    async def goto(self, url: str, **kwargs):
        self.url = url
        self.goto_calls.append(url)

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def title(self):
        return self.title_text

    async def inner_text(self, selector: str):
        assert selector == "body"
        return self.body

    async def click(self, selector: str, **kwargs):
        self.clicked.append(selector)

    async def fill(self, selector: str, text: str, **kwargs):
        self.filled.append((selector, text))

    async def wait_for_selector(self, selector: str, **kwargs):
        return None

    async def go_back(self, **kwargs):
        return None

    async def go_forward(self, **kwargs):
        return None

    async def reload(self, **kwargs):
        return None

    async def screenshot(self, full_page: bool = False):
        return b"fake-png"


class FakeContext:
    def __init__(self):
        self.page = FakePage()
        self.closed = False
        self.routes = []

    async def route(self, pattern: str, handler):
        self.routes.append(pattern)

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts: list[FakeContext] = []

    async def new_context(self, **kwargs):
        context = FakeContext()
        self.contexts.append(context)
        return context


def test_browser_manager_actions_and_session_limit(tmp_path):
    manager = BrowserManager(tmp_path / "browser", max_sessions=1)
    browser = FakeBrowser()
    manager._browser = browser
    manager._ensure_browser = AsyncMock()

    async def run():
        with patch("bot.ai.browser.is_private_host", AsyncMock(return_value=False)):
            with pytest.raises(BrowserError, match="open"):
                await manager.read("missing")
            page = await manager.open("one", "https://example.com")
            assert page["title"] == "Example"
            assert page["url"] == "https://example.com"

            read = await manager.read("one")
            assert read["text"] == "hello browser"

            await manager.click("one", "button")
            await manager.fill("one", "input", "nugget")
            await manager.press("one", "Enter")
            await manager.wait_for("one", "#done")
            await manager.back("one")
            await manager.forward("one")
            await manager.reload("one")
            assert await manager.screenshot("one") == b"fake-png"

            first_context = browser.contexts[0]
            await manager.open("two", "https://example.org")
            assert first_context.closed is True
            assert manager.has_session("one") is False
            assert manager.has_session("two") is True

            await manager.close("two")
            assert manager.has_session("two") is False

    asyncio.run(run())


def test_browser_blocks_private_urls(tmp_path):
    manager = BrowserManager(tmp_path / "browser")

    async def run():
        with (
            patch("bot.ai.browser.is_private_host", AsyncMock(return_value=True)),
            pytest.raises(BrowserError, match="private or local"),
        ):
            await manager.validate_url("http://127.0.0.1/admin")
        with pytest.raises(BrowserError, match="public http"):
            await manager.validate_url("file:///etc/passwd")

    asyncio.run(run())


def test_browser_tool_is_dm_only_and_needs_open(tmp_path):
    settings = SimpleNamespace(
        data_dir=tmp_path,
        browser_dir=tmp_path / "browser",
        browser_enabled=True,
        browser_headless=True,
        browser_timeout=10,
        browser_max_sessions=1,
        browser_idle_timeout=900,
    )
    box = Toolbox(settings)
    base = {
        "db": MagicMock(),
        "settings": settings,
        "client": AsyncMock(),
        "user_client": None,
        "bot_user_id": "U_BOT",
        "user_id": "U_OWNER",
        "channel_id": "D1",
        "thread_ts": None,
        "is_owner": True,
        "is_admin": True,
    }

    async def run():
        dm_ctx = ToolContext(**base, is_dm=True)
        missing = await box.execute("browser", {"action": "read"}, dm_ctx)
        assert "open" in missing

        channel_ctx = ToolContext(**base, is_dm=False)
        denied = await box.execute("browser", {"action": "read"}, channel_ctx)
        assert "direct messages" in denied

        stranger = ToolContext(**{**base, "is_owner": False, "is_admin": False}, is_dm=True)
        refused = await box.execute("browser", {"action": "open", "url": "https://example.com"}, stranger)
        assert "owner/admin" in refused

    asyncio.run(run())

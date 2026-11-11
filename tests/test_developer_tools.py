import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from bot.ai.tools import Toolbox, ToolContext
from bot.db import Database
from bot.features.commands import cmd_sysinfo, cmd_todo
from bot.features.router import resolve
from bot.features.stats import (
    collect_system_diagnostics,
    format_system_diagnostics,
    format_uptime,
)
from bot.runtime import runtime


def test_database_tasks(tmp_path):
    db = Database(str(tmp_path / "test_tasks.db"))
    db.init()

    # Add tasks
    t1 = db.add_task("U1", "Fix login bug")
    t2 = db.add_task("U1", "Write documentation")
    db.add_task("U2", "Other user task")
    assert t1 > 0
    assert t2 > t1

    # List tasks
    u1_tasks = db.list_tasks("U1")
    assert len(u1_tasks) == 2
    assert u1_tasks[0]["title"] == "Fix login bug"
    assert u1_tasks[0]["done"] == 0

    # Complete task
    ok = db.complete_task(t1, "U1")
    assert ok is True
    assert len(db.list_tasks("U1")) == 1
    assert len(db.list_tasks("U1", show_all=True)) == 2

    # Uncomplete task
    ok_uncomplete = db.uncomplete_task(t1, "U1")
    assert ok_uncomplete is True
    assert len(db.list_tasks("U1")) == 2

    # Delete task
    db.complete_task(t1, "U1")
    assert db.delete_task(t2, "U1") is True
    assert len(db.list_tasks("U1", show_all=True)) == 1

    # Clear completed
    cleared = db.clear_completed_tasks("U1")
    assert cleared == 1
    assert len(db.list_tasks("U1", show_all=True)) == 0


def test_tool_manage_tasks(tmp_path):
    db = Database(str(tmp_path / "test_task_tool.db"))
    db.init()
    settings = MagicMock()
    box = Toolbox(settings)

    ctx = ToolContext(
        db=db,
        settings=settings,
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_DEV",
        channel_id="C1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )

    async def run():
        # Empty list
        res_empty = await box.execute("manage_tasks", {"action": "list"}, ctx)
        assert "don't have any pending tasks" in res_empty

        # Add task
        res_add = await box.execute("manage_tasks", {"action": "add", "title": "Deploy v2"}, ctx)
        assert "added task `#1`" in res_add

        # List
        res_list = await box.execute("manage_tasks", {"action": "list"}, ctx)
        assert "Deploy v2" in res_list

        # Complete
        res_done = await box.execute("manage_tasks", {"action": "complete", "task_id": 1}, ctx)
        assert "completed task `#1`" in res_done

        # Reopen
        res_undone = await box.execute("manage_tasks", {"action": "uncomplete", "task_id": 1}, ctx)
        assert "reopened task `#1`" in res_undone

        # Delete
        res_del = await box.execute("manage_tasks", {"action": "delete", "task_id": 1}, ctx)
        assert "deleted task `#1`" in res_del

        # Clear
        await box.execute("manage_tasks", {"action": "add", "title": "Old task"}, ctx)
        await box.execute("manage_tasks", {"action": "complete", "task_id": 2}, ctx)
        res_clear = await box.execute("manage_tasks", {"action": "clear_completed"}, ctx)
        assert "cleared 1 completed task" in res_clear

    asyncio.run(run())


def test_cmd_todo_slash(tmp_path):
    db = Database(str(tmp_path / "test_todo_slash.db"))
    db.init()
    runtime.db = db

    captured = []

    async def mock_say(client, command, text, ephemeral=True):
        captured.append(text)

    async def run():
        with patch("bot.features.commands._say", new=mock_say):
            cmd = {"user_id": "U123", "channel_id": "C1"}

            # Bare list empty
            await cmd_todo(AsyncMock(), {**cmd, "text": ""}, AsyncMock())
            assert "don't have any pending tasks" in captured[-1]

            # Add with "add"
            await cmd_todo(AsyncMock(), {**cmd, "text": "add Build feature X"}, AsyncMock())
            assert "added task `#1`" in captured[-1]

            # Bare add
            await cmd_todo(AsyncMock(), {**cmd, "text": "Buy coffee"}, AsyncMock())
            assert "added task `#2`" in captured[-1]

            # List
            await cmd_todo(AsyncMock(), {**cmd, "text": "list"}, AsyncMock())
            assert "Build feature X" in captured[-1]
            assert "Buy coffee" in captured[-1]

            # Done
            await cmd_todo(AsyncMock(), {**cmd, "text": "done 1"}, AsyncMock())
            assert "completed task `#1`" in captured[-1]

            # Undone
            await cmd_todo(AsyncMock(), {**cmd, "text": "undone 1"}, AsyncMock())
            assert "reopened task `#1`" in captured[-1]

            # Rm
            await cmd_todo(AsyncMock(), {**cmd, "text": "rm 2"}, AsyncMock())
            assert "deleted task `#2`" in captured[-1]

            # Clear
            await cmd_todo(AsyncMock(), {**cmd, "text": "done 1"}, AsyncMock())
            await cmd_todo(AsyncMock(), {**cmd, "text": "clear"}, AsyncMock())
            assert "cleared 1 completed task" in captured[-1]

    asyncio.run(run())


def test_system_diagnostics(tmp_path):
    db = Database(str(tmp_path / "test_diag.db"))
    db.init()
    settings = MagicMock()
    settings.bot_name = "Chicen Nugget Bot"
    settings.data_dir = tmp_path
    settings.db_path = tmp_path / "test_diag.db"
    settings.is_admin.side_effect = lambda u: u == "U_ADMIN"

    info = collect_system_diagnostics(settings, db, started_at=100.0)
    assert info["bot_name"] == "Chicen Nugget Bot"
    assert "Python" in info["python_version"]
    assert "git_commit" in info

    formatted = format_system_diagnostics(info)
    assert "system diagnostics" in formatted
    assert "host:" in formatted
    assert "runtime:" in formatted

    assert format_uptime(0.0) == "just now"

    # Slash command cmd_sysinfo
    captured = []

    async def mock_say(client, command, text, ephemeral=True):
        captured.append(text)

    async def run():
        runtime.settings = settings
        runtime.db = db
        with patch("bot.features.commands._say", new=mock_say):
            # Guest
            await cmd_sysinfo(AsyncMock(), {"user_id": "U_GUEST", "channel_id": "C1"}, AsyncMock())
            assert "only admins" in captured[-1]

            # Admin
            await cmd_sysinfo(AsyncMock(), {"user_id": "U_ADMIN", "channel_id": "C1"}, AsyncMock())
            assert "system diagnostics" in captured[-1]

    asyncio.run(run())


def test_tool_github_info_expanded():
    settings = MagicMock()
    box = Toolbox(settings)

    ctx = ToolContext(
        db=MagicMock(),
        settings=settings,
        client=AsyncMock(),
        user_client=None,
        bot_user_id="U_BOT",
        user_id="U_DEV",
        channel_id="C1",
        thread_ts=None,
        is_dm=True,
        is_owner=True,
        is_admin=True,
    )

    async def run():
        # Mock issues
        mock_issues_resp = MagicMock(spec=httpx.Response)
        mock_issues_resp.status_code = 200
        mock_issues_resp.json.return_value = [
            {
                "number": 101,
                "title": "Bug in auth",
                "user": {"login": "alice"},
                "state": "open",
                "comments": 2,
                "labels": [{"name": "bug"}],
                "created_at": "2026-11-10T12:00:00Z",
                "html_url": "https://github.com/org/repo/issues/101",
            },
            # PR that should be filtered out
            {
                "number": 102,
                "title": "Some PR",
                "pull_request": {},
            },
        ]

        # Mock PRs
        mock_prs_resp = MagicMock(spec=httpx.Response)
        mock_prs_resp.status_code = 200
        mock_prs_resp.json.return_value = [
            {
                "number": 201,
                "title": "Add feature Y",
                "user": {"login": "bob"},
                "state": "open",
                "draft": False,
                "head": {"ref": "feature-y"},
                "base": {"ref": "main"},
                "created_at": "2026-11-10T13:00:00Z",
                "html_url": "https://github.com/org/repo/pull/201",
            }
        ]

        # Mock single issue
        mock_issue_resp = MagicMock(spec=httpx.Response)
        mock_issue_resp.status_code = 200
        mock_issue_resp.json.return_value = {
            "number": 101,
            "title": "Bug in auth",
            "user": {"login": "alice"},
            "state": "open",
            "labels": [{"name": "bug"}],
            "comments": 2,
            "created_at": "2026-11-10T12:00:00Z",
            "body": "Detailed error log here",
            "html_url": "https://github.com/org/repo/issues/101",
        }

        async def mock_get(self, url, *args, **kwargs):
            if "/pulls" in url:
                return mock_prs_resp
            if "/issues/101" in url:
                return mock_issue_resp
            if "/issues" in url:
                return mock_issues_resp
            return MagicMock(status_code=404)

        with patch.object(httpx.AsyncClient, "get", new=mock_get):
            # Issues
            res_issues = await box.execute("github_info", {"action": "issues", "target": "org/repo"}, ctx)
            assert "101" in res_issues
            assert "Bug in auth" in res_issues
            assert "102" not in res_issues  # PR was filtered

            # PRs
            res_prs = await box.execute("github_info", {"action": "prs", "target": "org/repo"}, ctx)
            assert "201" in res_prs
            assert "feature-y -> main" in res_prs

            # Single Issue
            res_issue = await box.execute("github_info", {"action": "issue", "target": "org/repo#101"}, ctx)
            assert "Detailed error log" in res_issue

    asyncio.run(run())


def test_router_new_commands():
    h_todo, rest_todo = resolve("todo add test")
    assert h_todo is not None
    assert rest_todo == "add test"

    h_task, _ = resolve("task")
    assert h_task is not None

    h_sys, _ = resolve("sysinfo")
    assert h_sys is not None

    h_diag, _ = resolve("diagnostics")
    assert h_diag is not None

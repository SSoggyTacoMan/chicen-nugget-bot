"""Persistent workspaces for repo investigation and shell work.

A sandbox is just a directory under `DATA_DIR/sandboxes`. `clone_repo` still
exists for one-shot peeks, but a sandbox stays on disk until it is deleted, so
the agent can clone once, read files across turns, run commands, and clean up
later.

The sandbox is a workspace boundary, not a security boundary: commands run as
the same OS user as the bot. Treat it as a place to keep work, not a jail.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_NAME_LENGTH = 48
MAX_READ_BYTES = 512_000
MAX_LIST_ENTRIES = 200
MAX_SEARCH_MATCHES = 60
MAX_COMMAND_CHARS = 8_000

# Noise that makes repo search useless. `.git` is always skipped.
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "target",
    "coverage",
    ".turbo",
}


class SandboxError(Exception):
    """A sandbox action the caller can fix."""


def safe_name(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise SandboxError("sandbox name is required (letters, numbers, dot, dash, underscore)")
    if "/" in raw or "\\" in raw or raw in {".", ".."}:
        raise SandboxError("sandbox name cannot contain path separators")
    name = re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip(".-")
    if not name:
        raise SandboxError("sandbox name is required (letters, numbers, dot, dash, underscore)")
    if len(name) > MAX_NAME_LENGTH:
        raise SandboxError(f"sandbox name is too long (max {MAX_NAME_LENGTH})")
    return name


def safe_env() -> dict[str, str]:
    """Environment for child processes, minus anything that looks secret."""
    blocked = (
        "TOKEN",
        "KEY",
        "SECRET",
        "PASS",
        "AUTH",
        "SSH_",
        "COOKIE",
        "CREDENTIAL",
        "DATABASE_URL",
    )
    return {
        k: v
        for k, v in os.environ.items()
        if not any(marker in k.upper() for marker in blocked)
    }


@dataclass(frozen=True)
class SandboxInfo:
    name: str
    path: Path
    repo: str | None
    created: float
    updated: float


@dataclass(frozen=True)
class ExecResult:
    code: int
    stdout: str
    stderr: str
    timed_out: bool
    cwd: str


class SandboxManager:
    def __init__(
        self,
        root: Path,
        *,
        max_workspaces: int = 20,
        exec_timeout: int = 120,
        max_exec_timeout: int = 600,
    ) -> None:
        self.root = Path(root).expanduser()
        self.meta_root = self.root / ".meta"
        self.max_workspaces = max(1, max_workspaces)
        self.exec_timeout = max(5, exec_timeout)
        self.max_exec_timeout = max(self.exec_timeout, max_exec_timeout)

    # ------------------------------------------------------------------ paths
    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta_root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
            self.meta_root.chmod(0o700)
        except OSError:
            pass

    def _path(self, name: str) -> tuple[str, Path]:
        name = safe_name(name)
        self._ensure()
        path = self.root / name
        resolved = path.resolve()
        root_resolved = self.root.resolve()
        if resolved != root_resolved and root_resolved not in resolved.parents:
            raise SandboxError("sandbox path escapes the sandbox root")
        return name, path

    def _meta_path(self, name: str) -> Path:
        return self.meta_root / f"{safe_name(name)}.json"

    def _read_meta(self, name: str) -> dict[str, Any]:
        try:
            data = json.loads(self._meta_path(name).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_meta(self, name: str, data: dict[str, Any]) -> None:
        self._ensure()
        self._meta_path(name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _touch(self, name: str, **updates: Any) -> None:
        data = self._read_meta(name)
        data.update(updates)
        data["updated"] = time.time()
        self._write_meta(name, data)

    # ------------------------------------------------------------- lifecycle
    def list(self) -> list[SandboxInfo]:
        self._ensure()
        infos: list[SandboxInfo] = []
        for entry in self.root.iterdir():
            if entry.name == ".meta" or entry.is_symlink() or not entry.is_dir():
                continue
            try:
                resolved = entry.resolve()
                if resolved != self.root.resolve() and self.root.resolve() not in resolved.parents:
                    continue
            except OSError:
                continue
            meta = self._read_meta(entry.name)
            try:
                stat = entry.stat()
            except OSError:
                continue
            infos.append(
                SandboxInfo(
                    name=entry.name,
                    path=entry,
                    repo=meta.get("repo") if isinstance(meta.get("repo"), str) else None,
                    created=float(meta.get("created") or stat.st_ctime),
                    updated=float(meta.get("updated") or stat.st_mtime),
                )
            )
        infos.sort(key=lambda info: info.updated, reverse=True)
        return infos

    def create(self, name: str, *, repo: str | None = None) -> SandboxInfo:
        name, path = self._path(name)
        if path.exists():
            raise SandboxError(f"sandbox {name!r} already exists")
        if len(self.list()) >= self.max_workspaces:
            raise SandboxError(
                f"too many sandboxes (max {self.max_workspaces}); delete one first"
            )
        path.mkdir(parents=True, exist_ok=False)
        now = time.time()
        self._write_meta(name, {"repo": repo, "created": now, "updated": now})
        resolved = path.resolve()
        return SandboxInfo(name=name, path=resolved, repo=repo, created=now, updated=now)

    def delete(self, name: str) -> str:
        name, path = self._path(name)
        if not path.exists():
            raise SandboxError(f"sandbox {name!r} does not exist")
        if path.is_symlink():
            raise SandboxError("refusing to delete a symlinked sandbox")
        shutil.rmtree(path)
        try:
            self._meta_path(name).unlink()
        except OSError:
            pass
        return name

    def get(self, name: str) -> SandboxInfo:
        name, path = self._path(name)
        if not path.exists() or not path.is_dir():
            raise SandboxError(f"sandbox {name!r} does not exist")
        meta = self._read_meta(name)
        stat = path.stat()
        return SandboxInfo(
            name=name,
            path=path.resolve(),
            repo=meta.get("repo") if isinstance(meta.get("repo"), str) else None,
            created=float(meta.get("created") or stat.st_ctime),
            updated=float(meta.get("updated") or stat.st_mtime),
        )

    # --------------------------------------------------------------- commands
    async def exec(
        self,
        name: str,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int | None = None,
    ) -> ExecResult:
        info = self.get(name)
        command = (command or "").strip()
        if not command:
            raise SandboxError("command is required")
        if len(command) > MAX_COMMAND_CHARS:
            raise SandboxError(f"command is too long (max {MAX_COMMAND_CHARS} chars)")

        workdir = info.path
        if cwd:
            workdir = self._resolve_in(info.path, cwd)
            if not workdir.is_dir():
                raise SandboxError(f"cwd is not a directory: {cwd}")

        timeout_s = max(5, min(int(timeout or self.exec_timeout), self.max_exec_timeout))
        env = safe_env()
        env.update(
            {
                "HOME": str(info.path),
                "TMPDIR": str(info.path / ".tmp"),
                "PYTHONPATH": "",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TERM": "dumb",
                "CI": "1",
            }
        )
        (info.path / ".tmp").mkdir(exist_ok=True)

        shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
        proc = await asyncio.create_subprocess_exec(
            shell,
            "-lc",
            command,
            cwd=str(workdir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                stdout_b, stderr_b = await proc.communicate()
            except Exception:  # noqa: BLE001
                stdout_b, stderr_b = b"", b""

        self._touch(name)
        return ExecResult(
            code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b.decode(errors="replace").strip(),
            stderr=stderr_b.decode(errors="replace").strip(),
            timed_out=timed_out,
            cwd=str(workdir.relative_to(info.path)) if workdir != info.path else ".",
        )

    # ----------------------------------------------------------------- files
    def _resolve_in(self, root: Path, rel_path: str) -> Path:
        rel = (rel_path or "").strip().lstrip("/")
        target = (root / rel).resolve() if rel else root.resolve()
        root_resolved = root.resolve()
        if target != root_resolved and root_resolved not in target.parents:
            raise SandboxError("access denied: path escapes the sandbox")
        return target

    def read(
        self,
        name: str,
        path: str,
        *,
        offset: int = 1,
        limit: int = 150,
    ) -> str:
        info = self.get(name)
        target = self._resolve_in(info.path, path)
        if not target.is_file():
            raise SandboxError(f"file not found: {path}")
        if target.stat().st_size > MAX_READ_BYTES:
            raise SandboxError(f"file is too large to read (max {MAX_READ_BYTES} bytes)")
        offset = max(1, int(offset))
        limit = max(1, min(500, int(limit)))
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[offset - 1 : offset - 1 + limit]
        self._touch(name)
        return "\n".join(
            f"{i}: {line}" for i, line in enumerate(selected, start=offset)
        ) or "(empty file)"

    def list_files(self, name: str, path: str = "") -> str:
        info = self.get(name)
        target = self._resolve_in(info.path, path)
        if not target.exists():
            raise SandboxError(f"path not found: {path}")
        if target.is_file():
            rel = target.relative_to(info.path)
            return f"file: {rel} ({target.stat().st_size} bytes)"
        entries: list[str] = []
        for item in sorted(target.iterdir()):
            if item.name in SKIP_DIRS and item.is_dir():
                continue
            if item.is_symlink():
                continue
            rel = item.relative_to(info.path)
            suffix = "/" if item.is_dir() else ""
            entries.append(f"{rel}{suffix}")
        self._touch(name)
        return "\n".join(entries[:MAX_LIST_ENTRIES]) or "(empty directory)"

    def search(self, name: str, pattern: str) -> str:
        info = self.get(name)
        pattern = (pattern or "").strip()
        if not pattern:
            raise SandboxError("pattern is required for search")
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)

        matches: list[str] = []
        for dirpath, dirnames, filenames in os.walk(info.path):
            dirnames[:] = [
                d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))
            ]
            for filename in sorted(filenames):
                full = Path(dirpath) / filename
                if full.is_symlink():
                    continue
                try:
                    if full.stat().st_size > MAX_READ_BYTES:
                        continue
                    lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                rel = full.relative_to(info.path)
                for index, line in enumerate(lines, start=1):
                    if rx.search(line):
                        matches.append(f"{rel}:{index}: {line.strip()}")
                        if len(matches) >= MAX_SEARCH_MATCHES:
                            break
                if len(matches) >= MAX_SEARCH_MATCHES:
                    break
            if len(matches) >= MAX_SEARCH_MATCHES:
                break

        self._touch(name)
        if not matches:
            return f"no matches for pattern: {pattern}"
        return "\n".join(matches)

    def disk_free(self) -> int:
        self._ensure()
        return shutil.disk_usage(self.root).free

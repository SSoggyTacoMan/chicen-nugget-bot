#!/usr/bin/env python3
"""Chicen Nugget Boi - Mac Companion Bridge.

Lightweight local bridge daemon running on macOS.
Exposes volume, battery, clipboard, notifications, frontmost app, lock, sleep,
speech, and (optionally) shell execution so the Slack assistant (Boi) can
interact with your Mac.

Zero dependencies (Python 3 standard library only).

This bridge is a root-equivalent capability: anyone who can reach it and knows
the token can run commands as your macOS user. It therefore refuses to start
without a token and listens on loopback by default.

Usage:
    python3 scripts/mac_companion.py --token YOUR_SECRET_TOKEN
    # optional, dangerous: allow `run` and `applescript` actions
    python3 scripts/mac_companion.py --token ... --allow-shell

Expose it through Cloudflare Tunnel or ngrok only if you understand the risk:
    cloudflared tunnel --url http://127.0.0.1:8765

And configure your bot in Slack:
    /nugget setup mac
"""

from __future__ import annotations

import argparse
import hmac
import http.server
import json
import os
import subprocess
import sys

MAX_BODY_BYTES = 1_000_000


def run_cmd(*cmd: str, stdin_data: bytes | None = None, timeout: float = 15.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_data is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = proc.communicate(stdin_data, timeout=timeout)
        return (
            proc.returncode or 0,
            out.decode("utf-8", errors="replace").strip(),
            err.decode("utf-8", errors="replace").strip(),
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        return -1, "", "command timed out"
    except Exception as exc:  # noqa: BLE001
        return -1, "", str(exc)


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    token: str = ""
    allow_shell: bool = False

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write(f"[mac_companion] {args[0]} - {args[1]}\n")

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.token:
            return False
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        return hmac.compare_digest(auth, expected)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/health", "/status"}:
            # Keep the health probe free of host details; no auth is required so
            # tunnels and uptime checks can use it safely.
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        if self.path != "/action":
            self._send_json(404, {"error": "unknown endpoint; use /action"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"error": f"body too large (max {MAX_BODY_BYTES} bytes)"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"error": f"invalid json: {exc}"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return

        action = str(payload.get("action") or "status").lower().strip()

        if action == "status":
            _, batt, _ = run_cmd("pmset", "-g", "batt")
            _, vol, _ = run_cmd("osascript", "-e", "get volume settings")
            _, up, _ = run_cmd("uptime")
            self._send_json(200, {"battery": batt, "volume": vol, "uptime": up})
            return

        if action == "frontmost":
            code, out, err = run_cmd(
                "osascript",
                "-e",
                'tell application "System Events" to get name of first process whose frontmost is true',
            )
            self._send_json(200, {"result": out} if code == 0 else {"error": err or "failed"})
            return

        if action == "apps":
            code, out, err = run_cmd(
                "osascript",
                "-e",
                'tell application "System Events" to get name of every process whose visible is true',
            )
            self._send_json(200, {"result": out} if code == 0 else {"error": err or "failed"})
            return

        if action == "volume":
            level = payload.get("level")
            if level is not None:
                try:
                    vol_int = max(0, min(100, int(level)))
                    code, _, err = run_cmd("osascript", "-e", f"set volume output volume {vol_int}")
                    self._send_json(200, {"result": f"set volume to {vol_int}%"} if code == 0 else {"error": err})
                    return
                except ValueError:
                    pass
            _, vol, _ = run_cmd("osascript", "-e", "get volume settings")
            self._send_json(200, {"result": vol})
            return

        if action == "open":
            target = str(payload.get("target") or "").strip()
            if not target:
                self._send_json(400, {"error": "target required"})
                return
            if target.startswith(("http://", "https://", "/", "~")) or "." in target:
                p = os.path.expanduser(target) if target.startswith("~") else target
                code, _, err = run_cmd("open", p)
            else:
                code, _, err = run_cmd("open", "-a", target)
                if code != 0:
                    code, _, err = run_cmd("open", target)
            self._send_json(200, {"result": f"opened {target}"} if code == 0 else {"error": err})
            return

        if action == "notify":
            text = str(payload.get("text") or "").strip()
            title = str(payload.get("title") or "Chicen Nugget").strip()
            if not text:
                self._send_json(400, {"error": "text required"})
                return
            code, _, err = run_cmd(
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e",
                "end run",
                "--",
                text,
                title,
            )
            self._send_json(200, {"result": "notification sent"} if code == 0 else {"error": err})
            return

        if action == "clipboard":
            if "text" in payload and payload["text"] is not None:
                code, _, err = run_cmd("pbcopy", stdin_data=str(payload["text"]).encode("utf-8"))
                self._send_json(200, {"result": "clipboard updated"} if code == 0 else {"error": err})
                return
            code, out, err = run_cmd("pbpaste")
            self._send_json(200, {"result": out} if code == 0 else {"error": err})
            return

        if action in {"applescript", "run"} and not self.allow_shell:
            self._send_json(
                403,
                {
                    "error": (
                        f"action {action!r} is disabled; restart the bridge with "
                        "--allow-shell to enable arbitrary script/command execution"
                    )
                },
            )
            return

        if action == "applescript":
            script = str(payload.get("script") or "").strip()
            if not script:
                self._send_json(400, {"error": "script required"})
                return
            code, out, err = run_cmd("osascript", "-e", script)
            self._send_json(200, {"result": out, "exit_code": code} if code == 0 else {"error": err, "exit_code": code})
            return

        if action == "run":
            cmd = str(payload.get("command") or "").strip()
            if not cmd:
                self._send_json(400, {"error": "command required"})
                return
            code, out, err = run_cmd("/bin/zsh", "-c", cmd)
            self._send_json(200, {"stdout": out, "stderr": err, "exit_code": code})
            return

        if action == "lock":
            code, _, err = run_cmd("pmset", "displaysleepnow")
            self._send_json(200, {"result": "locked the screen"} if code == 0 else {"error": err})
            return

        if action == "sleep":
            code, _, err = run_cmd("pmset", "sleepnow")
            self._send_json(200, {"result": "going to sleep"} if code == 0 else {"error": err})
            return

        if action == "say":
            text = str(payload.get("text") or "").strip()
            if not text:
                self._send_json(400, {"error": "text required"})
                return
            code, _, err = run_cmd("say", text, timeout=30)
            self._send_json(200, {"result": "spoke the text"} if code == 0 else {"error": err})
            return

        self._send_json(400, {"error": f"unknown action '{action}'"})


def main() -> None:
    if sys.platform != "darwin":
        print("warning: mac_companion is designed to run locally on macOS.", file=sys.stderr)

    parser = argparse.ArgumentParser(description="Chicen Nugget Mac Companion Bridge")
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1). Use 0.0.0.0 only behind a tunnel.",
    )
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    parser.add_argument(
        "--token",
        type=str,
        required=True,
        help="Shared bearer token. Required: the bridge refuses to run unauthenticated.",
    )
    parser.add_argument(
        "--allow-shell",
        action="store_true",
        help="Enable the `run` (arbitrary shell) and `applescript` actions. Off by default.",
    )
    args = parser.parse_args()

    if not args.token.strip():
        parser.error("--token must not be empty")

    BridgeHandler.token = args.token.strip()
    BridgeHandler.allow_shell = bool(args.allow_shell)
    server = http.server.ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    print(
        f"mac_companion listening on http://{args.host}:{args.port} "
        f"(auth: on, shell: {'on' if args.allow_shell else 'off'})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping mac_companion.")
        server.server_close()


if __name__ == "__main__":
    main()

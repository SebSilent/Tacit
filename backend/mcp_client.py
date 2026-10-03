"""Minimal MCP client: JSON-RPC 2.0 over a subprocess's stdio.

Written on the standard library rather than pulling in the official SDK. The
stdio binding is simply newline-delimited JSON-RPC on a subprocess's streams
(https://modelcontextprotocol.io/docs/concepts/transports), so a small, auditable
client is both sufficient and more in keeping with Tacit's minimal dependency
surface. Newer MCP revisions drop the ``initialize`` handshake; we still send it
because servers detect the client's era and fall back.

Nothing here decides *policy* — that lives in ``mcp_registry``. This module only
speaks the protocol and redacts secrets on the way to logs.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time

import httpx

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "tacit", "version": "1.0"}
DEFAULT_TIMEOUT = 30.0

_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")
_URL_CREDS = re.compile(r"//[^/@\s]+@")
# A credential in a query string is the shape that actually reaches a log: it is
# how a fetch or browser URL is usually written. Masking only the
# `//user:pass@host` form left `?token=...` in the ledger in plain text, which is
# the one thing the ledger promises never to hold.
_URL_SECRET_PARAM = re.compile(
    r"([?&;][^=&;#\s]*(?:" + "|".join(h.lower() for h in _SECRET_HINTS)
    + r")[^=&;#\s]*=)([^&#\s]+)", re.I)


def mask_url(url) -> str:
    """Strip credentials from a URL: the ``//user:pass@host`` form, and any
    secret-looking query parameter."""
    text = _URL_CREDS.sub("//***@", str(url or ""))
    return _URL_SECRET_PARAM.sub(r"\1***", text)


class McpError(RuntimeError):
    pass


def looks_secret(name: str) -> bool:
    up = str(name or "").upper()
    return any(hint in up for hint in _SECRET_HINTS)


def redact_env(env: dict) -> dict:
    """A copy of an env mapping with secret-looking values masked."""
    return {k: ("***" if looks_secret(k) else v) for k, v in (env or {}).items()}


def redact(text, secrets) -> str:
    """Replace any secret value appearing in log text with ``***``."""
    out = str(text or "")
    for value in secrets or []:
        value = str(value or "")
        if len(value) >= 6:
            out = out.replace(value, "***")
    return out


def resolve_command(command: str) -> list[str]:
    """Turn a bare command into an argv prefix that actually launches.

    On Windows ``npx``/``uvx``/``npm`` are ``.cmd`` shims that CreateProcess
    cannot run directly, so they are launched through the command processor.
    """
    cmd = str(command or "").strip()
    if not cmd:
        raise McpError("empty command")
    path = shutil.which(cmd) or cmd
    if os.name == "nt" and str(path).lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/c", str(path)]
    return [str(path)]


def extract_text(result) -> str:
    """Flatten an MCP tool result into plain text."""
    if not isinstance(result, dict):
        return str(result)
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict):
            kind = item.get("type")
            if kind == "text":
                parts.append(item.get("text") or "")
            elif kind == "resource":
                parts.append(json.dumps(item.get("resource") or {}, ensure_ascii=False))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        else:
            parts.append(str(item))
    text = "\n".join(p for p in parts if p)
    if not text and isinstance(result.get("structuredContent"), dict):
        text = json.dumps(result["structuredContent"], ensure_ascii=False)
    if result.get("isError") and not text:
        text = "the tool reported an error"
    return text or "(no output)"


class StdioMcpClient:
    """One MCP server subprocess, spoken to with newline-delimited JSON-RPC."""

    def __init__(self, spec: dict, *, on_log=None):
        self.spec = dict(spec or {})
        self.command = self.spec.get("command") or ""
        self.args = list(self.spec.get("args") or [])
        self.env = dict(self.spec.get("env") or {})
        self.cwd = self.spec.get("cwd") or None
        self.timeout = float(self.spec.get("timeout") or DEFAULT_TIMEOUT)
        self.server_info: dict = {}
        self.tools: list[dict] = []
        self.started_at = 0.0
        self._on_log = on_log or (lambda _line: None)

        self._proc: subprocess.Popen | None = None
        self._pending: dict[int, queue.Queue] = {}
        self._id = 0
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._secrets = [v for k, v in self.env.items() if looks_secret(k) and v]

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self) -> None:
        if self.alive():
            return
        argv = resolve_command(self.command) + self.args
        env = {**os.environ, **self.env}
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.cwd or None, env=env, text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=creationflags)
        except FileNotFoundError as exc:
            raise McpError(f"command not found: {argv[0]}") from exc
        except Exception as exc:  # noqa: BLE001
            raise McpError(f"could not start server: {exc}") from exc
        self._closed = False
        self._pending.clear()
        self.started_at = time.time()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self, grace: float = 3.0) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=grace)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
                proc.wait(timeout=grace)
            except Exception:  # noqa: BLE001
                pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:  # noqa: BLE001
                pass
        self._closed = True

    # ── transport ──────────────────────────────────────────────────────────
    def _write(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise McpError("server is not running")
        line = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            try:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            except Exception as exc:  # noqa: BLE001
                raise McpError(f"could not write to server: {exc}") from exc

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self._on_log(redact(line, self._secrets))
                    continue
                if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                    waiter = self._pending.pop(msg.get("id"), None)
                    if waiter is not None:
                        waiter.put(msg)
                # server-initiated notifications are accepted and dropped
        except Exception as exc:  # noqa: BLE001
            self._on_log(f"[stdout reader] {exc}")
        finally:
            self._closed = True
            for waiter in list(self._pending.values()):
                waiter.put({"error": {"message": "server closed the connection"}})

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = redact(raw.strip(), self._secrets)
                if line:
                    self._on_log(line)
        except Exception:  # noqa: BLE001
            pass

    def request(self, method: str, params=None, timeout: float | None = None):
        if not self.alive():
            raise McpError("server is not running")
        with self._id_lock:
            self._id += 1
            rid = self._id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        waiter: queue.Queue = queue.Queue()
        self._pending[rid] = waiter
        self._write(payload)
        wait = float(timeout or self.timeout)
        try:
            msg = waiter.get(timeout=wait)
        except queue.Empty:
            self._pending.pop(rid, None)
            raise McpError(f"'{method}' timed out after {wait:g}s") from None
        if isinstance(msg, dict) and msg.get("error"):
            err = msg["error"] or {}
            raise McpError(str(err.get("message") or err))
        return (msg or {}).get("result")

    def notify(self, method: str, params=None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    # ── protocol ───────────────────────────────────────────────────────────
    def initialize(self, timeout: float | None = None) -> dict:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=timeout or 20.0)
        self.server_info = (result or {}).get("serverInfo") or {}
        try:
            self.notify("notifications/initialized")
        except McpError:
            pass
        return self.server_info

    def list_tools(self, timeout: float | None = None) -> list[dict]:
        result = self.request("tools/list", {}, timeout=timeout or 20.0) or {}
        rows = []
        for tool in result.get("tools") or []:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            rows.append({
                "name": tool["name"],
                "description": (tool.get("description") or "").strip(),
                "inputSchema": tool.get("inputSchema") or {"type": "object", "properties": {}},
            })
        self.tools = rows
        return rows

    def call_tool(self, name: str, arguments: dict | None = None,
                  timeout: float | None = None) -> str:
        result = self.request("tools/call", {
            "name": name, "arguments": arguments or {},
        }, timeout=timeout or self.timeout)
        return extract_text(result)


class HttpMcpClient:
    """Streamable-HTTP MCP transport: one POST per JSON-RPC message.

    The server may answer with a JSON body or a request-scoped SSE stream; both
    are accepted. Session affinity is carried in ``Mcp-Session-Id`` when the
    server hands one out. Presents the same surface as the stdio client so the
    registry can treat them interchangeably.
    """

    def __init__(self, spec: dict, *, on_log=None):
        self.spec = dict(spec or {})
        self.url = str(self.spec.get("command") or self.spec.get("url") or "").strip()
        self.headers = {str(k): str(v) for k, v in (self.spec.get("headers") or {}).items()}
        self.timeout = float(self.spec.get("timeout") or DEFAULT_TIMEOUT)
        self.server_info: dict = {}
        self.tools: list[dict] = []
        self.session_id = ""
        self.started_at = time.time()
        self._id = 0
        self._id_lock = threading.Lock()
        self._on_log = on_log or (lambda _line: None)

    # the same lifecycle verbs the stdio client offers
    def start(self) -> None:
        if not self.url:
            raise McpError("no endpoint URL configured")

    def alive(self) -> bool:
        return bool(self.url)

    def stop(self, grace: float = 0.0) -> None:
        return None

    def _post(self, payload: dict, timeout: float | None = None):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            with httpx.Client(timeout=timeout or self.timeout) as client:
                r = client.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise McpError(f"transport error: {exc}") from exc
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if r.status_code >= 400:
            raise McpError(f"HTTP {r.status_code}: {r.text[:200]}")
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for raw in r.text.splitlines():
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    msg = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == payload.get("id"):
                    return msg
            raise McpError("no matching response in the event stream")
        if not r.text.strip():
            return None
        try:
            return r.json()
        except Exception as exc:  # noqa: BLE001
            raise McpError(f"bad response: {exc}") from exc

    def request(self, method: str, params=None, timeout: float | None = None):
        with self._id_lock:
            self._id += 1
            rid = self._id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        msg = self._post(payload, timeout=timeout)
        if isinstance(msg, dict) and msg.get("error"):
            err = msg["error"] or {}
            raise McpError(str(err.get("message") or err))
        return (msg or {}).get("result")

    def notify(self, method: str, params=None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._post(payload)
        except McpError:
            pass

    def initialize(self, timeout: float | None = None) -> dict:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=timeout or 20.0)
        self.server_info = (result or {}).get("serverInfo") or {}
        self.notify("notifications/initialized")
        return self.server_info

    def list_tools(self, timeout: float | None = None) -> list[dict]:
        result = self.request("tools/list", {}, timeout=timeout or 20.0) or {}
        rows = []
        for tool in result.get("tools") or []:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            rows.append({"name": tool["name"],
                         "description": (tool.get("description") or "").strip(),
                         "inputSchema": tool.get("inputSchema") or {"type": "object",
                                                                   "properties": {}}})
        self.tools = rows
        return rows

    def call_tool(self, name: str, arguments: dict | None = None,
                  timeout: float | None = None) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}},
                              timeout=timeout or self.timeout)
        return extract_text(result)

"""A reusable, deterministic OpenAI-shaped local endpoint for process tests.

A worker subprocess cannot see a fake engine patched into the test process, so
integration tests that need process semantics fake the model at the only seam
that crosses the boundary: a local HTTP server speaking the OpenAI SSE shape.
This module is that server, scripted per request, with the failure shapes the
conformance suite needs: plain answers, tool calls, delays, hangs, truncated
tool JSON, repeated calls, malformed bodies, HTTP errors and connection resets.

All traffic is local; all state belongs to the caller's redirected TACIT_HOME.
Nothing here talks to a real provider.

Script specs (one consumed per request, in order; when the script is exhausted
the server answers with a plain "(script exhausted)" text so a turn can end):

    {"text": "..."}                     plain SSE answer
    {"tool": {"name", "arguments", "id"}}   one tool call; arguments may be a
                                        dict (serialized) or a raw string
                                        (a cut string produces truncated JSON)
    {"tools": [tool, ...]}              several calls in one answer
    {"delay_s": float, ...}             wait before answering (interruptible)
    {"hang": True}                      accept and never answer until released
    {"status": 429 | 500, "body": ...}  HTTP error
    {"reset": True}                     drop the connection without a response
    {"garbage": True}                   200 with a body that is not an SSE stream
    {"raw": b"..."}                     exact bytes, text/event-stream
    {"finish": "...", "usage": {...}}   modifiers for the SSE builders
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def _data(payload: dict) -> str:
    return "data: " + json.dumps(payload) + "\n\n"


def sse_text(text: str, finish: str = "stop", usage: dict | None = None) -> bytes:
    return (_data({"choices": [{"delta": {"content": text}}]})
            + _data({"choices": [{"delta": {}, "finish_reason": finish}],
                     "usage": usage or DEFAULT_USAGE})
            + "data: [DONE]\n\n").encode()


def sse_tools(tools: list[dict], usage: dict | None = None) -> bytes:
    deltas = []
    for i, tool in enumerate(tools):
        args = tool.get("arguments")
        if isinstance(args, (dict, list)):
            args = json.dumps(args)
        deltas.append({"index": i, "id": tool.get("id") or f"call_{i + 1}",
                       "function": {"name": tool.get("name") or "",
                                    "arguments": str(args if args is not None else "{}")}})
    return (_data({"choices": [{"delta": {"tool_calls": deltas}}]})
            + _data({"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                     "usage": usage or DEFAULT_USAGE})
            + "data: [DONE]\n\n").encode()


class FakeProvider:
    """One scripted local endpoint. Thread-safe request counting; the hang and
    delay paths are interruptible so teardown never waits them out."""

    def __init__(self, script: list[dict] | None = None):
        self.script = list(script or [{"text": "ok"}])
        self.requests: list[dict] = []
        self.request_seen = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._next = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                try:
                    outer.requests.append(json.loads(raw))
                except Exception:  # noqa: BLE001
                    outer.requests.append({})
                outer.request_seen.set()
                spec = outer._spec()
                if spec.get("reset"):
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                delay = float(spec.get("delay_s") or 0)
                if delay > 0:
                    end = time.monotonic() + delay
                    while time.monotonic() < end and not outer.release.is_set():
                        time.sleep(0.05)
                if spec.get("hang"):
                    while not outer.release.is_set():
                        time.sleep(0.1)
                    self.close_connection = True
                    return
                if spec.get("status"):
                    body = json.dumps(spec.get("body") or
                                      {"error": {"message": "scripted error",
                                                 "code": spec["status"]}}).encode()
                    self.send_response(int(spec["status"]))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if spec.get("garbage"):
                    body = b"this is not an event stream {{{ not json"
                elif spec.get("raw") is not None:
                    body = spec["raw"]
                elif spec.get("tool") or spec.get("tools"):
                    tools = spec.get("tools") or [spec["tool"]]
                    body = sse_tools(tools, usage=spec.get("usage"))
                else:
                    body = sse_text(spec.get("text") or "",
                                    finish=spec.get("finish") or "stop",
                                    usage=spec.get("usage"))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def _spec(self) -> dict:
        with self._lock:
            if self._next < len(self.script):
                spec = self.script[self._next]
                self._next += 1
                return spec
        return {"text": "(script exhausted)"}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def register(self):
        """Point the (redirected) registry at this endpoint."""
        from backend import config
        config.save_registry({"default": "p/m", "providers": {"p": {
            "baseUrl": self.url, "apiKey": "k",
            "models": [{"id": "m", "contextWindow": 100000}]}}})

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
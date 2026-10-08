"""
Stage 3.5: steering a turn that runs in its own process. The socket's steer
list is dead storage in process mode — the worker owns the turn — so the
steer must ride the worker's stdin, the same line protocol that carries the
cancel. These tests drive a REAL subprocess and a REAL HTTP fake of the
provider, because the whole point is that the steer crosses a process
boundary that a thread cannot reach.

The scripted turn: step 1 calls `list_files` (a tool call keeps the turn
alive into a second step, where the steer drains); step 2 answers with text
that quotes the steer verbatim. If the steer never reached the worker's
`run_turn` steer list, step 2's request never contains it, and the echoed
text is missing — the assertion fails.
"""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from backend import config, turn_worker
from tests.test_turn_worker import TurnWorkerTest


def _sse(*chunks: str) -> bytes:
    """One OpenAI-shaped SSE body from delta chunks."""
    out = []
    for c in chunks:
        out.append('data: ' + json.dumps({"choices": [{"delta": c}]}) + '\n\n')
    out.append('data: ' + json.dumps({"choices": [{"delta": {},
                                                   "finish_reason": "stop"}],
                                      "usage": {"prompt_tokens": 10,
                                                "completion_tokens": 5,
                                                "total_tokens": 15}}) + '\n\n')
    out.append('data: [DONE]\n\n')
    return "".join(out).encode()


class SteerTest(TurnWorkerTest):
    """The steer path, end to end: socket -> stdin line -> run_turn."""

    def test_a_steer_reaches_run_turn_across_the_process_boundary(self):
        seen: dict = {}

        class Fake(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"     # keep-alive; httpx reuses the connection

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                msgs = body.get("messages") or []
                # The steer arrives as a user message appended by run_turn
                # before step 2's request is built. Echo the last user
                # message back, so the transcript proves what the model saw.
                last_user = next((m["content"] for m in reversed(msgs)
                                  if m.get("role") == "user"), "")
                seen["last_user"] = last_user
                # Step 2 is detected by the tool RESULT being in the
                # transcript, not by the steer's text: the steer is "the
                # magic word", so keying on the tool name would re-serve the
                # tool call forever and the turn would never reach step 2.
                if not any(m.get("role") == "tool" for m in msgs):
                    # Step 1: a tool call, so the turn survives into step 2
                    # and the steer has a drain point.
                    sse = _sse({"content": "checking"},
                               {"tool_calls": [{"index": 0, "id": "c1",
                                                "function": {
                                                    "name": "list_files",
                                                    "arguments": "{}"}}]})
                else:
                    # Step 2: quote the steer verbatim.
                    sse = _sse({"content": "steer seen: " + last_user})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(sse)))
                self.end_headers()
                self.wfile.write(sse)
                self.wfile.flush()

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        config.save_registry({"default": "p/m", "providers": {
            "p": {"baseUrl": f"http://127.0.0.1:{port}/v1", "apiKey": "k",
                  "models": [{"id": "m", "contextWindow": 100000}]}}})

        w, ok = self._spawn()
        self.assertTrue(ok)
        # The steer goes in only after the worker is up and reading stdin:
        # the handshake (transcript line) is written by start(), and the
        # worker's stdin watcher thread starts before its first model call.
        self.assertTrue(w.steer("the magic word"),
                        "a live worker must accept the steer over stdin")
        events = list(w.events())
        kinds = [e.get("type") for e in events]
        if "text" not in kinds:
            errs = [e.get("message") for e in events if e.get("type") == "error"]
            self.fail(f"no text event; errors: {errs}")
        text = "".join(e.get("delta") or "" for e in events if e.get("type") == "text")
        self.assertIn("steer seen: the magic word", text,
                      f"the steer must reach run_turn's steer list; got {text!r}")
        self.assertEqual(seen.get("last_user", ""), "the magic word",
                         "step 2's request must contain the steer as a user message")
        w.proc.wait(timeout=10)
        self.assertFalse(w.alive())

    def test_a_steer_on_a_dead_worker_returns_false(self):
        w, ok = self._spawn()
        self.assertTrue(ok)
        for _ in w.events():
            pass
        w.proc.wait(timeout=10)
        self.assertFalse(w.steer("late"),
                         "a dead worker must refuse the steer, not write to a closed pipe")
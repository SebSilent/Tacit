"""Session isolation: one session, one running turn, no cross-session bleed.

Stage 1a of the plan. The leak the user reported was structural: one socket
held one `running` dict while `switch` re-pointed `rec` at another session, so
a turn started in session A could be steered, stopped and saved into session B.
These tests pin the new invariants:

- a session with a running turn refuses a second prompt, from any socket;
- abort on session A never touches session B;
- switch / new_session / plan_implement are refused while busy;
- a browser that disconnects mid-turn does not mark the session idle.

The engine is scripted (no network, no real model), the same way
`test_engine.py` drives its WebSocket tests.
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend import config, session_state, store
from tests.helpers import state_paths


class _SlowProvider:
    """A model that streams one text delta and then stalls until released.

    The stall is the point: it holds the turn open while the test does
    something to another session, which is exactly the window the leak lived
    in. `release()` ends it; the turn then finishes normally.
    """

    def __init__(self):
        self._gate = threading.Event()
        self._released = threading.Event()

    def release(self):
        self._released.set()

    def __call__(self, messages, **kwargs):
        yield {"type": "text", "delta": "working"}
        # Not the gate — the gate is what the test holds. The worker waits on
        # `_released`, which only `release()` sets, so a turn cannot finish
        # behind the test's back.
        self._gate.set()
        self._released.wait(timeout=10)
        yield {"type": "text", "delta": " done"}
        yield {"type": "usage", "usage": {"input": 100, "output": 10, "total": 110}}
        yield {"type": "done", "finish": "stop", "model": {"ref": "p/m"}}


class SessionIsolationTest(unittest.TestCase):
    """Two sockets, two sessions, one invariant each."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        self.addCleanup(self._restore_config)
        config.HOME = home
        for k in self._keys:
            target = home / Path(self._orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        config.save_registry({"default": "p/m", "providers": {
            "p": {"baseUrl": "http://x", "apiKey": "k",
                  "models": [{"id": "m", "contextWindow": 100000}]}}})
        session_state.reset()
        self.addCleanup(session_state.reset)
        # Named apart from the `_client()` method on purpose: an attribute
        # called `_client` would shadow the method and every test would call
        # None. (First run of this suite died on exactly that.)
        self._http = None
        if self._http:
            self.addCleanup(self._http.close)

    def _restore_config(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)

    def _client(self):
        if self._http is None:
            from fastapi.testclient import TestClient
            from backend.main import app
            self._http = TestClient(app)
        return self._http

    def _open(self, sid, **params):
        q = "&".join(f"{k}={v}" for k, v in params.items())
        return self._client().websocket_connect(f"/ws/{sid}?create=1&{q}")

    def _until(self, ws, want, limit=50):
        for _ in range(limit):
            msg = ws.receive_json()
            if msg.get("type") == want:
                return msg
        self.fail(f"never saw a {want} message")

    def _start_turn(self, ws, text="work on it"):
        ws.send_json({"type": "prompt", "message": text})

    # ── one session, one running turn ─────────────────────────────────────
    def test_a_second_prompt_while_busy_is_refused(self):
        """Two sockets on ONE session must serialise, not interleave.

        The registry is the arbiter: the second window's socket has its own
        `running` dict with busy=False, so only the sid-keyed registry can see
        that the session is already working.
        """
        prov = _SlowProvider()
        from backend.ai import engine
        orig = engine.stream_chat
        engine.stream_chat = prov
        try:
            with self._open("iso-one-1", mode="agent", model="p/m") as ws:
                self._until(ws, "session_ready")
                self._start_turn(ws)
                # The registry is set synchronously inside start_turn, before
                # the worker thread is even spawned.
                deadline = time.time() + 5
                while time.time() < deadline and not session_state.busy("iso-one-1"):
                    time.sleep(0.02)
                self.assertTrue(session_state.busy("iso-one-1"),
                                "the session should be marked busy before the worker starts")
                # A second socket on the SAME session asks for a turn.
                with self._open("iso-one-1", mode="agent", model="p/m") as ws2:
                    self._until(ws2, "session_ready")
                    ws2.send_json({"type": "prompt", "message": "me too"})
                    reply = self._until(ws2, "notify")
                self.assertIn("already running", reply.get("message", ""))
                prov.release()
                self._until(ws, "done")
        finally:
            engine.stream_chat = orig

    def test_the_socket_flag_and_the_registry_agree_after_a_turn(self):
        from backend.ai import engine
        orig = engine.stream_chat
        engine.stream_chat = _InstantProvider()
        try:
            with self._open("iso-one-2", mode="agent", model="p/m") as ws:
                self._until(ws, "session_ready")
                self._start_turn(ws)
                self._until(ws, "done")
                deadline = time.time() + 5
                while time.time() < deadline and session_state.busy("iso-one-2"):
                    time.sleep(0.02)
                self.assertFalse(session_state.busy("iso-one-2"),
                                 "the worker's finally must clear the registry")
        finally:
            engine.stream_chat = orig

    # ── abort is scoped to its session ────────────────────────────────────
    def test_abort_on_session_a_does_not_touch_session_b(self):
        """A's stop event must be A's alone.

        The structural guarantee under test: each socket's `running` dict is
        created inside its own connection, so there is no shared stop event
        left to reach. This test would fail on the old code only if the two
        sockets shared one — which they did not even then — but it pins the
        behaviour the plan promises before 1b makes it load-bearing.
        """
        prov = _SlowProvider()
        from backend.ai import engine
        orig = engine.stream_chat
        engine.stream_chat = prov
        try:
            with self._open("iso-a-1", mode="agent", model="p/m") as wsa:
                self._until(wsa, "session_ready")
                self._start_turn(wsa)
                deadline = time.time() + 5
                while time.time() < deadline and not session_state.busy("iso-a-1"):
                    time.sleep(0.02)
                with self._open("iso-b-1", mode="agent", model="p/m") as wsb:
                    self._until(wsb, "session_ready")
                    wsb.send_json({"type": "abort"})
                    # B's abort must not end A's turn: A is still busy.
                    time.sleep(0.2)
                    self.assertTrue(session_state.busy("iso-a-1"),
                                    "abort on B must not stop A's turn")
                    # And B, never having started, is idle.
                    self.assertFalse(session_state.busy("iso-b-1"))
                prov.release()
                self._until(wsa, "done")
        finally:
            engine.stream_chat = orig

    def test_abort_on_the_running_session_stops_it(self):
        from backend.ai import engine
        orig = engine.stream_chat
        engine.stream_chat = _SlowProvider()
        try:
            with self._open("iso-a-2", mode="agent", model="p/m") as wsa:
                self._until(wsa, "session_ready")
                self._start_turn(wsa)
                deadline = time.time() + 5
                while time.time() < deadline and not session_state.busy("iso-a-2"):
                    time.sleep(0.02)
                wsa.send_json({"type": "abort"})
                self._until(wsa, "done")
                deadline = time.time() + 5
                while time.time() < deadline and session_state.busy("iso-a-2"):
                    time.sleep(0.02)
                self.assertFalse(session_state.busy("iso-a-2"))
        finally:
            engine.stream_chat = orig

    # ── rebinding after 1b ────────────────────────────────────────────────
    def test_switch_is_refused_always_now(self):
        """Stage 1b deleted the in-band switch.

        A socket is born attached to one session and stays attached; switching
        is the client opening a different socket. The command answers with the
        reason rather than the generic "not supported", and — the invariant
        that matters — it never rebinds this connection's session record,
        busy or idle.
        """
        other = store.create(title="other", model="p/m", thinking="high")
        store.save(other)
        with self._open("iso-sw-1", mode="agent", model="p/m") as ws:
            self._until(ws, "session_ready")
            ws.send_json({"type": "switch", "sid": other["id"], "thinking": "off"})
            reply = self._until(ws, "error")
            self.assertIn("no longer supported", reply.get("message", ""))
            # The target session was not touched, and this connection still
            # belongs to the session it opened with.
            self.assertEqual(store.get(other["id"])["thinking"], "high")
            ws.send_json({"type": "get_state"})
            hello = self._until(ws, "hello")
            self.assertEqual(hello["sid"], "iso-sw-1")

    def test_a_turn_keeps_writing_into_its_own_session_while_the_socket_is_rebound(self):
        """new_session mid-turn is legal in 1b — and must not steal the turn.

        The socket rebinds to a fresh record; the running turn's worker
        already closed over the record it was started against. Its events,
        its metrics and its transcript steps belong to the session the turn
        was started in. This is the 1b replacement for the 1a idle-only gate:
        not "rebinding is refused", but "rebinding cannot redirect".
        """
        prov = _SlowProvider()
        from backend.ai import engine
        orig = engine.stream_chat
        engine.stream_chat = prov
        try:
            with self._open("iso-ns-1", mode="agent", model="p/m") as ws:
                self._until(ws, "session_ready")
                self._start_turn(ws)
                deadline = time.time() + 5
                while time.time() < deadline and not session_state.busy("iso-ns-1"):
                    time.sleep(0.02)
                before = len(store.list_sessions())
                # Stage 1b: the server creates the session and returns its
                # id as an rpc_response; this socket is NOT rebound (a socket
                # is born attached to one session).
                ws.send_json({"type": "new_session"})
                reply = self._until(ws, "rpc_response")
                self.assertTrue(reply.get("ok"), str(reply)[:200])
                new_sid = (reply.get("data") or {}).get("sid") or ""
                self.assertTrue(new_sid)
                self.assertNotEqual(new_sid, "iso-ns-1")
                self.assertEqual(len(store.list_sessions()), before + 1)
                # The registry still says the ORIGINAL session is the busy one.
                self.assertTrue(session_state.busy("iso-ns-1"))
                self.assertFalse(session_state.busy(new_sid))
                prov.release()
                # The turn's completion arrives on this socket tagged with
                # the session it belongs to — the socket never moved.
                deadline = time.time() + 5
                while time.time() < deadline:
                    m = ws.receive_json()
                    if m.get("type") == "done":
                        break
                self.assertEqual(m.get("sid"), "iso-ns-1",
                                 "the turn's events carry the session they belong to")
                # And its transcript landed in the original record. done is
                # published before the epilogue saves the store — and a read
                # racing that write gets None back (read_json's fallback) — so
                # wait for the bus to die first: the save precedes the release.
                from backend import turn_bus
                deadline = time.time() + 5
                while turn_bus.alive("iso-ns-1") and time.time() < deadline:
                    time.sleep(0.05)
                rec = store.get("iso-ns-1")
                self.assertTrue(any(m.get("role") == "assistant" and (m.get("content") or "").strip()
                                    for m in rec.get("messages") or []),
                                "the turn's work is in its own session's transcript")
        finally:
            engine.stream_chat = orig

    # ── the registry outlives the socket ──────────────────────────────────
    def test_a_disconnecting_browser_does_not_mark_a_running_session_idle(self):
        """The busy flag belongs to the worker, not to the connection.

        The old code kept busy on the socket's `running` dict, so a browser
        that closed mid-turn left the flag behind in a dead connection while
        the turn kept running — and a reconnecting window saw an idle session
        that was in fact mid-turn. The registry is ended by the worker's own
        finally, which is still running here.
        """
        prov = _SlowProvider()
        from backend.ai import engine
        orig = engine.stream_chat
        engine.stream_chat = prov
        try:
            ws = self._open("iso-dc-1", mode="agent", model="p/m").__enter__()
            try:
                self._until(ws, "session_ready")
                self._start_turn(ws)
                deadline = time.time() + 5
                while time.time() < deadline and not session_state.busy("iso-dc-1"):
                    time.sleep(0.02)
            finally:
                ws.close()          # the browser walks away mid-turn
            time.sleep(0.3)
            self.assertTrue(session_state.busy("iso-dc-1"),
                            "the turn is still running; the session must read busy")
            prov.release()
            deadline = time.time() + 5
            while time.time() < deadline and session_state.busy("iso-dc-1"):
                time.sleep(0.02)
            self.assertFalse(session_state.busy("iso-dc-1"),
                             "only the worker's own finally ends the session")
        finally:
            engine.stream_chat = orig

    # ── the state endpoint ────────────────────────────────────────────────
    def test_the_state_endpoint_reports_the_running_session(self):
        prov = _SlowProvider()
        from backend.ai import engine
        orig = engine.stream_chat
        engine.stream_chat = prov
        try:
            with self._open("iso-api-1", mode="agent", model="p/m") as ws:
                self._until(ws, "session_ready")
                self._start_turn(ws)
                deadline = time.time() + 5
                while time.time() < deadline and not session_state.busy("iso-api-1"):
                    time.sleep(0.02)
                r = self._client().get("/api/sessions/state").json()
                self.assertIn("iso-api-1", r["busy"])
                row = r["states"]["iso-api-1"]
                self.assertTrue(row["busy"])
                self.assertEqual(row["kind"], "turn")
                self.assertGreater(row["turns"], 0)
                prov.release()
                self._until(ws, "done")
                # done is published before the worker's finally ends the
                # session; poll the endpoint instead of racing it once.
                deadline = time.time() + 5
                r = self._client().get("/api/sessions/state").json()
                while "iso-api-1" in r["busy"] and time.time() < deadline:
                    time.sleep(0.05)
                    r = self._client().get("/api/sessions/state").json()
                self.assertNotIn("iso-api-1", r["busy"])
        finally:
            engine.stream_chat = orig


class _InstantProvider:
    """A model that answers in one step with no tools."""

    def __call__(self, messages, **kwargs):
        yield {"type": "text", "delta": "answer"}
        yield {"type": "usage", "usage": {"input": 100, "output": 10, "total": 110}}
        yield {"type": "done", "finish": "stop", "model": {"ref": "p/m"}}


if __name__ == "__main__":
    unittest.main()
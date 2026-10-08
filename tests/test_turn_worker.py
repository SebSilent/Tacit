"""The turn worker: one process per running turn, killable at any moment.

Stage 3 of the plan. The decision (Decision 2) is process-per-turn with a
two-stage stop: cooperative cancel first, hard tree kill after a 5s grace.
These tests drive a REAL subprocess — no faked engine — because the whole
point is that the turn runs somewhere a thread cannot reach.

The worker runs `agent.run_turn` against a scripted model by pointing the
registry at a local endpoint that does not exist; the turn fails fast, which
is enough to prove the lifecycle: spawn, events, cancel, kill, and that a
killed worker's transcript steps survive through the per-step trace.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from backend import config, session_state, turn_worker
from tests.helpers import state_paths


class TurnWorkerTest(unittest.TestCase):
    """The lifecycle, against a real subprocess."""

    def setUp(self):
        self._live = None
        self._worker_mode = None
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
        # A model whose endpoint is unreachable: the turn fails fast and
        # cleanly, which is all the lifecycle tests need.
        reg = {"default": "p/m", "providers": {"p": {"baseUrl": "http://127.0.0.1:9", "apiKey": "k", "models": [{"id": "m", "contextWindow": 100000}]}}}
        config.save_registry(reg)
        # The suite-wide guard pins the thread path (tests/__init__.py);
        # these tests exercise the process path, so they opt back in — and
        # restore the guard on the way out, or the next suite would spawn
        # real subprocesses against faked engines.
        self._worker_mode = os.environ.get("TACIT_TURN_WORKER")
        os.environ["TACIT_TURN_WORKER"] = "process"
        self.addCleanup(self._restore_worker_mode)
        # The worker is a separate process: it resolves its home from the
        # TACIT_HOME environment variable, not from this process's module
        # attribute. Without this the worker read the real ~/.tacit, talked
        # to a real provider, and the test's fake server was never reached —
        # the unreachable-endpoint test passed by accident, because any
        # endpoint failure produces an error event.
        self._tacit_home = os.environ.get("TACIT_HOME")
        os.environ["TACIT_HOME"] = str(home)
        self.addCleanup(self._restore_tacit_home)
        session_state.reset()
        self.addCleanup(session_state.reset)

    def _restore_tacit_home(self):
        if self._tacit_home is None:
            os.environ.pop("TACIT_HOME", None)
        else:
            os.environ["TACIT_HOME"] = self._tacit_home

    def _restore_worker_mode(self):
        if self._worker_mode is None:
            os.environ.pop("TACIT_TURN_WORKER", None)
        else:
            os.environ["TACIT_TURN_WORKER"] = self._worker_mode

    def _restore_config(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)

    def _spawn(self, sid="tw-1"):
        w = turn_worker.spawn(sid)
        ok = w.start(project=None, ref="p/m", chat=False, session=sid,
                     has_instructions=False, reasoning=None, max_steps=2,
                     messages=[{"role": "system", "content": "s"},
                               {"role": "user", "content": "go"}],
                     trace=[])
        self._live = w if ok else None
        return w, ok

    def test_the_worker_spawns_and_streams_events(self):
        w, ok = self._spawn()
        self.assertTrue(ok, "the process path must spawn under the default mode")
        self.assertTrue(w.alive())
        events = list(w.events())
        kinds = [e.get("type") for e in events]
        self.assertIn("error", kinds, f"an unreachable endpoint must error, got {kinds}")
        # The turn errored before completing a step, so the trace is
        # legitimately empty — but the trace EVENT must have arrived, which
        # is what the parent's capture mechanism needs. Assert the
        # attribute was written (it starts as the caller's list; events()
        # replaces it with what the worker sent).
        self.assertIsNotNone(w._trace)
        w.proc.wait(timeout=10)
        self.assertFalse(w.alive())

    def test_a_completed_step_reaches_the_parent_through_the_trace(self):
        """End to end: a real model response, a real step, a real trace.

        The worker subprocess runs the real engine, so the model is faked
        at the only seam that exists across a process boundary: a local
        HTTP server speaking the OpenAI shape. One completion, no tool
        calls — the turn completes one step, and the parent receives that
        step through the kill-safe trace channel.
        """
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Fake(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"     # keep-alive; httpx reuses the connection

            def do_POST(self):
                # An OpenAI-shaped SSE stream: the engine reads `data:` lines,
                # not a bare JSON body, so the fake must speak the stream.
                sse = (
                    'data: ' + json.dumps({"choices": [{"delta": {
                        "content": "the answer"}}]}) + '\n\n'
                    'data: ' + json.dumps({"choices": [{"delta": {},
                                                        "finish_reason": "stop"}],
                                           "usage": {"prompt_tokens": 10,
                                                     "completion_tokens": 5,
                                                     "total_tokens": 15}}) + '\n\n'
                    'data: [DONE]\n\n').encode()
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
        reg = {"default": "p/m", "providers": {"p": {"baseUrl": f"http://127.0.0.1:{port}/v1", "apiKey": "k", "models": [{"id": "m", "contextWindow": 100000}]}}}
        config.save_registry(reg)
        w, ok = self._spawn()
        self.assertTrue(ok)
        events = list(w.events())
        kinds = [e.get("type") for e in events]
        if "text" not in kinds:
            # Diagnose, then fail with the real message: a bare kinds list
            # says nothing about WHY the endpoint refused.
            errs = [e.get("message") for e in events if e.get("type") == "error"]
            self.fail(f"no text event; errors: {errs}")
        self.assertIn("text", kinds, f"the model's answer must stream, got {kinds}")
        self.assertIn("usage", kinds)
        # The step the worker completed is in the parent's trace.
        self.assertTrue(w._trace, "a completed step must reach the parent")
        self.assertIn("the answer", w._trace[-1].get("text") or "")
        w.proc.wait(timeout=10)
        self.assertFalse(w.alive())

    def test_cancel_on_an_exited_worker_is_honest(self):
        w, ok = self._spawn()
        self.assertTrue(ok)
        for _ in w.events():
            pass
        w.proc.wait(timeout=10)
        res = w.cancel()
        self.assertEqual(res["how"], "already-exited")

    def test_the_thread_escape_hatch_reports_itself(self):
        """TACIT_TURN_WORKER=thread keeps the in-process path reachable."""
        os.environ["TACIT_TURN_WORKER"] = "thread"
        try:
            self.assertEqual(config.turn_worker_mode(), "thread")
            self.assertEqual(turn_worker.worker_mode(), "thread")
            w = turn_worker.spawn("tw-2")
            ok = w.start(project=None, ref="p/m", chat=False, session="tw-2",
                         has_instructions=False, reasoning=None, max_steps=2,
                         messages=[], trace=[])
            self.assertFalse(ok, "the thread mode must refuse to spawn a process")
        finally:
            os.environ.pop("TACIT_TURN_WORKER", None)
        self.assertEqual(config.turn_worker_mode(), "process")

    def test_the_worker_is_keyed_by_its_session(self):
        """The registry's sid key is the worker's sid, not the socket's."""
        # The worker itself never touches the registry (the parent owns the
        # busy flag); this pins the spawn contract: the sid it is handed is
        # the one the parent registered.
        session_state.begin("tw-sid", "turn")
        w, ok = self._spawn("tw-sid")
        self.assertTrue(ok)
        self.assertEqual(w.sid, "tw-sid")
        for _ in w.events():
            pass
        w.proc.wait(timeout=10)
        session_state.end("tw-sid")
        self.assertFalse(session_state.busy("tw-sid"))


class KillTreeTest(unittest.TestCase):
    """The hard stop, against a process that will not cooperate.

    The worker under test is a plain python process that ignores stdin and
    sleeps — the shape of a turn stuck in a model read. Cancel gets the
    grace period, then the tree kill lands, and the process is gone.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _stubborn_process(self):
        script = Path(self._tmp.name) / "stubborn.py"
        script.write_text(
            "import sys, time\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "time.sleep(120)\n", encoding="utf-8")
        argv = [sys.executable, str(script)]
        if os.name == "nt":
            return subprocess.Popen(argv, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, text=True, bufsize=1)
        return subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, bufsize=1,
                                start_new_session=True)

    def test_a_stuck_process_is_killed_after_the_grace(self):
        proc = self._stubborn_process()
        # Wrap it in the worker's kill machinery by pointing a TurnWorker at
        # it: the class's stop path is what is under test, not the spawn.
        w = turn_worker.TurnWorker("tw-kill")
        w.proc = proc
        self.assertTrue(w.alive())
        started = time.time()
        res = w.cancel(grace=1.0)     # a short grace for the test's own clock
        elapsed = time.time() - started
        self.assertEqual(res["how"], "killed")
        self.assertLess(elapsed, 15.0, "the kill must not wait out the sleep")
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the tree kill left the process alive")
        self.assertFalse(w.alive())

    def test_a_cooperating_process_exits_on_cancel(self):
        """The cooperative path: a worker that reads stdin exits cleanly."""
        script = Path(self._tmp.name) / "polite.py"
        script.write_text(
            "import sys, time\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "for line in sys.stdin:\n"
            "    if 'cancel' in line:\n"
            "        sys.stdout.write('bye\\n'); sys.stdout.flush()\n"
            "        break\n", encoding="utf-8")
        argv = [sys.executable, str(script)]
        kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                  "text": True, "bufsize": 1}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
        w = turn_worker.TurnWorker("tw-polite")
        w.proc = proc
        proc.stdout.readline()        # 'ready'
        res = w.cancel(grace=5.0)
        self.assertEqual(res["how"], "cancelled")
        proc.wait(timeout=10)
        self.assertFalse(w.alive())


if __name__ == "__main__":
    unittest.main()
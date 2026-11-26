"""
Stage 3.5: steering a turn that runs in its own process. The socket's steer
list is dead storage in process mode (the worker owns the turn) so the
steer must ride the worker's stdin, the same line protocol that carries the
cancel. These tests drive a REAL subprocess and a REAL HTTP fake of the
provider, because the whole point is that the steer crosses a process
boundary that a thread cannot reach.

The scripted turn: step 1 calls `list_files` (a tool call keeps the turn
alive into a second step, where the steer drains); step 2 answers with text
that quotes the steer verbatim. If the steer never reached the worker's
`run_turn` steer list, step 2's request never contains it, and the echoed
text is missing; the assertion fails.

P2.2 put the durable contract on top of that path: one id per accepted
instruction, the states (queued, delivered, applied, dropped, promoted)
published as they move, the user row persisted exactly once when the engine
reaches it, no final answer while a steer still waits, and an unapplied
steer carried into a new turn instead of vanishing.
"""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from backend import agent, config, turn_bus, turn_worker
from tests.test_turn_worker import TurnWorkerTest
from tests import test_turn_states


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
        self.addCleanup(server.server_close)
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

    def test_the_steer_line_carries_the_id_across_the_pipe(self):
        # P2.2: the control line names the instruction, so the applied event
        # crossing back can mark the right entry. A fake proc records bytes.
        class _Stdin:
            def __init__(self):
                self.lines: list = []

            def write(self, s):
                self.lines.append(s)

            def flush(self):
                pass

        class _Proc:
            def __init__(self):
                self.stdin = _Stdin()

            def poll(self):
                return None

        w = turn_worker.TurnWorker.__new__(turn_worker.TurnWorker)
        w.proc = _Proc()
        self.assertTrue(w.steer("turn left", "ab12cd34"))
        msg = json.loads(w.proc.stdin.lines[0])
        self.assertEqual(msg["steer"], "turn left")
        self.assertEqual(msg["steer_id"], "ab12cd34")


class _FakeWorker:
    def __init__(self):
        self.sent: list = []

    def steer(self, text, steer_id=None):
        self.sent.append((text, steer_id))
        return True

    def alive(self):
        return True


class SteerContract(unittest.TestCase):
    """P2.2 in thread mode: ids, states, exactly-once persistence, promotion."""

    def setUp(self):
        self.harness = test_turn_states.TurnStateDrive(
            "test_a_clean_turn_publishes_one_done_with_its_status")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        from backend import session_runtime
        self.rt = session_runtime

    def _bare_run(self, sid: str, worker=None):
        run = {"stop": threading.Event(), "steer": [], "worker": worker,
               "thread": None, "loop": self.harness.loop, "turn_id": "tq",
               "state": "starting", "states": [],
               "steers": {}, "steer_order": []}
        turn_bus.start(sid, self.harness.loop)
        self.rt._turns[sid] = run
        self.addCleanup(lambda: self.rt._turns.pop(sid, None))
        self.addCleanup(lambda: turn_bus.finish(sid))
        return run

    # ── acceptance and states ─────────────────────────────────────────────
    def test_an_accepted_steer_gets_one_id_and_a_queued_then_delivered_ack(self):
        sid = "sc-accept"
        run = self._bare_run(sid)
        self.assertTrue(self.rt.steer(sid, "not that way"))
        ids = list(run["steers"])
        self.assertEqual(len(ids), 1, "one accepted instruction, one id")
        entry = run["steers"][ids[0]]
        self.assertEqual(entry["text"], "not that way")
        self.assertEqual(run["steer"][0]["id"], ids[0],
                         "the thread path carries the id to the engine")
        states = [e.get("state") for e in turn_bus.replay(sid)
                  if e.get("type") == "steer_state"]
        self.assertEqual(states, ["queued", "delivered"],
                          "queued is acknowledged before applied, separately")

    def test_a_steer_with_no_running_turn_is_refused(self):
        self.assertFalse(self.rt.steer("sc-none", "nothing is running"))

    def test_a_worker_takes_the_steer_over_the_control_stream_not_the_list(self):
        sid = "sc-worker"
        w = _FakeWorker()
        run = self._bare_run(sid, worker=w)
        self.assertTrue(self.rt.steer(sid, "left here"))
        self.assertEqual(w.sent[0][0], "left here")
        self.assertTrue(w.sent[0][1], "the id rides the control stream")
        self.assertEqual(run["steer"], [], "the socket-side list is not the carrier")
        self.assertEqual(run["steers"][w.sent[0][1]]["state"], "delivered")

    def test_a_stop_marks_the_unapplied_queue_dropped(self):
        sid = "sc-cancel"
        run = self._bare_run(sid)
        self.rt.steer(sid, "never reached")
        steer_id = run["steer_order"][0]
        self.rt.cancel(sid)
        self.assertEqual(run["steers"][steer_id]["state"], "dropped",
                         "a stopped steer is dropped, named, never lost silently")

    # ── applied: the one and only user row ────────────────────────────────
    def test_the_applied_steer_persists_its_user_row_exactly_once(self):
        sid = "sc-once"

        def stream_fn(kw):
            live = self.rt._turns.get(sid)
            live["steers"]["zx9"] = {"id": "zx9", "text": "persist me once",
                                     "state": "delivered"}
            live["steer_order"].append("zx9")
            yield {"type": "notify", "message": "steering applied",
                   "level": "info", "steer_id": "zx9"}
            yield {"type": "notify", "message": "steering applied",
                   "level": "info", "steer_id": "zx9"}
            yield {"type": "done", "status": "completed"}

        res = self.harness._drive(sid, [], stream_fn=stream_fn)
        rows = [m for m in res["rec"]["messages"]
                if m.get("role") == "user" and m.get("content") == "persist me once"]
        self.assertEqual(len(rows), 1, "one accepted steer, one user row")
        applied = [e for e in res["published"]
                   if e.get("type") == "steer_state" and e.get("state") == "applied"]
        self.assertEqual(len(applied), 1, "the applied state publishes once")
        self.assertEqual(res["run"]["steers"]["zx9"]["state"], "applied")

    # ── drain before the final answer ─────────────────────────────────────
    def test_a_final_answer_waits_for_a_queued_steer(self):
        q: list = []
        users: list = []

        class FakeEngine:
            def __init__(self):
                self.calls = 0

            def __call__(self, messages, **kw):
                self.calls += 1
                users.append([m for m in messages if m.get("role") == "user"][-1]
                             .get("content"))
                if self.calls == 1:
                    # the steer lands while this answer is streaming
                    q.append({"text": "course change", "id": "df1"})
                    yield {"type": "text", "delta": "a final-sounding answer"}
                else:
                    yield {"type": "text", "delta": "final after the steer"}
                yield {"type": "usage",
                       "usage": {"input": 50, "output": 3, "total": 53}}
                yield {"type": "done", "model": {"ref": "p/m"}}

        orig = agent.engine.stream_chat
        agent.engine.stream_chat = FakeEngine()
        self.addCleanup(setattr, agent.engine, "stream_chat", orig)
        events = list(agent.run_turn(
            [{"role": "system", "content": "s"},
             {"role": "user", "content": "the task"}],
            project=None, ref="p/m", chat=True, steer=q))
        applied = [e for e in events if e.get("type") == "notify"
                   and e.get("steer_id") == "df1"]
        self.assertEqual(len(applied), 1, "the steer is applied, with its id")
        self.assertEqual(users[1], "course change",
                         "the second request sees the steering; the first "
                         "answer could not finish the turn")
        self.assertEqual(events[-1], {"type": "done", "status": "completed"})
        self.assertEqual(q, [], "the queue is drained before completion")

    # ── promotion: an unapplied steer is not lost ─────────────────────────
    def test_an_unapplied_steer_carries_into_a_new_turn(self):
        sid = "sc-promote"
        started: list = []
        orig_start = self.rt.start_turn

        def fake_start(s, rec, text, **kw):
            started.append((s, text))

        self.rt.start_turn = fake_start
        self.addCleanup(setattr, self.rt, "start_turn", orig_start)

        def stream_fn(kw):
            live = self.rt._turns.get(sid)
            live["steers"]["pk1"] = {"id": "pk1", "text": "apply me next",
                                     "state": "delivered"}
            live["steer_order"].append("pk1")
            yield {"type": "text", "delta": "an answer"}
            yield {"type": "done", "status": "completed"}

        res = self.harness._drive(sid, [], stream_fn=stream_fn)
        deadline = time.time() + 5
        while not started and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(started, [(sid, "apply me next")],
                         "the unapplied steer promotes into exactly one new turn")
        self.assertEqual(res["run"]["steers"]["pk1"]["state"], "promoted")
        notices = [e for e in res["published"]
                   if e.get("type") == "notify" and "new turn" in str(e.get("message"))]
        self.assertTrue(notices, "the promotion is announced, never silent")

    def test_a_cancelled_turn_promotes_nothing_and_drops_viscibly(self):
        sid = "sc-drop"

        def stream_fn(kw):
            live = self.rt._turns.get(sid)
            live["steers"]["dl1"] = {"id": "dl1", "text": "too late",
                                     "state": "delivered"}
            live["steer_order"].append("dl1")
            yield {"type": "text", "delta": "x"}
            # stream ends without a terminal; the stop makes it cancelled

        orig_start = self.rt.start_turn
        started: list = []
        self.rt.start_turn = lambda *a, **k: started.append(a)
        self.addCleanup(setattr, self.rt, "start_turn", orig_start)
        try:
            res = self.harness._drive(sid, [], stop_pre_set=True,
                                      stream_fn=stream_fn)
        finally:
            self.rt.start_turn = orig_start
        dones = [e for e in res["published"] if e.get("type") == "done"]
        self.assertEqual(dones[0]["status"], "cancelled")
        time.sleep(0.2)
        self.assertEqual(started, [], "a stopped turn promotes nothing")
        drops = [e for e in res["published"]
                 if e.get("type") == "notify" and "dropped" in str(e.get("message"))]
        self.assertTrue(drops, "the drop is named, never silent")
        self.assertEqual(res["run"]["steers"]["dl1"]["state"], "dropped")

    def test_promotion_is_bounded_and_its_limit_is_told(self):
        sid = "sc-bound"
        started: list = []
        orig_start = self.rt.start_turn
        self.rt.start_turn = lambda *a, **k: started.append(a)
        self.addCleanup(setattr, self.rt, "start_turn", orig_start)

        def stream_fn(kw):
            live = self.rt._turns.get(sid)
            live["steers"]["bd1"] = {"id": "bd1", "text": "over and over",
                                     "state": "delivered"}
            live["steer_order"].append("bd1")
            yield {"type": "done", "status": "completed"}

        res = self.harness._drive(sid, [], stream_fn=stream_fn,
                                  rec_setup=lambda r: r.update(
                                      {"_steer_promotions": 3}))
        time.sleep(0.2)
        self.assertEqual(started, [], "past the bound, nothing is promoted")
        warns = [e for e in res["published"]
                 if e.get("type") == "notify" and "faster than any turn"
                 in str(e.get("message"))]
        self.assertTrue(warns, "the bound is told, not swallowed")
        self.assertEqual(res["run"]["steers"]["bd1"]["state"], "dropped")


if __name__ == "__main__":
    unittest.main()

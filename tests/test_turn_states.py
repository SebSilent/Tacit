"""The formal turn state machine and terminal contract (P2.1).

Every turn publishes a turn_id and a monotonic seq on its events, advances
through the states the plan names, and ends in exactly ONE terminal outcome -
including a stream that dies without saying done, which gets a synthetic
terminal so no viewer ever watches a turn that never ends. And `done` is
never published before the transcript is persisted.
"""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from backend import agent, config, session_runtime, session_state, store, turn_bus
from tests.helpers import drain_turns, state_paths


class TurnStateDrive(unittest.TestCase):
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
        session_state.reset()
        self.addCleanup(session_state.reset)
        self.addCleanup(drain_turns)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def _restore_config(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)

    def _drive(self, sid: str, events: list, stop_pre_set: bool = False):
        rec = store.create(title="states", model="p/m", mode="agent", sid=sid)
        self.assertIsNotNone(rec)
        bus = turn_bus.start(sid, self.loop)
        published: list = []
        orig_publish = bus.publish
        bus.publish = lambda ev, _l=published, _o=orig_publish: (
            _l.append(dict(ev)), _o(ev))[1]
        run = {"stop": threading.Event(), "steer": [], "worker": None,
               "thread": None, "loop": self.loop,
               "turn_id": "t-abc123", "state": "starting", "states": []}
        if stop_pre_set:
            run["stop"].set()
        trace: list = []
        orig_run_turn = agent.run_turn

        def scripted(messages, **kw):
            yield from events

        agent.run_turn = scripted
        try:
            session_runtime._turns[sid] = run
            session_state.begin(sid, "turn")
            session_runtime._drive_turn(sid, rec, [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "go"}],
                project=None, ref="p/m", chat=False, has_instructions=False,
                reasoning=None, trace=trace, run=run, bus=bus)
        finally:
            agent.run_turn = orig_run_turn
        return {"published": published, "run": run, "rec": store.get(sid)}

    # ── ────────────────────────────────────────────────────────────────────
    def test_a_clean_turn_publishes_one_done_with_its_status(self):
        res = self._drive("ts-done", [
            {"type": "text", "delta": "working"},
            {"type": "tool_start", "name": "run_shell", "id": "c1", "args": {}},
            {"type": "tool_end", "name": "run_shell", "id": "c1", "result": "ran",
             "is_error": False},
            {"type": "text", "delta": "answer"},
            {"type": "done", "status": "completed"},
        ])
        dones = [e for e in res["published"] if e["type"] == "done"]
        self.assertEqual(len(dones), 1, "exactly one terminal outcome")
        self.assertEqual(dones[0]["status"], "completed")
        self.assertEqual(res["run"]["state"], "completed")
        self.assertEqual(dones[0]["turn_id"], "t-abc123")

    def test_events_carry_turn_id_and_monotonic_sequence(self):
        res = self._drive("ts-seq", [
            {"type": "text", "delta": "a"},
            {"type": "usage", "usage": {"input": 1, "output": 1}},
            {"type": "done", "status": "completed"},
        ])
        stamped = [e for e in res["published"] if "seq" in e]
        seqs = [e["seq"] for e in stamped]
        self.assertEqual(seqs, sorted(set(seqs)), "sequence numbers strictly grow")
        self.assertTrue(seqs, "events are stamped")
        self.assertTrue(all(e["turn_id"] == "t-abc123" for e in stamped))

    def test_the_done_never_outruns_the_persisted_transcript(self):
        """The terminal publish happens after the store write: a viewer that
        reloads on done reads the whole story."""
        rec = store.create(title="order", model="p/m", mode="agent", sid="ts-order")
        bus = turn_bus.start("ts-order", self.loop)
        orig_publish = bus.publish
        at_done: list = []

        def traced_publish(ev, _o=orig_publish):
            if ev.get("type") == "done":
                on_disk = store.get("ts-order") or {}
                at_done.append(any(
                    "done answer" in str(m.get("content") or "")
                    for m in on_disk.get("messages") or []))
            return _o(ev)

        bus.publish = traced_publish
        run = {"stop": threading.Event(), "steer": [], "worker": None,
               "thread": None, "loop": self.loop, "turn_id": "t1",
               "state": "starting", "states": []}
        session_runtime._turns["ts-order"] = run
        session_state.begin("ts-order", "turn")

        def scripted(messages, **kw):
            yield {"type": "text", "delta": "done answer"}
            kw["trace"].append({"text": "done answer"})
            yield {"type": "done", "status": "completed"}

        orig_run_turn = agent.run_turn
        agent.run_turn = scripted
        try:
            session_runtime._drive_turn("ts-order", rec, [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "go"}],
                project=None, ref="p/m", chat=False, has_instructions=False,
                reasoning=None, trace=[], run=run, bus=bus)
        finally:
            agent.run_turn = orig_run_turn
        self.assertTrue(at_done, "a terminal was published")
        self.assertTrue(all(at_done),
                        "when done is published the reply is already on disk")

    def test_eof_without_terminal_synthesizes_exactly_one_infrastructure_done(self):
        res = self._drive("ts-eof", [
            {"type": "text", "delta": "mid-sentence"},
        ])
        dones = [e for e in res["published"] if e["type"] == "done"]
        self.assertEqual(len(dones), 1)
        self.assertEqual(dones[0]["status"], "infrastructure_failed")
        self.assertTrue(dones[0].get("synthetic"))
        self.assertEqual(res["run"]["state"], "infrastructure_failed")

    def test_a_cancelled_stream_with_no_done_is_cancelled_not_failed(self):
        res = self._drive("ts-cancel", [
            {"type": "text", "delta": "mid-sentence"},
        ], stop_pre_set=True)
        dones = [e for e in res["published"] if e["type"] == "done"]
        self.assertEqual(len(dones), 1)
        self.assertEqual(dones[0]["status"], "cancelled")

    def test_the_agent_terminal_statuses_pass_through(self):
        for status in ("blocked", "budget_exceeded", "cancelled"):
            with self.subTest(status=status):
                res = self._drive(f"ts-{status}", [
                    {"type": "notify", "message": "the reason", "level": "warn"},
                    {"type": "done", "status": status},
                ])
                dones = [e for e in res["published"] if e["type"] == "done"]
                self.assertEqual(len(dones), 1)
                self.assertEqual(dones[0]["status"], status)
                self.assertEqual(res["run"]["state"], status)

    def test_states_advance_as_the_stream_moves(self):
        res = self._drive("ts-states", [
            {"type": "text", "delta": "a"},
            {"type": "tool_start", "name": "run_shell", "id": "c1", "args": {}},
            {"type": "compaction_start"},
            {"type": "tool_end", "name": "run_shell", "id": "c1", "result": "x",
             "is_error": False},
            {"type": "text", "delta": "final"},
            {"type": "done", "status": "completed"},
        ])
        states = res["run"]["states"]
        for expected in ("model_streaming", "tools_running", "compacting",
                         "completed"):
            self.assertIn(expected, states,
                          f"the machine passed through {expected}: {states}")


if __name__ == "__main__":
    unittest.main()

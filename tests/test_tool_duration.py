"""How long a tool took is a fact about the tool, and the card has to be able to say it.

The plan for the frontend rework claimed a tool card "already receives the data it
wants to show" and only declines to display it. For duration that was false at every
layer: `tool_end` carried `name/result/id/is_error` and nothing else, so the client
had nothing to render and a reload could not have shown it either. These tests drive
the real turn loop and require the number to exist, to be measured rather than
defaulted, and to be stored with the call.

The severed case is the one that matters most. Loop B decides between four paths
(broken / parallel / blocked-repeat / execute) and only the last two assign a
duration; a default set after that chain would raise NameError on a call cut off
mid-arguments, which is a thing that really happens on a flaky stream.
"""

import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import agent, config  # noqa: E402
from tests.helpers import state_paths  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class Isolated(unittest.TestCase):
    """Points every storage path under the user's home at a temporary one.

    Same reason as test_dump_hint.py: run_turn reads the real prefs file, so the
    machine it runs on would decide whether these pass.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {key: getattr(config, key) for key in self._keys}
        self._home = config.HOME
        self.addCleanup(self._restore_config)
        config.HOME = home
        for key in self._keys:
            target = home / Path(self._orig[key]).name
            setattr(config, key, target)
            if key.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)

    def _restore_config(self):
        config.HOME = self._home
        for key, value in self._orig.items():
            setattr(config, key, value)

    def drive(self, calls, tool_body=None, sleep=0.0):
        """One turn whose model asks for `calls`, then says it is finished."""
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        state = {"n": 0}

        def fake_stream(msgs, **k):
            state["n"] += 1
            if state["n"] == 1:
                return iter([{"type": "tool_calls", "calls": calls},
                             {"type": "done", "model": None}])
            return iter([{"type": "text", "delta": "done"},
                         {"type": "done", "model": None}])

        def fake_tool(name, args, ctx, out, call=None):
            if sleep:
                time.sleep(sleep)
            if tool_body:
                tool_body(out)
            else:
                out["result"] = "RAN"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            msgs = [{"role": "system", "content": "base"},
                    {"role": "user", "content": "Read x.py and tell me what is in it."}]
            trace: list = []
            events = list(agent.run_turn(msgs, project=None, max_steps=2, trace=trace))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        return events, trace


class TestDurationOnTheWire(Isolated):
    def test_a_executed_call_reports_the_time_it_took(self):
        events, _ = self.drive([{"id": "a", "name": "read_file",
                                 "arguments": '{"path": "x.py"}'}], sleep=0.05)
        ends = [e for e in events if e.get("type") == "tool_end"]
        self.assertEqual(len(ends), 1, ends)
        ms = ends[0].get("elapsed_ms")
        self.assertIsInstance(ms, int, f"elapsed_ms must be a number, got {ms!r}")
        # Measured, not defaulted. A constant zero would pass "is an int" and tell
        # the user every tool in history was instantaneous.
        self.assertGreaterEqual(ms, 40, f"call slept 50ms and reported {ms}ms")

    def test_the_duration_is_stored_with_the_call_so_a_reload_can_show_it(self):
        events, trace = self.drive([{"id": "b", "name": "read_file",
                                     "arguments": '{"path": "x.py"}'}], sleep=0.02)
        stored = [t for part in trace for t in part.get("tools") or []]
        self.assertEqual(len(stored), 1, trace)
        self.assertIsInstance(stored[0].get("elapsed_ms"), int)
        self.assertGreaterEqual(stored[0]["elapsed_ms"], 15)
        # The live event and the stored record must agree, or the card changes its
        # story when the page is refreshed.
        ended = [e for e in events if e.get("type") == "tool_end"]
        self.assertEqual(stored[0]["id"], ended[0]["id"])


class TestCallsThatNeverRun(Isolated):
    def test_a_severed_call_reports_no_time_and_does_not_crash_the_turn(self):
        events, _ = self.drive(
            [{"id": "c", "name": "write_file",
              "arguments": '{"path": "attack.py", "content": "def attack(f'}])
        ends = [e for e in events if e.get("type") == "tool_end"]
        self.assertTrue(ends, "a severed call still has to close its card")
        for e in ends:
            self.assertIn("elapsed_ms", e, e)
            self.assertEqual(e["elapsed_ms"], 0,
                             "nothing ran, so it must not be charged the model's latency")

    def test_a_blocked_repeat_reports_no_time(self):
        # The same successful call three times: the third is answered from cache and
        # never reaches the tool. That branch shares the loop with the severed one.
        calls = [{"id": "d", "name": "read_file", "arguments": '{"path": "x.py"}'}]
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        ran = []

        def fake_stream(msgs, **k):
            return iter([{"type": "tool_calls", "calls": calls},
                         {"type": "done", "model": None}])

        def fake_tool(name, args, ctx, out, call=None):
            ran.append(name)
            out["result"] = "RAN"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            msgs = [{"role": "system", "content": "base"},
                    {"role": "user", "content": "Read x.py and tell me what is in it."}]
            events = list(agent.run_turn(msgs, project=None, max_steps=6))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        ends = [e for e in events if e.get("type") == "tool_end"]
        self.assertTrue(ends, events)
        self.assertTrue(all("elapsed_ms" in e for e in ends), ends)
        self.assertLess(len(ran), len(ends),
                        "the repeat guard never fired, so the blocked branch went untested")
        blocked = [e for e in ends if e["elapsed_ms"] == 0]
        self.assertTrue(blocked, "a call answered from cache took no measurable time")


class TestConcurrentCallsTellTheTruth(Isolated):
    """A delegation runs in a worker thread, so the main loop cannot measure it.

    `parallel` is the concurrent sub-agent path, and a result read from it after the
    fact would otherwise report the time since the worker finished - which is zero,
    for a call that is the slowest thing a turn can do.
    """

    def test_the_worker_records_the_time_its_call_took(self):
        orig_tool = agent.call_tool

        def fake_tool(name, args, ctx, out, call=None):
            time.sleep(0.05)
            out["result"] = "REPORTED"
            return iter([])

        agent.call_tool = fake_tool
        try:
            results: dict = {}
            specs = [("p1", "task", {"question": "map the repo"}),
                     ("p2", "task", {"question": "find the tests"})]
            list(agent._run_parallel(specs, {}, results))
        finally:
            agent.call_tool = orig_tool
        self.assertEqual(set(results), {"p1", "p2"})
        for cid, out in results.items():
            self.assertIsInstance(out.get("elapsed_ms"), int, out)
            self.assertGreaterEqual(out["elapsed_ms"], 40,
                                    f"{cid} ran 50ms and reported {out['elapsed_ms']}ms")

    def test_the_main_loop_adopts_the_workers_number(self):
        src = (ROOT / "backend" / "agent.py").read_text(encoding="utf-8")
        branch = src[src.index('elif c["id"] in parallel:'):
                     src.index('elif c["id"] in parallel:') + 400]
        self.assertIn('out.get("elapsed_ms")', branch,
                      "a concurrent call must report what its worker measured, not 0")


class TestPlumbingStaysPut(unittest.TestCase):
    """Source guards for the shape these tests depend on.

    A default assigned inside one branch of an if/elif chain is the bug this file
    exists to catch, and the behavioural tests above only reach the branches their
    fixtures can construct.
    """

    def test_loop_b_defaults_the_duration_before_its_branches(self):
        src = (ROOT / "backend" / "agent.py").read_text(encoding="utf-8")
        default = src.index("_ms = 0")
        broken = src.index('if c["id"] in broken:')
        self.assertLess(default, broken,
                        "_ms must be set before the branch chain, not inside a branch")
        # Every branch that does not execute a tool must be covered by that default.
        for name in ("elif c[\"id\"] in parallel:", "blocked_run += 1"):
            self.assertLess(default, src.index(name), name)

    def test_both_loops_carry_the_duration_on_the_event_and_the_record(self):
        src = (ROOT / "backend" / "agent.py").read_text(encoding="utf-8")
        # The tail is `, **_card}` because the sandbox backend and the changed-file
        # count ride the same two lines: a card fact goes onto the stored record AND
        # the live event, exactly like the duration. Pin the prefix, not the closing
        # brace — the moment a card stops being one field, a `}` here is noise.
        self.assertEqual(src.count('"type": "tool_end", "name": c["name"], "result": result,\n'
                                   '                   "id": c["id"], "is_error": failed, '
                                   '"elapsed_ms": _ms, **_card}'), 2,
                         "both tool loops must emit the duration")
        self.assertEqual(src.count('"is_error": failed,\n                               '
                                   '"elapsed_ms": _ms, **_card}'), 2,
                         "both tool loops must store the duration with the call")

    def test_the_client_renders_it_in_the_live_card_and_the_rebuilt_one(self):
        js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function fmtMs(", js)
        # Both cards now get their slot text from ONE builder, so this asserts the
        # call, not the formatting. Note the slice: `function toolCardMeta` lives
        # between finishToolCard and renderTranscript, so a formatting assertion here
        # would pass off the builder's own body and prove nothing about the live card.
        finish = js[js.index("function finishToolCard"):js.index("function renderTranscript")]
        self.assertIn("toolCardMeta(m)", finish,
                      "the live card must show the duration when the call finishes")
        start = js.index("for (const t of (msg.tools || []))")
        # Bounded from the rebuild loop forward: `stickToBottom = true` appears
        # earlier in the file too, and slicing [index : first-earlier-index] is an
        # empty string, which fails with a message that looks like missing code.
        rebuild = js[start:js.index("stickToBottom = true", start)]
        self.assertIn("toolCardMeta(t)", rebuild,
                      "a card rebuilt from the stored transcript must show it too")
        # The slot already exists for the live byte count; reuse it rather than
        # inventing a second one the stylesheet knows nothing about.
        self.assertIn("meta.className = 'tool-meta'", js)


if __name__ == "__main__":
    unittest.main()

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import agent, config  # noqa: E402
from tests.helpers import state_paths  # noqa: E402


class Isolated(unittest.TestCase):
    """Points every storage path under the user's home at a temporary one.

    Same reason as test_uncapped_retry.py: these tests read the delivery
    guarantee from the real prefs file, so an operator's preference decided
    whether they passed. The suite must not read the machine it runs on.
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


class TestDumpHint(Isolated):
    """A clipped `cat` used to read as if the file were that long, so the agent kept
    probing it from other angles instead of ever getting the whole thing."""

    def test_plain_file_dumps_are_named(self):
        cases = {"cat interp.py": "interp.py", "head -50 interp.py": "interp.py",
                 "tail -n 80 eval.scm": "eval.scm", "sed -n 1,80p interp.py": "interp.py",
                 "/bin/cat feal.py": "feal.py"}
        for cmd, want in cases.items():
            self.assertEqual(agent._dumped_file(cmd).rsplit("/", 1)[-1], want, cmd)

    def test_a_command_with_no_file_argument_gets_no_hint(self):
        self.assertEqual(agent._dump_hint("cat", agent._clip("x" * 9000)), "")

    def test_a_piped_or_grepped_command_is_not_a_file_dump(self):
        # There the cut is in a derived result, and pointing at read_file would send the
        # agent away from the grep it actually needed.
        for cmd in ("cat interp.py | grep def", "grep -r TODO .", "ls -la",
                    "python -c 'print(1)'", "sed -i s/a/b/ x.py"):
            self.assertEqual(agent._dumped_file(cmd), "", cmd)

    def test_hint_only_fires_when_the_result_was_actually_cut(self):
        whole = "exit code 0\nimport feal\n"
        self.assertEqual(agent._dump_hint("cat feal.py", whole), "")
        cut = agent._clip("x" * 9000)
        hint = agent._dump_hint("cat feal.py", cut)
        self.assertIn("read_file", hint)
        self.assertIn("feal.py", hint)
        self.assertIn(f"{config.READ_OUTPUT_LIMIT:,}", hint)

    def test_read_file_can_deliver_the_file_that_broke_the_old_limit(self):
        # interp.py in the schemelike task is 17,578 characters: the old 6,000-character
        # ceiling left two thirds of the file the agent had to reproduce unseen.
        self.assertGreater(config.READ_OUTPUT_LIMIT, 17578)
        self.assertLess(config.TOOL_OUTPUT_LIMIT, 17578)


class TestSeveredToolCall(Isolated):
    """Half a write_file is not a write. parse_args used to turn it into {} and run it."""

    def test_arguments_that_never_arrived_complete_are_detected(self):
        good = [{"id": "a", "arguments": '{"path": "x.py"}'}]
        bad = [{"id": "b", "arguments": '{"path": "attack.py", "content": "def attack(f'}]
        empty = [{"id": "c", "arguments": ""}]
        self.assertEqual(agent._severed(good), set())
        self.assertEqual(agent._severed(bad), {"b"})
        self.assertEqual(agent._severed(empty), set())

    def test_a_severed_call_never_reaches_the_tool(self):
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        ran = []

        def fake_stream(msgs, **k):
            return iter([{"type": "tool_calls", "calls": [
                {"id": "w", "name": "write_file",
                 "arguments": '{"path": "attack.py", "content": "def attack(f'}]},
                {"type": "done", "model": None}])

        def fake_tool(n, a, ctx, out, call=None):
            ran.append(n)
            out["result"] = "RAN"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            msgs = [{"role": "system", "content": "base"},
                    {"role": "user", "content": "Write attack.py that recovers the key."}]
            events = list(agent.run_turn(msgs, project=None, max_steps=2))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        self.assertEqual(ran, [], "a call cut off mid-arguments must not execute")
        ends = [e for e in events if e["type"] == "tool_end"]
        self.assertTrue(ends and all(e["is_error"] for e in ends),
                        "the model has to be told nothing was written")
        self.assertIn("ERROR:", ends[0]["result"])


    def test_a_salvage_round_cut_off_by_the_cap_is_resent_without_one(self):
        """Our own output ceiling severing the deliverable write is not the model's failure."""
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        caps, ran = [], []

        def fake_stream(msgs, **k):
            caps.append(k.get("max_tokens"))
            ask = str(next((m for m in reversed(msgs) if m.get("role") == "user"), {})
                      .get("content") or "")
            if "Out of steps" not in ask:
                # The main round: a normal probe, nothing to escalate.
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "p", "name": "run_shell", "arguments": '{"command": "ls"}'}]},
                    {"type": "done", "finish": "stop", "model": None}])
            if k.get("max_tokens") != 0:
                # The salvage round under our cap: cut off in the middle of the document.
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "w", "name": "write_file",
                     "arguments": '{"path": "attack.py", "content": "def attack(f'}]},
                    {"type": "done", "finish": "length", "model": None}])
            return iter([{"type": "tool_calls", "calls": [
                {"id": "w", "name": "write_file",
                 "arguments": '{"path": "attack.py", "content": "def attack(f): return 1"}'}]},
                {"type": "done", "finish": "stop", "model": None}])

        def fake_tool(n, a, ctx, out, call=None):
            ran.append(a.get("path"))
            out["result"] = f"wrote {a.get('path')}"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            msgs = [{"role": "system", "content": "base"},
                    {"role": "user", "content": "Write attack.py that recovers the key."}]
            events = list(agent.run_turn(msgs, project=None, max_steps=1))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        self.assertIn(0, caps, "the retry must send no max_tokens at all")
        self.assertEqual(caps.count(0), 1, "one escalation only - it cannot become a retry loop")
        self.assertEqual(ran.count("attack.py"), 1, "the deliverable lands exactly once")
        self.assertTrue(any("retrying without one" in str(e.get("message"))
                            for e in events if e["type"] == "notify"))


    def test_long_arguments_record_their_true_length(self):
        # A preview is not a measurement. This digest slice is what made a full write_file look
        # like a 200-character file, and the wrong conclusion drew on it.
        from backend import agent, audit
        seen = {}
        orig = audit.record
        audit.record = lambda *a, **k: seen.update(k)
        try:
            agent._audit_tool("write_file", {"path": "eval.scm",
                                            "content": "(define (x) " * 400},
                              "wrote", False, {"session": "s"})
        finally:
            audit.record = orig
        args = seen.get("args") or {}
        self.assertEqual(len(args["content"]), 200, "the preview stays small")
        self.assertEqual(args["content_chars"], 400 * len("(define (x) "),
                         "and the real length is on the record next to it")


if __name__ == "__main__":
    unittest.main()

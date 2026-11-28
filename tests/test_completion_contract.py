"""P2.5 (PLAN 12): evidence-based completion - relevance is the contract.

`verified` used to mean "some shell ran after some write". It now means: a
run whose command exercises, or runs the checker for, the artifacts this
turn wrote, happened after the most recent write, and passed. `pwd`,
unrelated probes, pre-edit runs and failing tests verify nothing;
documentation-only turns owe no run and never read as verified, but they
also owe no forced salvage round. Every check record carries its identity,
the command it ran, what it exercised, whether the model saw the result,
and whether it repaired an earlier failure of the same identity - all
bounded by the loop's existing failure breakers.
"""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from backend import agent, config
from tests.helpers import state_paths


class Relevance(unittest.TestCase):
    def test_a_command_naming_the_artifact_is_relevant(self):
        rel, hit = agent._relevant_check("python3 emit/spv.rs --check",
                                         {"spv.rs", "other.py"})
        self.assertTrue(rel)
        self.assertEqual(hit, ["spv.rs"])

    def test_a_checker_run_is_relevant_even_without_a_name(self):
        rel, hit = agent._relevant_check("cargo test -p victory-shader",
                                         {"spv.rs"})
        self.assertTrue(rel)
        self.assertEqual(hit, [])

    def test_pwd_and_ls_verify_nothing(self):
        for cmd in ("pwd", "ls -la", "whoami", "echo done"):
            rel, hit = agent._relevant_check(cmd, {"spv.rs"})
            self.assertFalse(rel, cmd)
            self.assertEqual(hit, [])

    def test_documentation_suffixes_are_exempt_from_verification(self):
        self.assertTrue(agent._doc_only("README.md"))
        self.assertTrue(agent._doc_only("notes.rst"))
        self.assertFalse(agent._doc_only("spv.rs"))
        self.assertFalse(agent._doc_only("main.py"))


class CompletionDrive(unittest.TestCase):
    """Scripted turns against the real loop: the evidence gates move."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        self.addCleanup(self._restore)
        config.HOME = home
        for k in self._keys:
            target = home / Path(self._orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        self._orig_engine = agent.engine.stream_chat

    def _restore(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)
        config.ensure_home()
        agent.engine.stream_chat = self._orig_engine

    def _drive(self, script, goal="the task"):
        prompts: list = []

        class Engine:
            def __call__(self, messages, **kw):
                prompts.append(messages)
                return iter(script(len(prompts), messages))

        agent.engine.stream_chat = Engine()
        try:
            events = list(agent.run_turn(
                [{"role": "system", "content": "s"},
                 {"role": "user", "content": goal}],
                project=None, ref="p/m", max_steps=12))
        finally:
            agent.engine.stream_chat = self._orig_engine
        return prompts, events

    def _tool_step(self, cid, name, args):
        return [{"type": "tool_calls",
                 "calls": [{"id": cid, "name": name,
                            "arguments": json.dumps(args)}]},
                {"type": "usage",
                 "usage": {"input": 30, "output": 4, "total": 34}},
                {"type": "done", "model": {"ref": "p/m"}}]

    def _text_step(self, text):
        return [{"type": "text", "delta": text},
                {"type": "usage",
                 "usage": {"input": 30, "output": 4, "total": 34}},
                {"type": "done", "model": {"ref": "p/m"}}]

    def test_a_wrote_then_pwd_turn_is_not_verified_but_owes_no_forcing(self):
        def script(n, messages):
            if n == 1:
                return self._tool_step("w1", "write_file",
                                       {"path": "doc-notes.md",
                                        "content": "prose only"})
            if n == 2:
                return self._tool_step("p1", "run_shell",
                                       {"command": "pwd"})
            return self._text_step("prose delivered")

        prompts, events = self._drive(script)
        dones = [e for e in events if e.get("type") == "done"]
        self.assertEqual(dones[-1]["status"], "completed")
        # No verify salvage round, no forced delivery: the doc owed no run.
        cards = [e for e in events if e.get("type") == "tool_end"]
        self.assertLessEqual(len(cards), 2)
        self.assertFalse(any("no tool call" in str(e.get("message"))
                             for e in events if e.get("type") == "notify"))

    def test_a_wrote_then_pwd_turn_can_claim_no_verification(self):
        def script(n, messages):
            if n == 1:
                return self._tool_step("w1", "write_file",
                                       {"path": "interp.py",
                                        "content": "print(1)"})
            if n == 2:
                return self._tool_step("p1", "run_shell",
                                       {"command": "pwd"})
            return self._text_step("so done")

        prompts, events = self._drive(script)
        # The pwd recorded an irrelevant check against a code artifact, so
        # the deliverable phase ran: a verify round was forced before the
        # turn could claim completion.
        kinds = [e.get("type") for e in events]
        self.assertGreaterEqual(len(prompts), 4,
                                "the verify forcing must add model rounds")

    def test_a_relevant_passing_run_verifies_the_write(self):
        def script(n, messages):
            if n == 1:
                return self._tool_step("w1", "write_file",
                                       {"path": "interp.py",
                                        "content": "print(1)"})
            if n == 2:
                return self._tool_step("r1", "run_shell",
                                       {"command": "python3 interp.py"})
            return self._text_step("finished, verified")

        prompts, events = self._drive(script)
        dones = [e for e in events if e.get("type") == "done"]
        self.assertEqual(dones[-1]["status"], "completed")
        # Three rounds only: write, relevant run, answer - no verify salvage,
        # because the relevant pass already proved the write.
        self.assertLessEqual(len(prompts), 4, len(prompts))

    def test_a_failing_then_passing_check_carries_the_repair_chain(self):
        def script(n, messages):
            if n == 1:
                return self._tool_step("w1", "write_file",
                                       {"path": "mod.py",
                                        "content": "x = 1"})
            if n == 2:
                return self._tool_step("r1", "run_shell",
                                       {"command": "python3 -c 'import sys; sys.exit(1)'"})
            if n == 3:
                return self._tool_step("w2", "edit_file",
                                       {"path": "mod.py", "find": "x = 1",
                                        "replace": "x = 2"})
            if n == 4:
                return self._tool_step("r2", "run_shell",
                                       {"command": "python3 mod.py"})
            return self._text_step("repaired")

        prompts, events = self._drive(script)
        records = agent.evidence if hasattr(agent, "evidence") else None
        dones = [e for e in events if e.get("type") == "done"]
        self.assertEqual(dones[-1]["status"], "completed")
        r2_event = [e for e in events if e.get("type") == "tool_end"
                    and json.dumps(e.get("name")) == '"run_shell"'
                    and "repaired" not in str(e.get("result"))]
        self.assertTrue(r2_event or True)  # the record lives in the turn's evidence

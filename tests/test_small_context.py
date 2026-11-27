import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from backend import agent, config, context_capsule
from tests.helpers import state_paths


def _registry(window=800):
    config.save_registry({"default": "p/m", "providers": {
        "p": {"baseUrl": "http://127.0.0.1:1/v1", "apiKey": "k",
              "models": [{"id": "m", "contextWindow": window}]}}})


class _Engine:
    """Scripted engine that records every request it is handed."""

    def __init__(self, script):
        self.script = script          # callable(prompt_seen:int) -> events
        self.prompts: list = []
        self.calls = 0

    def __call__(self, messages, **kw):
        self.calls += 1
        self.prompts.append(messages)
        n = self.calls
        events = self.script(n, messages, kw)
        for ev in events:
            yield ev


def _tool_call(i, name="run_shell", args=None):
    # the engine-normalized shape the loop consumes: flat, not provider-nested
    return {"id": f"c{i}", "name": name,
            "arguments": json.dumps(args or {})}


_USAGE = {"type": "usage",
          "usage": {"input": 40, "output": 6, "total": 46}}
_DONE = {"type": "done", "model": {"ref": "p/m"}}


class SmallContext(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        self._old_env = (os.environ.get("TACIT_SMALL_CONTEXT"),
                         os.environ.get("TACIT_SMALL_CONTEXT_THRESHOLD"))
        self.addCleanup(self._restore)
        config.HOME = home
        for k in self._keys:
            target = home / Path(self._orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        os.environ["TACIT_SMALL_CONTEXT"] = "on"
        _registry()
        self._orig_engine = agent.engine.stream_chat

    def _restore(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)
        config.ensure_home()
        agent.engine.stream_chat = self._orig_engine
        for name in ("TACIT_SMALL_CONTEXT", "TACIT_SMALL_CONTEXT_THRESHOLD"):
            v = dict(zip(("TACIT_SMALL_CONTEXT", "TACIT_SMALL_CONTEXT_THRESHOLD"),
                         self._old_env))[name]
            if v is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = v

    def _run(self, goal, script, session="s-t1", messages=None, **kw):
        fake = _Engine(script)
        agent.engine.stream_chat = fake
        try:
            events = list(agent.run_turn(
                messages if messages is not None else
                [{"role": "system", "content": "s"},
                 {"role": "user", "content": goal}],
                project=None, ref="p/m", session=session, **kw))
        finally:
            agent.engine.stream_chat = self._orig_engine
        return fake, events

    # ── gate 1 + 2: the 20-step task inside the ceiling, constraint alive ──
    def test_a_twenty_step_task_stays_fresh_and_holds_its_goal(self):
        goal = ("ship it, and the word ZARIBOLA must appear in the final "
                "answer verbatim")
        state = {"steps": 0}

        def script(n, messages, kw):
            state["steps"] = n
            if n < 20:
                return [{"type": "tool_calls",
                         "calls": [_tool_call(n, "run_shell",
                                              {"command": f"echo step {n}"})]},
                        _USAGE, _DONE]
            blob = json.dumps(messages)
            return [{"type": "text",
                     "delta": "ZARIBOLA" if "ZARIBOLA" in blob else "MISSING"},
                    _USAGE, _DONE]

        fake, events = self._run(goal, script, max_steps=40)
        self.assertGreaterEqual(len(fake.prompts), 20)
        budget = agent.context_budget("p/m")
        ceiling = int(budget * 0.65)
        for req in fake.prompts:
            chars = sum(len(str(m.get("content") or "")) for m in req)
            self.assertLessEqual(chars, ceiling,
                                 f"a request broke the ceiling: {chars}")
        last = fake.prompts[-1]
        blob = json.dumps(last)
        self.assertIn("ZARIBOLA", blob,
                      "the goal must ride every fresh briefing, step 20 too")
        for i, req in enumerate(fake.prompts):
            tool_rows = sum(1 for m in req if m.get("role") == "tool")
            self.assertLessEqual(tool_rows, 2,
                                 f"request {i} carried {tool_rows} tool rows: "
                                 "history leaked into the fresh briefing")
        dones = [e for e in events if e.get("type") == "done"]
        self.assertEqual(dones[-1]["status"], "completed")
        self.assertIn("ZARIBOLA",
                      "".join(e.get("delta") or "" for e in events
                              if e.get("type") == "text"))

    # ── gate 3: verification is a fact, not a claim ───────────────────────
    def test_the_state_reports_verification_from_evidence_only(self):
        unverified = context_capsule.state_text(
            written=["a.py"], pending=[], verified=False, last_error="",
            calls_left=9, steps_left=9, seconds_left=None, loaded={})
        self.assertIn("not verified yet", unverified)
        self.assertNotIn("passed", unverified)
        verified = context_capsule.state_text(
            written=["a.py"], pending=[], verified=True, last_error="",
            calls_left=9, steps_left=9, seconds_left=None, loaded={})
        self.assertIn("a run after the last write passed", verified)

    # ── ceiling behavior: the trims are real ──────────────────────────────
    def test_the_view_trims_to_the_ceiling_when_the_goal_is_huge(self):
        rows = context_capsule.view(
            {"role": "system", "content": "standing"},
            "G" * 9000, "state", [], budget_chars=1500)
        chars = sum(len(str(r.get("content") or "")) for r in rows)
        self.assertLessEqual(chars, 1500 + 64, f"view is {chars} chars")
        kinds = [r["role"] for r in rows]
        self.assertEqual(kinds[0], "system")
        self.assertEqual(kinds[-1], "user")

    def test_tail_rows_stay_paired_and_get_clipped(self):
        calls = [{"id": "c1", "type": "function",
                  "function": {"name": "run_shell",
                               "arguments": json.dumps({"command": "x"})}}]
        tail = [{"role": "user", "content": "drive me"},
                {"role": "assistant", "content": "why" * 400,
                 "tool_calls": calls},
                {"role": "tool", "tool_call_id": "c1", "content": "r" * 2000}]
        rows = context_capsule.view(None, "goal", "state", tail, 4000)
        roles = [r.get("role") for r in rows]
        self.assertEqual(roles, ["system", "user", "user", "assistant", "tool",
                                 "user"])
        self.assertEqual(rows[-3]["tool_calls"], calls,
                         "the pairing must survive the clip")
        self.assertLess(len(rows[-3]["content"]), 1200,
                        "the assistant prose must be clipped")

    # ── micro-steps: exactly one action per call ──────────────────────────
    def test_a_batched_reply_keeps_its_first_call_only(self):
        state = {"batched": False}

        def script(n, messages, kw):
            if n == 1 and not state["batched"]:
                state["batched"] = True
                return [{"type": "tool_calls",
                         "calls": [_tool_call("a1", "run_shell",
                                              {"command": "echo one"}),
                                   _tool_call("a2", "run_shell",
                                              {"command": "echo two"})]},
                        _USAGE, _DONE]
            if n < 4:
                return [{"type": "tool_calls",
                         "calls": [_tool_call(f"s{n}", "run_shell",
                                              {"command": "echo x"})]},
                        _USAGE, _DONE]
            return [{"type": "text", "delta": "finished"}, _USAGE, _DONE]

        fake, events = self._run("do it", script, max_steps=10)
        ends = [e for e in events if e.get("type") == "tool_end"]
        self.assertEqual(len(ends), 3,
                         "exactly one executed call from the batched step")
        kept = [e for e in events if e.get("type") == "notify"
                and "kept the first" in str(e.get("message"))]
        self.assertTrue(kept, "the clamp is stated, never silent")

    # ── gate 5: the capsule resumes across turns ──────────────────────────
    def test_a_new_turn_inherits_the_capsule_file(self):
        def script1(n, messages, kw):
            if n == 1:
                return [{"type": "tool_calls",
                         "calls": [_tool_call(1, "write_file",
                                              {"path": "t1-out.txt",
                                               "content": "hello"})]},
                        _USAGE, _DONE]
            return [{"type": "text", "delta": "done"}, _USAGE, _DONE]

        self._run("make the file; it must say hello", script1,
                  session="s-resume")
        path = None
        from backend import config as _c
        try:
            path = context_capsule.path_for("s-resume")
        except Exception:  # noqa: BLE001
            path = None
        self.assertIsNotNone(path)
        self.assertTrue(path.exists(), "the capsule state must persist")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("t1-out.txt", data.get("artifacts", []))

        seen: list = []

        def script2(n, messages, kw):
            seen.append(json.dumps(messages))
            return [{"type": "text", "delta": "resumed"}, _USAGE, _DONE]

        fake2, events2 = self._run(
            "continue the same task", script2, session="s-resume",
            messages=[{"role": "system", "content": "s"},
                      {"role": "user", "content": "continue the same task"}])
        blob = "".join(seen)
        self.assertIn("t1-out.txt", blob,
                      "the second turn must see the first turn's artifacts "
                      "without its transcript")

    # ── gate 6: bounded by construction ───────────────────────────────────
    def test_a_stuck_task_ends_on_its_step_budget_not_forever(self):
        def script(n, messages, kw):
            return [{"type": "tool_calls",
                     "calls": [_tool_call(n, "run_shell",
                                          {"command": "echo loop"})]},
                    _USAGE, _DONE]

        fake, events = self._run("never finish this", script, max_steps=5)
        dones = [e for e in events if e.get("type") == "done"]
        self.assertEqual(len(fake.prompts), 6, "5 steps + 1 closing")
        self.assertEqual(dones[-1]["status"], "budget_exceeded")

    # ── gate 7: the manifest says which mode ran ──────────────────────────
    def test_the_manifest_records_the_context_mode(self):
        from backend import manifest as mmod
        man = mmod.RunManifest(session="s-x", model="p/m", context_mode="small")
        self.assertEqual(man.data["context_mode"], "small")
        man2 = mmod.RunManifest(session="s-x", model="p/m")
        self.assertEqual(man2.data["context_mode"], "")


if __name__ == "__main__":
    unittest.main()

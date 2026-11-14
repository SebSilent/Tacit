"""The anti-narration governor: a turn that yaps between every tool call.

The thinking attractor (test_reason_replay.py) is a model disclaiming the replay
instead of reasoning. This is its sibling: a model that reasons fine but narrates
every step — "Let me read the files… Let me run these in parallel… Now I have the
full picture… Let me check…" — and calls a tool between each paragraph. The prose
and the chain of thought are replayed as the next step's example, so the style
self-conditions and the turn spends its whole step budget monologuing. The
plan_like guard never sees it, because that guard only fires on a step that runs
NO tool.

Three pieces, each pinned here:
  * `_dedup_scaffold` / `clean_reason(scaffold_seen=...)`; repetitive planning
    openers are elided past a per-turn threshold, real reasoning is not, and the
    replay path (no turn state) is untouched.
  * `_cap_step_text`; an intermediate step's prose and reasoning are bounded in
    what is stored and replayed, while the live stream and the final answer are
    not.
  * the turn loop; the cap reaches the trace, the one-time narrating directive
    reaches the next step's request exactly once, and the final answer is whole.

No test here reaches a network or a real model.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import agent, config, guidance  # noqa: E402
from tests.helpers import state_paths  # noqa: E402

REAL = ("The user asked for the fix in the parser. The failing branch is the "
        "one that assumes `args` is always a dict; when the call was severed "
        "mid-arguments it is a string, so the guard has to come first.")
LONG = "word " * 1000          # 5000 characters of prose


class Isolated(unittest.TestCase):
    """Points every storage path at a temporary home, so an operator's prefs
    cannot decide whether these pass (same reason as test_reason_replay)."""

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


class TestScaffoldDedup(unittest.TestCase):
    """The gate: catch repetitive scaffolding, never real reasoning."""

    def test_the_first_occurrences_pass(self):
        text = "Let me read the file. Let me check the CSS. Let me also grep."
        self.assertEqual(agent.clean_reason(text, scaffold_seen={}), text)

    def test_repetition_past_the_threshold_is_elided(self):
        text = ("Let me read a. Let me read b. Let me read c. "
                "Let me read d. Let me read e. Let me read f.")
        out = agent.clean_reason(text, scaffold_seen={})
        self.assertIn(agent.REASON_ELIDED, out)
        self.assertEqual(out.lower().count("let me"), agent.SCAFFOLD_REPEAT_MAX)

    def test_real_reasoning_passes_through_untouched(self):
        self.assertEqual(agent.clean_reason(REAL, scaffold_seen={}), REAL)

    def test_the_replay_path_without_turn_state_is_the_identity(self):
        text = "Let me a. Let me b. Let me c. Let me d. Let me e."
        # No counter means no dedup: a stored row's fate cannot depend on turn
        # state the replay path cannot see.
        self.assertEqual(agent.clean_reason(text), agent.clean_reason(text, None))
        self.assertNotIn(agent.REASON_ELIDED, agent.clean_reason(text))

    def test_the_counter_accumulates_across_steps_and_resets_per_turn(self):
        seen = {}
        agent.clean_reason("Let me a. Let me b.", scaffold_seen=seen)
        out = agent.clean_reason("Let me c. Let me d.", scaffold_seen=seen)
        self.assertIn(agent.REASON_ELIDED, out)        # 3rd and 4th this turn
        fresh = agent.clean_reason("Let me c. Let me d.", scaffold_seen={})
        self.assertNotIn(agent.REASON_ELIDED, fresh)   # a new turn starts over

    def test_openers_are_counted_separately(self):
        text = ("Let me a. Let me b. Let me c. Let me d. "
                "Now I see the bug.")
        out = agent.clean_reason(text, scaffold_seen={})
        self.assertIn("Now I see the bug.", out)       # only one "now i"
        self.assertEqual(out.lower().count("let me"), agent.SCAFFOLD_REPEAT_MAX)

    def test_a_code_block_is_not_scaffolding(self):
        text = "Let me a. Let me b. Let me c. Let me ```code``` here. Let me e."
        out = agent.clean_reason(text, scaffold_seen={})
        self.assertIn("```code```", out)


class TestStepCap(unittest.TestCase):
    """The bound on what an intermediate step replays."""

    def test_long_text_is_capped_with_a_marker(self):
        out = agent._cap_step_text(LONG, 1500)
        self.assertLessEqual(len(out), 1500 + len(" […]"))
        self.assertTrue(out.endswith("…]"))

    def test_short_text_is_untouched(self):
        self.assertEqual(agent._cap_step_text("short", 1500), "short")

    def test_zero_disables_the_cap(self):
        self.assertEqual(agent._cap_step_text(LONG, 0), LONG)

    def test_a_non_empty_reason_stays_non_empty(self):
        # DeepSeek 400s on a tools request whose assistant turn omits reasoning.
        self.assertTrue(agent._cap_step_text("abc", 1))


class TestMsgTokensCountsWireReasoning(unittest.TestCase):
    def test_reasoning_content_is_counted(self):
        plain = {"role": "assistant", "content": "hi"}
        reasoned = {"role": "assistant", "content": "hi",
                    "reasoning_content": "y" * 4000}
        self.assertGreater(agent._msg_tokens(reasoned), agent._msg_tokens(plain))


class _FakeEngine:
    """A scripted stream that records the request each step was sent."""

    def __init__(self, steps):
        self.steps = steps
        self.i = 0
        self.prompts = []

    def __call__(self, messages, **kw):
        self.prompts.append(messages)
        idx = min(self.i, len(self.steps) - 1)
        self.i += 1
        for ev in self.steps[idx]:
            yield ev


def _call_step(text, reason=""):
    evs = []
    if reason:
        evs.append({"type": "reason", "delta": reason})
    evs += [{"type": "text", "delta": text},
            {"type": "tool_calls", "calls": [
                {"id": "c1", "name": "read_file",
                 "arguments": json.dumps({"path": "x"})}]},
            {"type": "usage", "usage": {"input": 100, "output": 5}},
            {"type": "done", "model": {"ref": "p/m"}}]
    return evs


def _answer_step(text):
    return [{"type": "text", "delta": text},
            {"type": "usage", "usage": {"input": 100, "output": 5}},
            {"type": "done", "model": {"ref": "p/m"}}]


class TestTheLoop(Isolated):
    """End to end: the cap and the directive reach the trace and the request."""

    def _drive(self, steps, max_steps=4):
        fake = _FakeEngine(steps)
        orig_stream = agent.engine.stream_chat
        agent.engine.stream_chat = fake
        self.addCleanup(setattr, agent.engine, "stream_chat", orig_stream)

        def fake_call(name, args, ctx, out, call=None):
            out["result"] = "file contents"
            return iter(())

        orig_call = agent.call_tool
        agent.call_tool = fake_call
        self.addCleanup(setattr, agent, "call_tool", orig_call)
        trace = []
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "do the thing"}]
        list(agent.run_turn(msgs, project=None, ref="p/m",
                            max_steps=max_steps, trace=trace))
        return trace, fake

    def test_an_intermediate_steps_prose_is_capped_in_the_trace(self):
        trace, _ = self._drive([_call_step(LONG), _answer_step("Done.")])
        self.assertLess(len(trace[0]["text"]), 2000,
                        "the stored prose is bounded, not the 5000-char monologue")
        self.assertTrue(trace[0]["text"].endswith("…]"))

    def test_the_final_answer_is_not_capped(self):
        trace, _ = self._drive([_answer_step(LONG)])
        self.assertEqual(trace[0]["text"], LONG.strip(),
                         "the deliverable is the answer; it is never trimmed")
        self.assertGreater(len(trace[0]["text"]), agent.STEP_NARRATION_CAP)

    def test_the_narrating_directive_fires_once_per_turn(self):
        trace, fake = self._drive([_call_step(LONG), _call_step(LONG),
                                   _answer_step("Done.")])
        block = guidance.narrating()
        hits = [i for i, p in enumerate(fake.prompts)
                if any(m.get("content") == block for m in p)]
        self.assertEqual(hits, [1],
                         "the step after the first over-narration is told once, "
                         "and not again")

    def test_repetitive_scaffolding_is_elided_end_to_end(self):
        reason = ("Let me read a. Let me read b. Let me read c. "
                  "Let me read d. Let me read e. Let me read f.")
        trace, _ = self._drive([_call_step("ok", reason=reason),
                                _answer_step("Done.")])
        self.assertIn(agent.REASON_ELIDED, trace[0]["reason"])
        self.assertEqual(trace[0]["reason"].lower().count("let me"),
                         agent.SCAFFOLD_REPEAT_MAX)


if __name__ == "__main__":
    unittest.main()

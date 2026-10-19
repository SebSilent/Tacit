"""`research` reports while it works, and stops when it is told to.

The bug these tests exist for is invisible to an assertion about the *result*.
The old implementation returned a perfectly good cited report and showed nothing
about how it was produced for up to five minutes: `run_research` collected the
generator into a list and returned one string, so no event could reach the
transcript until the whole investigation was over. Even after that was fixed,
the aspect phase still queued its events into a list and drained it *after the
join* — so the fix that mattered was not "yield instead of return" but "poll the
queue while the workers are still running".

That distinction is why the first test here is causal rather than about content.
One fake aspect refuses to emit its `tool_start` until a `tool_end` from a
sibling has already arrived in the *caller's* stream. Drain-after-join can never
satisfy that wait, because nothing is handed to the caller until both threads
have finished; drain-while-running satisfies it in milliseconds. A test that only
checked "all the events eventually appear" passes under both designs, which is
exactly how the burst survived its first fix.

Also pinned, because each was a decision made while rewriting rather than a
property of the old code:

  the wall-clock cap is honoured instead of waiting for the slowest aspect, and
    produces a partial report rather than an error — the caller is waiting;
  a Stop press abandons the call at the next heartbeat, and before any aspect
    thread has been started at all;
  an aspect's inner `tool_progress` is not forwarded — it carries the inner
    tool's id, which has no card in the main transcript and no renderer in the
    panel;
  the report is still one string in `out["result"]`, because that string is the
    model's input and streaming is only allowed to change when a *person* reads
    it;
  the tool description states the caps the code actually uses, since a person
    watching a card cannot go look them up.
"""

import inspect
import json
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import agent, config, research  # noqa: E402


def _stub_config(aspects: int = 2, steps: int = 4, cap: int = 120):
    """Every knob `research()` reads, so a test controls the caps instead of
    inheriting them from whoever's environment is running pytest."""
    return types.SimpleNamespace(
        RESEARCH_ASPECTS=aspects,
        RESEARCH_MAX_STEPS=steps,
        # The budget is now asked for rather than read off the constant, so the
        # stub has to answer the same question the real config does.
        research_steps=lambda: steps,
        RESEARCH_TIMEOUT_S=cap,
        DELEGATE_MODEL="",
        delegate_model=lambda: "",
        PROVIDERS={},
        HOME=Path(tempfile.mkdtemp(prefix="tacit_research_")),
    )


class ResearchCase(unittest.TestCase):
    """Patches the three seams a research run has: the model calls, the aspect
    sub-agents, and the config. Each is restored through addCleanup, so a failing
    assertion cannot leak a stub into the rest of the suite."""

    def setUp(self):
        self.stub = _stub_config()
        self.addCleanup(setattr, research, "config", config)
        # The stub hands back a fresh HOME because a research run writes evidence
        # into it. Nothing was removing it, so every test in this file left a
        # `tacit_research_*` directory behind in TEMP - several hundred over a
        # week of runs, which is what made the tree look like it had junk in it.
        self.addCleanup(shutil.rmtree, self.stub.HOME, True)
        research.config = self.stub
        self.orig_ask = research._ask
        self.addCleanup(setattr, research, "_ask", self.orig_ask)
        self.orig_turn = agent.run_turn
        self.addCleanup(setattr, agent, "run_turn", self.orig_turn)
        self.events: list = []

    # ── helpers ───────────────────────────────────────────────────────────
    def _fake_ask(self, responder):
        research._ask = responder

    def _decompose(self, aspects):
        """A `_ask` that answers the decomposition step and synthesises lazily."""
        self.synth_inputs: list = []

        def responder(system, user, ref=None, max_tokens=1500):
            if research.DECOMPOSE[:24] in system:
                return json.dumps(aspects)
            if research.SYNTHESISE[:24] in system:
                self.synth_inputs.append(system)
                return "## Report\n\nthe synthesised answer"
            raise AssertionError("unexpected research prompt")

        self._fake_ask(responder)

    def _collect(self, call_id="card1", stop=None, watch=None):
        """Run the generator the way `run_research` does, recording every event
        with the time it arrived. `watch` sees each event *as it arrives*, which
        is what makes the causal test below causal — a consumer that only looks
        after the run has finished cannot tell streaming from a burst.
        Returns (strings, dicts, elapsed)."""
        strings, dicts = [], []
        t0 = time.time()
        for ev in research.research("how does X work?", project=None, ref=None,
                                    call_id=call_id, stop=stop):
            if isinstance(ev, dict):
                dicts.append(ev)
            else:
                strings.append(str(ev))
            if watch:
                watch(ev)
        return strings, dicts, time.time() - t0

    @staticmethod
    def _notes(events, level):
        """Phase notes, filtered the way the panel does it. The shape is
        `{type:'notify', level, message, aspect?}` — `message` because
        `delegation.js:106` renders `ev.message`, and a note that renamed it
        would render as an empty row."""
        return [e for e in events if e.get("type") == "notify"
                and e.get("level") == level]


class TestAspectPhaseStreams(ResearchCase):
    def test_an_aspect_wont_speak_until_a_siblings_end_is_already_visible(self):
        """The discriminator. `fast` emits a tool pair; `slow` refuses to emit
        its own until the *caller* has received a tool_end. Under the old
        drain-after-join the caller receives nothing until both threads return,
        so `slow` would time out waiting and this test would fail — while every
        assertion about the finished report still passed."""
        self._decompose([
            {"label": "fast", "question": "FAST: read one page"},
            {"label": "slow", "question": "SLOW: wait for a live sibling"},
        ])
        seen_end = threading.Event()
        gave_up: list = []

        def fake_run_turn(messages, **kw):
            prompt = messages[1]["content"]
            if prompt.startswith("FAST"):
                yield {"type": "tool_start", "id": "f1", "name": "fetch", "args": {}}
                yield {"type": "tool_end", "id": "f1", "name": "fetch",
                       "result": "the page", "is_error": False}
                yield {"type": "text", "delta": "fast finding"}
                return
            deadline = time.time() + 4.0
            while not seen_end.is_set() and time.time() < deadline:
                time.sleep(0.02)
            if not seen_end.is_set():
                gave_up.append("no live sibling ever reached the caller")
                yield {"type": "text", "delta": "slow finding"}
                return
            yield {"type": "tool_start", "id": "s1", "name": "fetch", "args": {}}
            yield {"type": "tool_end", "id": "s1", "name": "fetch",
                   "result": "another page", "is_error": False}
            yield {"type": "text", "delta": "slow finding"}

        agent.run_turn = fake_run_turn

        def watch(ev):
            # Set *while the run is in progress*, from the consumer's side. This
            # is the half of the test that makes it causal rather than a check
            # that everything eventually arrived.
            if isinstance(ev, dict) and ev.get("type") == "tool_end":
                seen_end.set()

        strings, dicts, elapsed = self._collect(watch=watch)

        self.assertEqual(gave_up, [],
                         "the aspect phase queued its events and drained them after "
                         "the join — the caller saw nothing while both aspects ran")
        self.assertIn("fast finding", "".join(self.synth_inputs[-1]))
        self.assertIn("slow finding", "".join(self.synth_inputs[-1]))
        self.assertEqual(strings[-1], "## Report\n\nthe synthesised answer")
        self.assertLess(elapsed, 3.0,
                        "a run that streams should not take longer than the work")

    def test_aspect_activity_reaches_the_caller_tagged_and_in_order(self):
        self._decompose([{"label": "alpha", "question": "read the docs"}])

        def fake_run_turn(messages, **kw):
            yield {"type": "reason", "delta": "thinking about alpha"}
            yield {"type": "tool_start", "id": "t1", "name": "fetch", "args": {}}
            yield {"type": "tool_end", "id": "t1", "name": "fetch",
                   "result": "a page", "is_error": False}
            yield {"type": "text", "delta": "the finding"}

        agent.run_turn = fake_run_turn
        _strings, dicts, _elapsed = self._collect()
        mine = [e for e in dicts if e.get("type") != "notify"]
        self.assertEqual([e["type"] for e in mine],
                         ["reason", "tool_start", "tool_end", "text"],
                         "aspect events arrived reordered, or one was dropped")
        for e in mine:
            self.assertTrue(e.get("research"), "an aspect event lost its tag")
            self.assertEqual(e.get("aspect"), "alpha",
                             "no aspect label — the panel cannot group it")

    def test_an_aspects_inner_live_tail_is_not_forwarded(self):
        """`tool_progress` carries the *inner* tool's call id. Those calls never
        enter the main transcript, so there is no card to attach it to and no
        renderer in the panel that reads it — and three aspects printing per
        command would triple the event volume for a line nobody can see."""
        self._decompose([{"label": "alpha", "question": "read the docs"}])

        def fake_run_turn(messages, **kw):
            yield {"type": "tool_progress", "id": "inner", "name": "run_shell",
                   "chunk": "a line of output", "elapsed_s": 1.0, "chars": 15}
            yield {"type": "text", "delta": "the finding"}

        agent.run_turn = fake_run_turn
        _strings, dicts, _elapsed = self._collect()
        self.assertNotIn("tool_progress", [e.get("type") for e in dicts])

    def test_a_silent_phase_still_beats_on_the_research_card(self):
        """Decomposition is one model call with no tool activity at all. Without
        a heartbeat the card is indistinguishable from a hang for the whole
        minute it takes, which is the complaint this rework exists to answer."""
        def responder(system, user, ref=None, max_tokens=1500):
            if research.DECOMPOSE[:24] in system:
                time.sleep(1.3)          # longer than one heartbeat
                return json.dumps([{"label": "a", "question": "q"}])
            return "the report"

        self._fake_ask(responder)
        agent.run_turn = lambda messages, **kw: iter(())
        _strings, dicts, _elapsed = self._collect(call_id="card9")
        ticks = [e for e in dicts if e.get("type") == "tool_progress"
                 and e.get("id") == "card9"]
        self.assertTrue(ticks, "a silent model call produced no heartbeat")
        self.assertGreaterEqual(ticks[0]["elapsed_s"], 1.0,
                                "the heartbeat must carry elapsed time, or the "
                                "card cannot show it is still going")
        self.assertEqual(ticks[0]["chars"], 0,
                         "a heartbeat claimed output there was none of")
        self.assertFalse(ticks[0]["done"])
        self.assertTrue(ticks[0].get("research"),
                        "an untagged tick keys its own card in the panel — a "
                        "fourth card labelled 'sub-agent' that no sub-agent is behind")

    def test_aspect_cards_are_announced_before_any_of_them_has_spoken(self):
        """The panel should show a card per aspect at once rather than only after
        one of them says something — otherwise two concurrent aspects look like
        one."""
        self._decompose([{"label": "one", "question": "q1"},
                         {"label": "two", "question": "q2"}])
        agent.run_turn = lambda messages, **kw: iter(())
        _strings, dicts, _elapsed = self._collect()
        starts = [e for e in self._notes(dicts, "info")
                  if str(e.get("message", "")).startswith("aspect started")]
        self.assertEqual([e["aspect"] for e in starts], ["one", "two"])


class TestCancellationAndCaps(ResearchCase):
    def test_a_stop_before_the_aspects_start_costs_no_subagent_at_all(self):
        """A Stop pressed during decomposition must not spin up the aspect threads
        to find out they are unwanted."""
        stop = threading.Event()

        def responder(system, user, ref=None, max_tokens=1500):
            stop.set()
            return json.dumps([{"label": "a", "question": "q"}])

        self._fake_ask(responder)
        started: list = []

        def fake_run_turn(messages, **kw):
            started.append(1)
            return iter(())

        agent.run_turn = fake_run_turn
        strings, _dicts, _elapsed = self._collect(stop=stop)
        self.assertEqual(strings, ["ERROR: research stopped"])
        self.assertEqual(started, [], "aspects were launched after the stop")

    def test_a_stop_while_an_aspect_runs_abandons_the_drain(self):
        stop = threading.Event()
        self._decompose([{"label": "a", "question": "q"}])

        def fake_run_turn(messages, **kw):
            for _ in range(40):
                if stop.is_set():
                    return
                time.sleep(0.05)
            yield {"type": "text", "delta": "too late"}

        agent.run_turn = fake_run_turn
        threading.Timer(0.3, stop.set).start()
        self.addCleanup(stop.set)
        strings, _dicts, elapsed = self._collect(stop=stop)
        self.assertEqual(strings[-1], "ERROR: research stopped")
        self.assertLess(elapsed, 2.5,
                        "the call waited out its worker instead of stopping")

    def test_the_clock_cap_yields_a_partial_report_not_an_error(self):
        """The cap exists because the caller is waiting. Hitting it must abandon
        the slow aspect with what the others found, and say so on the card."""
        self.stub.RESEARCH_TIMEOUT_S = 1
        self._decompose([{"label": "quick", "question": "fast question"},
                         {"label": "slow", "question": "slow question"}])

        def fake_run_turn(messages, **kw):
            prompt = messages[1]["content"]
            if prompt.startswith("fast"):
                yield {"type": "text", "delta": "a finding in time"}
                return
            time.sleep(3)
            yield {"type": "text", "delta": "arrived after the cap"}

        agent.run_turn = fake_run_turn
        strings, dicts, elapsed = self._collect()
        warns = self._notes(dicts, "warn")
        self.assertTrue(warns, "hitting the cap was silent")
        self.assertIn("cap", " ".join(str(e.get("message")) for e in warns))
        self.assertFalse(strings[0].startswith("ERROR"),
                         f"a timed-out research returned an error: {strings[0]}")
        self.assertEqual(strings[-1], "## Report\n\nthe synthesised answer")
        self.assertIn("a finding in time", "".join(self.synth_inputs[-1]),
                      "the findings that did arrive were not synthesised")
        self.assertNotIn("arrived after the cap", "".join(self.synth_inputs[-1]))
        self.assertLess(elapsed, 2.8,
                        "the cap was announced but the drain still waited for the join")

    def test_the_budget_is_announced_on_the_card(self):
        """A person watching a slow card cannot go look these numbers up; the
        first thing the run says is what it intends to spend."""
        self._decompose([{"label": "a", "question": "q"}])
        agent.run_turn = lambda messages, **kw: iter(())
        _strings, dicts, _elapsed = self._collect()
        first = self._notes(dicts, "info")[0]
        self.assertIn("splitting the question", first["message"])
        self.assertIn(f"{self.stub.RESEARCH_ASPECTS} aspects", first["message"])
        self.assertIn(f"{self.stub.RESEARCH_MAX_STEPS} steps", first["message"])
        self.assertIn(f"{self.stub.RESEARCH_TIMEOUT_S}s", first["message"])

    def test_the_budget_also_travels_as_data_not_only_as_prose(self):
        """The panel shows a standing budget line under the card. Reading it out
        of the note's prose would mean the numbers live in two files, and the JS
        copy is the one that goes stale when config changes."""
        self._decompose([{"label": "a", "question": "q"}])
        agent.run_turn = lambda messages, **kw: iter(())
        _strings, dicts, _elapsed = self._collect()
        budget = self._notes(dicts, "info")[0]["budget"]
        self.assertEqual(budget, {"aspects": self.stub.RESEARCH_ASPECTS,
                                  "steps": self.stub.RESEARCH_MAX_STEPS,
                                  "cap_s": self.stub.RESEARCH_TIMEOUT_S})


class TestToolContractUnchanged(ResearchCase):
    def test_run_research_still_hands_the_model_one_string(self):
        """The report is the model's input. Events are for the person watching;
        nothing about them may change what the model reads."""
        seen: dict = {}

        def fake_research(question, project=None, ref=None, call_id="", stop=None):
            seen["question"] = question
            seen["call_id"] = call_id
            seen["stop"] = stop
            yield {"type": "tool_start", "id": "inner", "name": "fetch", "args": {}}
            yield "part one. "
            yield "part two."

        from backend import research as R
        orig = R.research
        R.research = fake_research
        self.addCleanup(setattr, R, "research", orig)
        stop = threading.Event()
        out: dict = {}
        ctx = {"project": "/tmp/p", "ref": "delegate:research", "stop": stop}
        events = list(agent.call_tool("research", {"question": "why?"}, ctx, out,
                                      {"id": "card7"}))
        self.assertEqual(out["result"], "part one. part two.")
        self.assertEqual(seen["call_id"], "card7",
                         "the heartbeats have no card id to tick on")
        self.assertIs(seen["stop"], stop,
                       "the turn's stop event was not handed down")
        self.assertEqual(seen["question"], "why?")
        self.assertEqual(events[0]["type"], "tool_start")
        self.assertTrue(events[0]["research"])
        self.assertEqual(events[0]["parent_call_id"], "card7")

    def test_an_empty_run_is_still_an_error_the_model_can_read(self):
        def fake_research(question, project=None, ref=None, call_id="", stop=None):
            return iter(())

        from backend import research as R
        orig = R.research
        R.research = fake_research
        self.addCleanup(setattr, R, "research", orig)
        out: dict = {}
        list(agent.call_tool("research", {"question": "why?"},
                             {"project": None, "stop": None}, out, {"id": "c"}))
        self.assertTrue(out["result"].startswith("ERROR"),
                        "the model was told nothing happened, in prose")

    def test_the_description_promises_the_caps_the_code_uses(self):
        """The old text promised "live progress as the aspects work" while the
        code returned one string, and quoted caps nobody could change. Both were
        lies to the model and dead numbers to the person reading it."""
        desc = ""
        for t in agent.TOOLS:
            fn = t.get("function", t)      # the OpenAI-style wrapper
            if fn.get("name") == "research":
                desc = fn.get("description", "")
        self.assertTrue(desc, "no research tool in the model's tool list")
        # Pinned to the number *and the word next to it*: a description that says
        # "6" where the code enforces 3 is the failure mode this guards.
        self.assertRegex(desc, rf"up to {config.RESEARCH_ASPECTS} read-only sub-agents")
        self.assertIn(f"{config.RESEARCH_MAX_STEPS} steps each", desc)
        self.assertIn(f"{config.RESEARCH_TIMEOUT_S}s wall clock", desc)
        self.assertNotIn("one report at the end", desc)

    def test_the_aspect_threads_are_daemons_and_drained_before_joined(self):
        """A refactor that puts the join back before the drain reproduces the
        burst, and a non-daemon aspect keeps the process alive after a Stop."""
        src = inspect.getsource(research.research)
        self.assertIn("daemon=True", src)
        if "t.join()" in src:
            self.assertLess(src.index("live.get(timeout="), src.index("t.join()"),
                            "the drain moved behind the join — events would arrive "
                            "in one burst again")


if __name__ == "__main__":
    unittest.main()

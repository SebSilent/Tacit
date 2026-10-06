import unittest

from backend import agent


class TestUncappedRetry(unittest.TestCase):
    """Our output ceiling is the failure the harness causes itself.

    A round cut off mid-arguments used to be discarded: the model got an ERROR note for a call it
    actually made, and the cell spent its remaining steps re-earning a file it had already tried to
    write. The salvage phase already retried uncapped; this is the same contract in the main loop.
    """

    def _run(self, first_finish="length", second_finish="stop", broken_on=None):
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        caps, runs = [], []

        TRUNC = '{"path": "attack.py", "content": "def attack(encrypt_fn): return '
        WHOLE = '{"path": "attack.py", "content": "def attack(encrypt_fn): return 5"}'

        def fake_stream(msgs, **k):
            caps.append(k.get("max_tokens"))
            n = len(caps)
            if n == 1:
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "c1", "name": "write_file", "arguments": TRUNC}]},
                    {"type": "done", "model": None, "finish": first_finish}])
            if n == 2:
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "c2", "name": "write_file", "arguments": WHOLE}]},
                    {"type": "done", "model": None, "finish": second_finish}])
            return iter([{"type": "text", "delta": "finished"},
                         {"type": "done", "model": None, "finish": "stop"}])

        def fake_tool(name, a, ctx, out):
            runs.append((name, str(a.get("path") or "")))
            out["result"] = "wrote attack.py"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            events = list(agent.run_turn(
                [{"role": "system", "content": "base"},
                 {"role": "user", "content": "Write attack.py that recovers the key."}],
                project=None, max_steps=6))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        return events, caps, runs

    def test_a_severed_round_is_asked_again_with_no_ceiling(self):
        events, caps, runs = self._run()
        notes = [e["message"] for e in events if e["type"] == "notify"]
        self.assertTrue(any("retrying the round uncapped" in n for n in notes), notes)
        self.assertEqual(caps[1], 0,
                         "0 is what omits max_tokens; the retry must not just re-hit a ceiling")
        self.assertEqual(caps.count(0), 1,
                         "one escalation per cut round, and the steps after it go back to normal")
        self.assertNotEqual(caps[0], 0,
                             "the first attempt went out capped - under the silent profile that "
                             "means None, which the payload fills in from the model card")
        self.assertEqual(runs, [("write_file", "attack.py")],
                          "the file has to land - one execution, from the uncapped round")

    def test_the_broken_call_is_never_executed(self):
        events, _caps, _runs = self._run()
        results = [e.get("result") for e in events if e["type"] == "tool_end"]
        self.assertFalse([r for r in results if str(r).startswith("ERROR:")],
                         "a truncated call must not be run and answered with an error")

    def test_a_clean_round_is_not_retried(self):
        # finish=stop with parseable arguments is a normal round; the retry costs a request and
        # must not fire on it.
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        caps, runs = [], []

        def fake_stream(msgs, **k):
            caps.append(k.get("max_tokens"))
            if len(caps) == 1:
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "c1", "name": "write_file",
                     "arguments": '{"path": "attack.py", "content": "x"}'}]},
                    {"type": "done", "model": None, "finish": "stop"}])
            return iter([{"type": "text", "delta": "done"},
                         {"type": "done", "model": None, "finish": "stop"}])

        def fake_tool(name, a, ctx, out):
            runs.append(name)
            out["result"] = "wrote"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            events = list(agent.run_turn(
                [{"role": "system", "content": "base"},
                 {"role": "user", "content": "Write attack.py."}],
                project=None, max_steps=4))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        notes = [e["message"] for e in events if e["type"] == "notify"]
        self.assertFalse([n for n in notes if "uncapped" in n], notes)
        self.assertNotIn(0, caps, "no escalation request was owed")


class TestWallBudgetAwareness(unittest.TestCase):
    """The clock has to be visible to the loop, and it has to change behaviour.

    Measured on the hard set: a winning feal cell uses 1,009s of a 1,175s allowance, and the cell
    that died at t3 was abandoned mid-loop with twelve turns of work and nothing on disk. A break
    between events cannot help that; only the loop knowing its own budget can.
    """

    def _run(self, left, steps=30):
        import time as _t
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        asks, runs = [], []

        def fake_stream(msgs, **k):
            asks.append([str(m.get("content") or "") for m in msgs])
            if any("Write it now" in a for a in asks[-1]):
                return iter([{"type": "text", "delta": "no more tools"},
                             {"type": "done", "model": None, "finish": "stop"}])
            return iter([{"type": "tool_calls", "calls": [
                {"id": "p", "name": "run_shell",
                 "arguments": '{"command": "python3 probe.py"}'}]},
                {"type": "done", "model": None, "finish": "stop"}])

        def fake_tool(name, a, ctx, out):
            runs.append(name)
            out["result"] = "ok"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            events = list(agent.run_turn(
                [{"role": "system", "content": "base"},
                 {"role": "user", "content": "Write attack.py that recovers the key."}],
                project=None, max_steps=steps,
                deadline=(_t.time() + left if left is not None else None)))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        return events, asks, runs

    def test_a_cell_running_out_delivers_instead_of_being_cut_off(self):
        events, asks, runs = self._run(100)
        notes = [e["message"] for e in events if e["type"] == "notify"]
        self.assertTrue(any("wall budget left" in n and "delivering now" in n for n in notes),
                        notes)
        self.assertLess(len(asks), 8,
                        "the turn has to end on the clock, not on the 30-step cap")

    def test_a_cell_with_budget_left_is_told_the_time_but_keeps_working(self):
        events, asks, runs = self._run(400, steps=3)
        self.assertTrue(any("wall-clock budget remain" in a for ask in asks for a in ask),
                        "the remaining budget is part of the request once it gets late")
        notes = [e["message"] for e in events if e["type"] == "notify"]
        self.assertFalse([n for n in notes if "delivering now" in n],
                         "400s left is not an emergency; do not cut the search short")

    def test_no_deadline_means_no_time_talk(self):
        # The interactive path passes no deadline at all, and must not start counting down.
        events, asks, runs = self._run(None, steps=2)
        self.assertFalse([a for ask in asks for a in ask if "wall-clock budget remain" in a],
                         "a chat turn has no wall to count down against")


class TestProseExtraction(unittest.TestCase):
    """A phase that ends with nothing on disk scored zero however well it argued.

    feal trial 2 of the current arm: 16 turns, 38,500 tokens, no write_file ever attempted, and the
    checker's whole verdict is `attack.py does not exist`. When the answer to "write it now" is
    prose containing the code, the code is the deliverable.
    """

    CODE = "def attack(encrypt_fn):\n    return 5\n"

    def _run(self, salvage_reply):
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        runs = []

        def fake_stream(msgs, **k):
            last = str(next((m for m in reversed(msgs) if m.get("role") == "user"), {})
                       .get("content") or "")
            if "Write it now" in last:
                return iter([{"type": "text", "delta": salvage_reply},
                             {"type": "done", "model": None, "finish": "stop"}])
            if "never executed" in last:
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "r", "name": "run_shell",
                     "arguments": '{"command": "python3 t.py"}'}]},
                    {"type": "done", "model": None, "finish": "stop"}])
            return iter([{"type": "text", "delta": "considering the differential"},
                         {"type": "done", "model": None, "finish": "stop"}])

        def fake_tool(name, a, ctx, out):
            runs.append((name, str(a.get("path") or ""), str(a.get("content") or "")))
            out["result"] = "wrote" if name == "write_file" else "ok"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            events = list(agent.run_turn(
                [{"role": "system", "content": "base"},
                 {"role": "user", "content": "Write attack.py that recovers the key."}],
                project=None, max_steps=4))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        return events, runs

    def test_a_prose_reply_still_lands_the_file(self):
        events, runs = self._run("Here it is:\n```python\n" + self.CODE + "```\n")
        wrote = [r for r in runs if r[0] == "write_file"]
        self.assertTrue(wrote, runs)
        self.assertEqual(wrote[0][1], "attack.py")
        self.assertIn("def attack(encrypt_fn)", wrote[0][2],
                      "the model's own code, unedited - the harness only moved it onto disk")
        notes = [e["message"] for e in events if e["type"] == "notify"]
        self.assertTrue(any("attack.py" in n for n in notes), notes)

    def test_prose_with_no_code_writes_nothing(self):
        # Fabricating a stub to have *something* on disk would be gaming the checker, not
        # delivering the model's work.
        _e, runs = self._run("I could not complete the attack in the budget.")
        self.assertFalse([r for r in runs if r[0] == "write_file"], runs)


class TestVerifyPresentFile(unittest.TestCase):
    """A deliverable that exists but was never run is still an unverified guess.

    schemelike went 0/3 in the arm before this with `Missing closing parenthesis` and the like -
    files present, syntax broken, and the salvage loop never fired because it only asks for
    ABSENT files. The turn has to run what it wrote before it is allowed to stop.
    """

    def _run(self, after="run"):
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        asks, runs = [], []

        def fake_stream(msgs, **k):
            last = str(next((m for m in reversed(msgs) if m.get("role") == "user"), {})
                       .get("content") or "")
            asks.append(last)
            if "never executed" in last:
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "v", "name": "run_shell",
                     "arguments": '{"command": "python3 interp.py eval.scm"}'}]},
                    {"type": "done", "model": None, "finish": "stop"}])
            if "Write it now" in last:
                return iter([{"type": "text", "delta": "done"},
                             {"type": "done", "model": None, "finish": "stop"}])
            if not [n for n, _p in runs if n == "write_file"]:
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "w", "name": "write_file",
                     "arguments": '{"path": "attack.py", "content": "def attack(): pass"}'}]},
                    {"type": "done", "model": None, "finish": "stop"}])
            if after == "run":
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "r", "name": "run_shell",
                     "arguments": '{"command": "python3 attack.py"}'}]},
                    {"type": "done", "model": None, "finish": "stop"}])
            if after == "read":
                # The regression. Reading the workspace is not executing the deliverable, and
                # treating it as such let a 200-character eval.scm fragment pass for verified.
                if [n for n, _p in runs if n == "read_file"]:
                    return iter([{"type": "text", "delta": "looks fine to me"},
                                 {"type": "done", "model": None, "finish": "stop"}])
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "r", "name": "read_file",
                     "arguments": '{"path": "feal.py"}'}]},
                    {"type": "done", "model": None, "finish": "stop"}])
            return iter([{"type": "text", "delta": "That should do it."},
                         {"type": "done", "model": None, "finish": "stop"}])

        def fake_tool(name, a, ctx, out):
            runs.append((name, str(a.get("path") or "")))
            out["result"] = "wrote attack.py" if name == "write_file" else "ok"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            list(agent.run_turn(
                [{"role": "system", "content": "base"},
                 {"role": "user", "content": "Write attack.py that recovers the key."}],
                project=None, max_steps=8))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        return asks, runs

    def test_a_written_but_unrun_deliverable_is_run(self):
        asks, runs = self._run("nothing")
        self.assertTrue([a for a in asks if "never executed" in a],
                        "the turn ended with the file written and not once executed")
        self.assertTrue([n for n, _p in runs if n == "run_shell"], runs)

    def test_a_file_the_model_already_ran_owes_nothing(self):
        # The verify round costs a model call. It must not fire on a turn that already ran its
        # output, or every healthy cell pays for it.
        asks, _runs = self._run("run")
        self.assertFalse([a for a in asks if "never executed" in a], asks)

    def test_reading_the_workspace_is_not_running_the_deliverable(self):
        # The exact bug that let schemelike ship a fragment: the flag for "it was executed" was
        # set by any tool except a write, so one read_file after the write marked the artifact
        # verified and the turn ended free to stop.
        asks, runs = self._run("read")
        self.assertTrue([a for a in asks if "never executed" in a],
                        "a read after the write is not an execution of it")
        self.assertTrue([n for n, _p in runs if n == "run_shell"], runs)


class TestMidTurnDemand(unittest.TestCase):
    """The artifact has to be ordered while there is still a turn left to produce it.

    feal t2 spent 16 turns and 38,500 tokens without attempting a write; schemelike t3 spent 34
    turns. Both ran out of turn rather than stopping, so the end-of-turn phase never fired.
    """

    def _run(self, writes_after_demand, steps=8):
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        asks, runs = [], []

        def fake_stream(msgs, **k):
            last = str(next((m for m in reversed(msgs) if m.get("role") == "user"), {})
                       .get("content") or "")
            asks.append(last)
            ordered = "still not on disk" in last
            if ordered and writes_after_demand:
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "w", "name": "write_file",
                     "arguments": '{"path": "attack.py", "content": "def attack(): return 5"}'}]},
                    {"type": "done", "model": None, "finish": "stop"}])
            if [n for n, _p in runs if n == "write_file"]:
                return iter([{"type": "text", "delta": "done"},
                             {"type": "done", "model": None, "finish": "stop"}])
            if not ordered and writes_after_demand is None:
                # writes on step 1: nothing should ever be demanded
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "w", "name": "write_file",
                     "arguments": '{"path": "attack.py", "content": "def attack(): return 5"}'}]},
                    {"type": "done", "model": None, "finish": "stop"}])
            n = len(asks)
            return iter([{"type": "tool_calls", "calls": [
                {"id": f"p{n}", "name": "run_shell",
                 "arguments": '{"command": "python3 probe' + str(n) + '.py"}'}]},
                {"type": "done", "model": None, "finish": "stop"}])

        def fake_tool(name, a, ctx, out):
            runs.append((name, str(a.get("path") or "")))
            out["result"] = "wrote" if name == "write_file" else "ok"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            events = list(agent.run_turn(
                [{"role": "system", "content": "base"},
                 {"role": "user", "content": "Write attack.py that recovers the key."}],
                project=None, max_steps=steps))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        return events, asks, runs

    def test_a_turn_that_only_probes_is_ordered_to_write(self):
        _e, asks, runs = self._run(writes_after_demand=True)
        demand_at = [i for i, a in enumerate(asks) if "still not on disk" in a]
        self.assertTrue(demand_at, "a probing turn with no artifact was never ordered to write")
        self.assertGreaterEqual(demand_at[0], 3,
                                "the order comes once half the turn is gone, not on step one")
        self.assertIn(("write_file", "attack.py"), runs,
                      "and the turn converts: the file lands inside the same turn")

    def test_the_order_escalates_once(self):
        # The demand stays in the transcript, so later steps still see it as the last user
        # message - count distinct messages, not occurrences.
        _e, asks, _r = self._run(writes_after_demand=False, steps=10)
        uniq = [a for i, a in enumerate(asks) if i == 0 or a != asks[i - 1]]
        first = [a for a in uniq if "of this turn is spent" in a]
        second = [a for a in uniq if "STILL not on disk" in a]
        self.assertEqual(len(first), 1, first)
        self.assertEqual(len(second), 1, second)

    def test_a_turn_that_already_writes_is_not_interrupted(self):
        # Every step costs a request; nagging a healthy turn would slow the cells that work.
        _e, asks, _runs = self._run(writes_after_demand=None)
        self.assertFalse([a for a in asks if "not on disk" in a], asks)


if __name__ == "__main__":
    unittest.main()

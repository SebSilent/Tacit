"""Research shares the turn's budget: deadline, cancellation, child slots.

Before the shared budget, a research call minted its own wall clock wherever
it appeared in a turn: a model that asked for research three times bought
three full windows. Now the caller's WorkBudget provides the stop and can
only shrink the clock, and each call reserves one child slot that a spent
turn refuses to hand out.
"""

import threading
import unittest

from backend import agent, budget as budget_mod
import backend.research as R


class TestResearchSharesTheBudget(unittest.TestCase):
    def _setup(self, fake):
        orig = R.research
        R.research = fake
        self.addCleanup(setattr, R, "research", orig)
        out: dict = {}
        return out

    def test_a_spent_children_slice_refuses_research(self):
        calls = []

        def fake(question, project=None, ref=None, call_id="", stop=None,
                 deadline=None):
            calls.append(deadline)
            yield "a report"

        out = self._setup(fake)
        work = budget_mod.WorkBudget(children=1)
        # One delegation already reserved the slice; research is next and gets
        # refused rather than starting a clock the turn cannot pay for.
        self.assertTrue(work.reserve_children(1), "setup: first child runs")
        list(agent.run_research({"question": "why?"}, {"work": work, "stop": None},
                                out, "call-1"))
        self.assertIn("ERROR: the turn's delegation budget is spent", out["result"])
        self.assertEqual(calls, [], "a refused research call never starts")

    def test_the_shared_deadline_shrinks_the_research_clock(self):
        seen = {}
        lock = threading.Event()

        def fake(question, project=None, ref=None, call_id="", stop=None,
                 deadline=None):
            seen["deadline"] = deadline
            seen["stop"] = stop
            lock.set()
            yield "a report"

        self._setup(fake)
        work = budget_mod.WorkBudget(deadline=R.time.monotonic() + 3)
        list(agent.run_research({"question": "why?"}, {"work": work, "stop": None},
                                {}, "call-1"))
        if not lock.wait(10):
            self.fail("the research call never ran")
        # The budget's clock can only ever SHORTEN research's own cap: the
        # deadline passed down is at most now + RESEARCH_TIMEOUT_S.
        self.assertIsNotNone(seen["deadline"])
        self.assertLess(seen["deadline"],
                        __import__("time").monotonic() + R.config.RESEARCH_TIMEOUT_S)
        self.assertIs(seen["stop"], work.stop)

    def test_a_budgetless_turn_keeps_the_unchanged_research_contract(self):
        seen = {}

        def fake(question, project=None, ref=None, call_id="", stop=None,
                 deadline=None):
            seen["stop"] = stop
            seen["deadline"] = deadline
            yield "a report"

        self._setup(fake)
        list(agent.run_research({"question": "why?"}, {"stop": "the turn's stop"},
                                {}, ""))
        self.assertEqual(seen["stop"], "the turn's stop")
        self.assertIsNone(seen["deadline"],
                          "no shared budget: research keeps its own full cap")


if __name__ == "__main__":
    unittest.main()
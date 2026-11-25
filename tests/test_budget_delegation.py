"""Delegation charges the calling turn: the integration half of Phase 2.3.

Driven through the real `run_subagent` with the nested run_turn patched out:
the parent turn carries a WorkBudget; the sub-agent's token spend must drain
the caller's pool, and a turn whose allowance or children slice is spent
refuses the next delegation with a reason the model can act on.
"""

import unittest

from backend import agent, budget as budget_mod


class _FakeTurn:
    """A nested run_turn that emits one usage event per round and ends."""

    def __init__(self, spend_per_round=0, rounds=1):
        self.spend = spend_per_round
        self.rounds = rounds
        self.runs = 0

    def __call__(self, messages, *, project, ref, depth, readonly, nested,
                 session, stop, work, max_steps):
        self.runs += 1
        self.last_work = work
        self.last_max_steps = max_steps

        def stream():
            for i in range(self.rounds):
                # The nested run's own loop is the biller: charge the shared
                # pool the way the real run_turn does through its `work` slice
                # (which IS the caller's pool, by reference).
                if work is not None:
                    work.spend(self.spend)
                yield {"type": "usage", "usage": {"input": self.spend,
                                                  "output": 0, "total": self.spend}}
            yield {"type": "text", "delta": "the sub-agent's report"}

        return stream()


class TestDelegationChargesTheTurn(unittest.TestCase):
    def _setup(self, fake):
        self._orig_run_turn = agent.run_turn
        agent.run_turn = fake
        self.addCleanup(setattr, agent, "run_turn", self._orig_run_turn)

    def _run(self, ctx: dict) -> dict:
        out: dict = {}
        list(agent.run_subagent("look into it", ctx, out, "call-1"))
        return out

    def test_delegated_spend_drains_the_caller_who_started_it(self):
        fake = _FakeTurn(spend_per_round=400, rounds=1)
        self._setup(fake)
        work = budget_mod.WorkBudget(token_allowance=1000, children=3)
        out = self._run({"project": None, "ref": None, "stop": None, "work": work})
        # The parent's pool was charged by the sub-agent's usage.
        self.assertEqual(work.tokens_left, 600, out.get("result"))
        self.assertEqual(out["delegated_tokens"], 400)

    def test_an_explicit_spent_slice_refuses_the_delegation(self):
        """A turn whose children cap drained refuses the next fan-out."""
        fake = _FakeTurn()
        self._setup(fake)
        work = budget_mod.WorkBudget(children=1)
        first = self._run({"project": None, "ref": None, "stop": None, "work": work})
        self.assertNotIn("ERROR:", first.get("result"), first)
        second = self._run({"project": None, "ref": None, "stop": None, "work": work})
        self.assertIn("ERROR:", second.get("result"), second)
        self.assertIn("delegation budget", second.get("result"))
        self.assertEqual(fake.runs, 1, "two delegations from a one-child slice is a leak")

    def test_a_child_cannot_outlive_its_own_call_slice(self):
        fake = _FakeTurn(spend_per_round=10, rounds=50)
        self._setup(fake)
        work = budget_mod.WorkBudget(token_allowance=0, children=3)
        self._run({"project": None, "ref": None, "stop": None, "work": work})
        # The sub-agent's step budget is its slice: subagent_steps() rounds,
        # not the parent's.
        self.assertEqual(fake.last_max_steps, agent.config.subagent_steps())
        self.assertEqual(fake.last_work.calls_left,
                         agent.config.subagent_steps(),
                         "the child's own slice is bounded, not the parent's")

    def test_a_delegation_refused_by_the_depth_gate_still_pays_nothing(self):
        fake = _FakeTurn()
        self._setup(fake)
        work = budget_mod.WorkBudget(children=2)
        out = self._run({"project": None, "ref": None, "stop": None, "work": work,
                         "depth": agent.config.SUBAGENT_MAX_DEPTH})
        self.assertIn("ERROR:", out.get("result"), out)
        self.assertEqual(work.children_left, 2,
                         "a delegation that never started reserves no child")

    def test_the_depth_gate_still_stops_recursion(self):
        fake = _FakeTurn()
        self._setup(fake)
        work = budget_mod.WorkBudget(children=9)
        out = self._run({"project": None, "ref": None, "stop": None, "work": work,
                         "depth": 99})
        self.assertIn("ERROR:", out.get("result"), out)


if __name__ == "__main__":
    unittest.main()
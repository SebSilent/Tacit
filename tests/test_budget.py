"""The shared work budget: one turn's spending authority (Phase 2.3).

Before this object existed every nested worker minted its own budget: a task
delegation got a fresh step allowance that was never charged to anything, a
research call its own wall clock, and a fan-out of six delegations bought six
extra budgets. What is pinned here:

- a child shares its creator's token pool, deadline and cancellation, and gets
  its own bounded call and children slices;
- delegated usage is charged to the caller: the sub-agent's token events drain
  the turn's allowance, and a turn that is out refuses further delegations
  instead of starting work it cannot pay for;
- a child cannot outlive or outspend its slice.
"""

import threading
import time
import unittest

from backend.budget import WorkBudget


class TestWorkBudget(unittest.TestCase):
    def test_child_shares_pool_deadline_and_stop_gets_own_slices(self):
        stop = threading.Event()
        deadline = time.monotonic() + 60
        parent = WorkBudget(deadline=deadline, token_allowance=10000,
                            model_calls=10, children=2, stop=stop)
        child = parent.child(model_calls=4, children=0)
        self.assertEqual(child.deadline, deadline)
        self.assertIs(child.stop, stop)
        # The pool travels, the slices do not: the child's call count is its
        # own, and spending through it drains the parent.
        parent.spend(1000)
        self.assertEqual(child.tokens_left, 9000)
        self.assertEqual(child.calls_left, 4)
        self.assertEqual(parent.calls_left, 10)

    def test_spend_drains_the_shared_pool_and_exhaustion_is_reported(self):
        parent = WorkBudget(token_allowance=1000, model_calls=10)
        child = parent.child(model_calls=3)
        child.spend(600)
        child.spend(700)
        self.assertEqual(parent.tokens_left, -300)
        self.assertIn("spent", parent.exhausted())
        self.assertIn("spent", child.exhausted())
        # A capped pool may go negative exactly once, on the call that
        # overran it; the next start is refused.
        self.assertTrue(parent.exhausted() != "")

    def test_no_allowance_is_unbounded(self):
        b = WorkBudget()
        self.assertEqual(b.exhausted(), "")
        b.spend(10_000_000)
        self.assertEqual(b.exhausted(), "",
                         "a zero allowance means unbounded, never spent")

    def test_a_wall_clock_deadline_ends_the_share(self):
        parent = WorkBudget(deadline=time.monotonic() - 1)
        child = parent.child(model_calls=5)
        self.assertIn("wall budget", child.exhausted())

    def test_cancellation_ends_the_share(self):
        stop = threading.Event()
        parent = WorkBudget(stop=stop)
        child = parent.child(model_calls=5)
        self.assertEqual(child.exhausted(), "")
        stop.set()
        self.assertEqual(child.exhausted(), "cancelled")

    def test_children_are_reserved_or_refused(self):
        parent = WorkBudget(children=2)
        self.assertTrue(parent.reserve_children(1))
        self.assertTrue(parent.reserve_children(1))
        self.assertFalse(parent.reserve_children(1),
                         "a third fan-out is refused, not silently free")
        self.assertEqual(parent.children_left, 0)

    def test_deadline_in_clips_to_the_shared_clock(self):
        parent = WorkBudget(deadline=time.monotonic() + 10)
        later = parent.deadline_in(60)
        self.assertLessEqual(later, parent.deadline)
        # A nested worker asking for more than remains gets what remains.
        self.assertLessEqual(parent.deadline_in(5), parent.deadline)

    def test_a_budgetless_turn_carries_the_defaults(self):
        b = WorkBudget()
        self.assertIsNone(b.deadline)
        self.assertIsNone(b.stop)
        self.assertEqual(b.tokens_left, 0)
        self.assertEqual(b.calls_left, 0)
        self.assertEqual(b.exhausted(), "", "unbounded defaults never exhaust")


if __name__ == "__main__":
    unittest.main()
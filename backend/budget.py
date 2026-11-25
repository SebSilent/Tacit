"""One turn's spending authority, shared by everything the turn starts.

The measured failure: every nested worker minted its own budget. A `task`
delegation got a fresh sub-agent step allowance, a research call its own wall
clock, and neither was charged to anything above it - so a turn that fanned out
six delegations bought six extra step budgets and three research clocks, and
the only ceiling on the whole fan-out was the parent's context
window. `WorkBudget` is one object: a call allowance, a token allowance, a
children allowance, a shared deadline and the turn's cancellation event.

The accounting rules:

- **Delegated usage is charged to the parent.** A child shares its creator's
  token pool and deadline: what the sub-agent, the aspects or the salvage
  rounds spend comes out of the same allowance, so a fan-out cannot multiply
  the bill.
- **A child's own call count is bounded.** A delegation still gets its
  step slice (the sub-agent's own budget, unchanged), and the slice is
  subtracted from what the parent can hand to further children, so a
  turn's total reserved work is what it earned.
- **Every consumer checks before it works.** Spending happens when usage
  lands, reservations happen before a child starts; a consumer that finds the
  allowance spent refuses the work rather than starting something it cannot
  pay for.

Allowances of zero mean unbounded, the default for a turn whose operator has
not set one - the object still carries children and deadline accounting.
"""

from __future__ import annotations

import time


class WorkBudget:
    """Shared spending authority for one turn and everything it starts."""

    def __init__(self, *, deadline: float | None = None, token_allowance: int = 0,
                 model_calls: int = 0, children: int = 0, stop=None):
        self._deadline = deadline            # shared: monotonic seconds
        self._stop = stop                    # shared: the turn's cancellation
        self._tokens_left = [int(token_allowance or 0)]
        self._children_left = [int(children or 0)]
        # Zero means unbounded only where the OPERATOR set nothing: an
        # explicitly capped slice stays exhausted when it drains.
        self._children_capped = bool(children)
        self.calls_left = int(model_calls or 0)

    @property
    def deadline(self):
        return self._deadline

    @property
    def stop(self):
        return self._stop

    @property
    def tokens_left(self) -> int:
        return self._tokens_left[0]

    @property
    def children_left(self) -> int:
        return self._children_left[0]

    def exhausted(self) -> str:
        """Why no further work may start, or '' while the budget carries."""
        if self._stop is not None and self._stop.is_set():
            return "cancelled"
        if self._deadline is not None and time.monotonic() >= self._deadline:
            return "the turn's wall budget is spent"
        if self.calls_left < 0 or (
                self._tokens_left[0] < 0):
            # A negative balance means a reservation overran its pool: the last
            # consumer is still entitled to finish, further starts are not.
            return "the turn's budget is spent"
        return ""

    def consume_call(self, n: int = 1) -> None:
        self.calls_left -= n

    def spend(self, tokens: int) -> None:
        """Charge real usage against the shared pool. A capped pool may go
        negative exactly once, on the call that overran it; exhaustion is
        enforced before the next start, not by aborting work in flight."""
        if self._tokens_left[0] > 0:
            self._tokens_left[0] -= int(tokens or 0)

    def reserve_children(self, n: int = 1) -> bool:
        """Reserve n child workers. False when the turn's slice is spent; a
        zero children allowance means unbounded (nothing was capped), while a
        drained explicit cap refuses."""
        left = self._children_left[0]
        if self._children_capped and left < n:
            return False
        if left:
            self._children_left[0] = left - n
        return True

    def child(self, *, model_calls: int = 0, children: int = 0) -> "WorkBudget":
        """One nested worker's slice: its own call and children counts, and
        the SHARED token pool, deadline and cancellation. The parent's pool is
        charged by anything the child spends, which is the accounting rule
        above; the child cannot outlive the turn it serves."""
        child = WorkBudget(deadline=self._deadline, token_allowance=0,
                           model_calls=model_calls, children=children,
                           stop=self._stop)
        child._tokens_left = self._tokens_left   # the pool, BY REFERENCE
        return child

    def deadline_in(self, seconds: float) -> float:
        """The shared deadline clipped to at most `seconds` from now, or None
        when the budget carries no wall clock. Nested workers use this instead
        of minting a full fresh window."""
        if self._deadline is None:
            return None
        return min(self._deadline, time.monotonic() + max(0.5, float(seconds)))

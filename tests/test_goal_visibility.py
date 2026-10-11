"""The Goal is the Assistant's, and never the agent's.

Two bugs this replaces, in opposite directions. The first: the Goal was appended
to the agent's user turn on every step, so the objective the Assistant was meant
to hold was printed in front of the agent -- and the agent, told it "outranks any
instruction to wrap up early", could not ignore it. The second would be the
over-correction: dropping the Goal from the record, or letting compaction
summarise it away, leaving the Assistant steering an autonomous run against
nothing.

So the contract has two halves and both are asserted here: the Assistant sees the
Goal, the agent does not, and the Goal survives a save.
"""

import unittest
from pathlib import Path

from backend import agent, assistant, store
from backend.ai import prompts

# The literal marker the injected block was built from. If this string appears in
# an agent-side module, the leak is back.
MARKER = "Goal for this session"

# Everything that assembles what the agent is sent. session_runtime is here
# because it is where the agent's message list is built -- the site the leak lived
# at, and the one place a future edit would reintroduce it.
AGENT_SIDE = ("backend/agent.py", "backend/session_runtime.py", "backend/headless.py",
              "backend/turn_worker.py", "backend/ai/prompts.py", "backend/guidance.py")

ROOT = Path(__file__).resolve().parent.parent

GOAL = "Ship the meter fix, then write the tests."


def _rec():
    return {
        "id": "goal-visibility", "title": "t", "model": "", "mode": "agent",
        "project": "", "messages": [
            {"role": "user", "content": "start the work"},
            {"role": "assistant", "content": "listed the files"},
        ],
        assistant.GOAL_FIELD: GOAL,
    }


class TestGoalVisibility(unittest.TestCase):
    def test_the_agent_side_never_names_the_goal_block(self):
        for rel in AGENT_SIDE:
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn(MARKER, src,
                             "%s contains the injected Goal text -- the Goal is for "
                             "the Assistant alone, and this is the string that was "
                             "leaking into the agent's turn." % rel)

    def test_no_agent_side_calls_the_cost_report(self):
        """goal_block() is a cost report with no caller. Re-wiring it into a turn
        is the leak, so the absence of a caller is what is under test."""
        for rel in AGENT_SIDE:
            src = (ROOT / rel).read_text(encoding="utf-8")
            calls = [ln.strip() for ln in src.splitlines()
                     if "goal_block(" in ln and not ln.strip().startswith("#")]
            self.assertEqual(calls, [],
                             "%s calls goal_block() -- nothing on the agent's side may." % rel)

    def test_the_assistant_does_see_it(self):
        """The other half. A guard that only proved the agent was blind would pass
        just as happily if the Goal had been deleted outright."""
        self.assertIn(GOAL, assistant.system_prompt(_rec()))
        self.assertIn("Goal", assistant.system_prompt(_rec()))

    def test_the_agent_prompt_and_transcript_carry_no_goal(self):
        """Behavioural, not merely textual: build the two things the agent is
        actually sent from a record that has a Goal, and look for it."""
        self.assertNotIn(GOAL, prompts.system_prompt("", False))
        rows = agent.transcript_messages(_rec()["messages"])
        self.assertNotIn(GOAL, str(rows),
                         "the Goal rides on the record, not in the transcript; if it "
                         "reaches the wire the leak is back")

    def test_the_goal_is_preserved_across_a_save(self):
        """Compaction rewrites messages, so the Goal lives on the record field --
        and a turn holding a minutes-old copy must not rewind it on its next save."""
        self.assertIn(assistant.GOAL_FIELD, store.PRESERVED)
        self.assertIn(assistant.AUTONOMY_FIELD, store.PRESERVED)


if __name__ == "__main__":
    unittest.main()

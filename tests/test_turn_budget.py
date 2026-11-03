"""The turn budgets are settings, not constants.

A cap that only an environment variable can move is a cap nobody moves: the
person whose turn ran out of steps would have to close Tacit and edit whatever
launches it. So each budget reads the environment, then the stored preference,
then the module constant, in that order, and these tests hold that precedence
plus the clamp. They patch `config.prefs` rather than writing to `~/.tacit`,
because a test that edits the operator's real preferences is a test that can
change how the next turn runs.
"""

import os
import unittest
from unittest import mock

from backend import config

ALL_ENV = ("TACIT_MAX_STEPS", "TACIT_SUBAGENT_STEPS", "TACIT_RESEARCH_STEPS",
           "TACIT_ASSISTANT_STEPS", "TACIT_TURN_TOKEN_BUDGET")


class BudgetPrecedence(unittest.TestCase):
    """One helper per rule, since the rule is the order the three sources win in."""

    def with_sources(self, env, prefs, fn):
        clean = {k: v for k, v in env.items() if k in ALL_ENV}
        with mock.patch.dict(os.environ, clean, clear=True):
            with mock.patch.object(config, "prefs", return_value=prefs):
                return fn()

    def test_constant_is_the_default(self):
        self.assertEqual(self.with_sources({}, {}, config.max_steps),
                         config.AGENT_MAX_STEPS)
        self.assertEqual(self.with_sources({}, {}, config.subagent_steps),
                         config.SUBAGENT_MAX_STEPS)
        self.assertEqual(self.with_sources({}, {}, config.research_steps),
                         config.RESEARCH_MAX_STEPS)
        self.assertEqual(self.with_sources({}, {}, config.assistant_steps),
                         config.ASSISTANT_MAX_STEPS)
        self.assertEqual(self.with_sources({}, {}, config.turn_token_budget),
                         config.TURN_TOKEN_BUDGET)

    def test_saved_value_beats_the_constant(self):
        prefs = {"maxSteps": 40, "subagentSteps": 20, "researchSteps": 9,
                 "assistantSteps": 24, "turnTokenBudget": 250000}
        self.assertEqual(self.with_sources({}, prefs, config.limits),
                         {"max_steps": 40, "subagent_steps": 20,
                          "research_steps": 9, "assistant_steps": 24,
                          "turn_token_budget": 250000})

    def test_environment_beats_the_saved_value(self):
        # The same precedence every other harness switch follows: a pinned deploy
        # cannot be talked out of its limit by a click.
        got = self.with_sources({"TACIT_MAX_STEPS": "8"}, {"maxSteps": 40}, config.max_steps)
        self.assertEqual(got, 8)

    def test_pinned_names_are_reported_to_the_panel(self):
        # A control that saves into an override and shows the saved number is a
        # control that lies, so the route hands the panel the variable names.
        with mock.patch.dict(os.environ, {"TACIT_RESEARCH_STEPS": "4"}, clear=True):
            self.assertEqual(config.limits_pinned(), ["TACIT_RESEARCH_STEPS"])

    def test_nothing_is_pinned_by_default(self):
        self.assertEqual(self.with_sources({}, {}, config.limits_pinned), [])

    def test_out_of_range_is_clamped_not_trusted(self):
        self.assertEqual(self.with_sources({}, {"maxSteps": 100000}, config.max_steps), 200)
        self.assertEqual(self.with_sources({}, {"maxSteps": 0}, config.max_steps), 1)

    def test_junk_falls_back_to_the_constant(self):
        # A half-typed value in prefs.json must not become 0 steps, which would
        # end every turn before the agent could do anything.
        self.assertEqual(self.with_sources({}, {"maxSteps": "many"}, config.max_steps),
                         config.AGENT_MAX_STEPS)

    def test_a_zero_ceiling_is_a_value_and_not_a_missing_one(self):
        # 0 means "no ceiling" here, so a falsy check would silently re-read the
        # constant and put a cap back on a turn that asked for none.
        self.assertEqual(self.with_sources({}, {"turnTokenBudget": 0},
                                           config.turn_token_budget), 0)

    def test_bounds_are_published(self):
        bounds = config.limit_bounds()
        self.assertEqual(set(bounds), {"max_steps", "subagent_steps", "research_steps",
                                       "assistant_steps", "turn_token_budget"})
        self.assertEqual(bounds["max_steps"], {"min": 1, "max": 200})

    def test_the_assistant_budget_is_its_own_number(self):
        """The pane's cap used to be `TOOL_LIMIT = 6` inside assistant.py, which no
        setting could raise and which cut replies off mid-read. It is a budget now,
        and it stays well under the main loop's, because the figure that runs out in
        the pane is the one that costs an answer rather than a step."""
        self.assertEqual(self.with_sources({}, {}, config.assistant_steps), 16)
        self.assertLess(self.with_sources({}, {}, config.assistant_steps),
                        self.with_sources({}, {}, config.max_steps))
        self.assertEqual(self.with_sources({"TACIT_ASSISTANT_STEPS": "30"},
                                           {"assistantSteps": 4}, config.assistant_steps), 30)
        self.assertEqual(self.with_sources({}, {"assistantSteps": 999},
                                           config.assistant_steps), 60)


    def test_the_constants_are_still_the_last_resort(self):
        # The fallback is looked up by name through globals(), so a test (or a
        # site) that moves the constant still moves the answer. A captured value
        # would leave the loop paying the number that was there at import.
        original = config.AGENT_MAX_STEPS
        try:
            config.AGENT_MAX_STEPS = 17
            self.assertEqual(self.with_sources({}, {}, config.max_steps), 17)
        finally:
            config.AGENT_MAX_STEPS = original


class BudgetCallSites(unittest.TestCase):
    """Every place that spends a budget has to ask for it, not read the constant."""

    MODULES = ("agent.py", "plan.py", "research.py", "headless.py", "assistant.py")

    def test_no_call_site_reads_a_constant_directly(self):
        # A site that reads `config.AGENT_MAX_STEPS` ignores whatever the panel
        # saved, and nothing fails loudly: the turn simply runs the old length.
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "backend")
        for name in self.MODULES:
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                src = handle.read()
            for constant in ("config.AGENT_MAX_STEPS", "config.SUBAGENT_MAX_STEPS",
                             "config.RESEARCH_MAX_STEPS", "config.ASSISTANT_MAX_STEPS",
                             "config.TURN_TOKEN_BUDGET"):
                self.assertNotIn(constant, src, f"{name} still reads {constant}")

    def test_the_assistant_loop_asks_for_its_budget(self):
        """`assistant.py` had its cap as a module constant of its own, so the file
        that spends the budget was the one file that never read the setting."""
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "backend")
        with open(os.path.join(root, "assistant.py"), encoding="utf-8") as handle:
            src = handle.read()
        self.assertIn("config.assistant_steps()", src,
                      "the pane's rounds must come from the tunable budget")


if __name__ == "__main__":
    unittest.main()

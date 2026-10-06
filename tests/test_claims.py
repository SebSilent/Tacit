"""Regression tests for the claims the README makes about behaviour.

Every test here exists because a documented behaviour and the running program
disagreed, and the disagreement was only visible in a transcript: an agent
re-reading the same file five times, a sub-agent that had forgotten what it was
asked, a dashboard row that could never leave zero. They lock the claim to the
code so the next refactor cannot quietly drop it.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import (agent, assistant, audit, benchmarks, config, guidance,  # noqa: E402
                     metrics, plugin_manager, project_context, tokens)
from backend.ai import engine  # noqa: E402
from backend.plugins import task_list  # noqa: E402
from backend.routers import chat  # noqa: E402
from tests.helpers import state_paths  # noqa: E402


class Isolated(unittest.TestCase):
    """Points every storage path under the user's home at a temporary one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {key: getattr(config, key) for key in self._keys}
        self._home = config.HOME
        config.HOME = home
        for key in self._keys:
            target = home / Path(self._orig[key]).name
            setattr(config, key, target)
            if key.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        config.HOME = self._home
        for key, value in self._orig.items():
            setattr(config, key, value)
        self._tmp.cleanup()


class ProjectFixture(Isolated):
    """A small throwaway project to point the file tools at."""

    def setUp(self):
        super().setUp()
        self._proj = tempfile.TemporaryDirectory()
        self.addCleanup(self._proj.cleanup)
        self.root = Path(self._proj.name)
        (self.root / "alpha.py").write_text(
            "def only_in_alpha():\n    return 1\n", encoding="utf-8")
        (self.root / "beta.py").write_text(
            "def only_in_beta():\n    return 2\n", encoding="utf-8")
        sub = self.root / "pkg"
        sub.mkdir()
        (sub / "gamma.py").write_text("SHARED = 'in gamma'\n", encoding="utf-8")
        (sub / "delta.py").write_text("SHARED = 'in delta'\n", encoding="utf-8")
        (self.root / "long.txt").write_text(
            "\n".join(f"line {i} " + "x" * 60 for i in range(1, 401)), encoding="utf-8")
        # 400 lines is ~27 KB, the size of a real source file the agent must reproduce.
        # The second fixture is big enough to still need clipping.
        (self.root / "huge.txt").write_text(
            "\n".join(f"line {i} " + "x" * 60 for i in range(1, 1001)), encoding="utf-8")

    def proj(self) -> str:
        return str(self.root)


# ── 1. read_file tells the model where it stopped ─────────────────────────
class TestReadFileRange(ProjectFixture):
    """README: the agent should not have to re-read what it already read."""

    def test_truncated_read_names_the_range_and_the_resume_offset(self):
        out = agent.t_read_file("huge.txt", offset=0, limit=1000, project=self.proj())
        self.assertLessEqual(len(out), config.READ_OUTPUT_LIMIT + 120)
        # The trailer survives the clip and states the span actually delivered,
        # not the span asked for.
        self.assertIn("of 1000 shown", out)
        self.assertRegex(out, r"pass offset=\d+ for the next \d+")
        last = int(out.rsplit("lines 1-", 1)[1].split(" of ")[0])
        self.assertLess(last, 1000)
        self.assertIn(f"pass offset={last}", out)

    def test_a_real_source_file_arrives_whole(self):
        # 27 KB is larger than interp.py (17,578 chars). Clipping it at 6,000 left two
        # thirds of the file the agent must reimplement unseen, and the agent stopped
        # reading and started probing with the shell instead.
        out = agent.t_read_file("long.txt", offset=0, limit=400, project=self.proj())
        self.assertNotIn("truncated", out)
        self.assertIn("end of file", out)
        self.assertIn("line 400 ", out)
        self.assertGreater(len(out), config.TOOL_OUTPUT_LIMIT)

    def test_complete_read_says_end_of_file(self):
        out = agent.t_read_file("alpha.py", project=self.proj())
        self.assertIn("end of file", out)
        self.assertIn("of 2 shown", out)

    def test_offset_beyond_the_end_is_an_error_not_a_silent_reread(self):
        out = agent.t_read_file("alpha.py", offset=900, project=self.proj())
        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertIn("past the end", out)

    def test_requested_chunk_is_honoured_when_it_fits(self):
        out = agent.t_read_file("long.txt", offset=10, limit=5, project=self.proj())
        self.assertIn("lines 11-15 of 400 shown", out)
        self.assertIn("pass offset=15", out)


# ── 2. grep_files searches what it was pointed at ─────────────────────────
class TestGrepPath(ProjectFixture):
    """A file path means that file. A missing path is an error."""

    def test_file_path_searches_only_that_file(self):
        out = agent.t_grep_files("def only_in_", "alpha.py", project=self.proj())
        self.assertIn("alpha.py:1", out)
        self.assertNotIn("beta.py", out)

    def test_file_path_does_not_report_a_neighbours_match(self):
        # The old fallback searched the parent directory, so this returned a hit
        # from a file nobody asked about and read as a real answer.
        out = agent.t_grep_files("only_in_beta", "alpha.py", project=self.proj())
        self.assertEqual(out, "(no matches)")

    def test_directory_path_still_searches_the_tree(self):
        out = agent.t_grep_files("SHARED", "pkg", project=self.proj())
        self.assertIn("gamma.py", out)
        self.assertIn("delta.py", out)

    def test_missing_path_errors_instead_of_searching_the_parent(self):
        out = agent.t_grep_files("SHARED", "pkg/nope.py", project=self.proj())
        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertIn("does not exist", out)

    def test_output_respects_the_tool_output_limit(self):
        # README documents TACIT_TOOL_OUTPUT_LIMIT as the characters kept from a
        # tool result. grep used to return tens of thousands.
        out = agent.t_grep_files("x", ".", project=self.proj())
        self.assertLessEqual(len(out), config.TOOL_OUTPUT_LIMIT + 60)


# ── 3. elision keeps the task ─────────────────────────────────────────────
class TestWindowedPinsTheTask(unittest.TestCase):
    """A sub-agent that loses its instructions cannot answer the question."""

    def _subagent_messages(self):
        return ([{"role": "system", "content": "you are a sub-agent"},
                 {"role": "user", "content": "MAP THE RUNTIME LOOP and report class names"}]
                + [{"role": "assistant", "content": "x" * 900} for _ in range(40)])

    def test_task_prompt_survives_elision(self):
        msgs = self._subagent_messages()
        kept = agent.windowed(msgs, 6000)
        self.assertIn("MAP THE RUNTIME LOOP", kept[1]["content"])
        self.assertEqual(kept[0]["role"], "system")

    def test_elision_is_announced(self):
        kept = agent.windowed(self._subagent_messages(), 6000)
        self.assertTrue(any("elided" in (m.get("content") or "") for m in kept))

    def test_compaction_summary_survives_elision(self):
        # After compact_history the summary sits at index 1, behind the standing
        # prompt. Pinning only messages[0] threw away the one record of the work
        # the elision had just removed.
        msgs = ([{"role": "system", "content": "standing prompt"},
                 {"role": "system", "content": "[Compacted history of earlier work]\nwe did X"}]
                + [{"role": "assistant", "content": "y" * 900} for _ in range(40)])
        kept = agent.windowed(msgs, 6000)
        self.assertIn("we did X", " ".join(m.get("content") or "" for m in kept))

    def test_no_elision_when_it_fits(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
        self.assertEqual(agent.windowed(msgs, 6000), msgs)

    def test_orphan_tool_result_is_dropped(self):
        msgs = ([{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
                + [{"role": "assistant", "content": "z" * 800} for _ in range(20)]
                + [{"role": "tool", "tool_call_id": "c1", "content": "late result"}])
        kept = agent.windowed(msgs, 3000)
        self.assertNotEqual(kept[2].get("role"), "tool")


# ── 4. compaction measures the real transcript ────────────────────────────
class TestCompactionTrigger(Isolated):
    """README: older turns compact in place. They have to notice they should."""

    def _rec(self, tool_chars: int):
        return {"model": "p/m", "messages": [
            {"role": "user", "content": "understand the project"},
            {"role": "assistant", "content": "reading",
             "tools": [{"id": "c1", "name": "read_file", "args": {"path": "a.py"},
                        "result": "r" * tool_chars, "is_error": False}]},
            {"role": "assistant", "content": "more"},
            {"role": "assistant", "content": "more"},
            {"role": "assistant", "content": "more"},
            {"role": "assistant", "content": "more"},
            {"role": "assistant", "content": "more"},
            {"role": "assistant", "content": "more"},
        ]}

    def test_chars_counts_tool_results(self):
        rec = self._rec(50000)
        self.assertGreaterEqual(agent._chars(rec["messages"]), 50000)

    def test_tool_heavy_session_compacts_without_provider_usage(self):
        # 13k of narration hiding 200k of tool results is the shape that never
        # triggered compaction, so the window cap elided the work instead.
        self.assertTrue(agent.should_compact(self._rec(200000)))

    def test_small_session_does_not_compact(self):
        self.assertFalse(agent.should_compact(self._rec(10)))

    def test_digest_row_includes_the_tools_that_ran(self):
        row = {"role": "assistant", "content": "let me look",
               "tools": [{"id": "c1", "name": "read_file", "args": {"path": "a.py"},
                          "result": "x" * 9000, "is_error": True}]}
        text = agent._digest_row(row)
        self.assertIn("read_file", text)
        self.assertIn("a.py", text)
        self.assertIn("FAILED", text)
        # The summariser gets a digest, not the payload.
        self.assertLess(len(text), 600)


# ── 5. the window cap follows the model ───────────────────────────────────
class TestContextBudget(Isolated):
    """One flat cap is wrong for a 1M-token model and for an 8k one."""

    def setUp(self):
        super().setUp()
        self._orig_resolve = config.resolve_model
        self.addCleanup(setattr, config, "resolve_model", self._orig_resolve)
        self._orig_profile = benchmarks.profile_for
        self.addCleanup(setattr, benchmarks, "profile_for", self._orig_profile)
        benchmarks.profile_for = lambda ref: {}

    def test_unknown_window_falls_back_to_the_configured_default(self):
        config.resolve_model = lambda ref=None: {"contextWindow": 0}
        self.assertEqual(agent.context_budget("p/m"), config.AGENT_CONTEXT_BUDGET)

    def test_large_window_raises_the_cap(self):
        config.resolve_model = lambda ref=None: {"contextWindow": 1_000_000}
        budget = agent.context_budget("p/m")
        self.assertGreater(budget, config.AGENT_CONTEXT_BUDGET)
        self.assertLessEqual(budget, config.CONTEXT_BUDGET_MAX)

    def test_small_window_lowers_the_cap(self):
        config.resolve_model = lambda ref=None: {"contextWindow": 4000}
        budget = agent.context_budget("p/m")
        self.assertLess(budget, config.AGENT_CONTEXT_BUDGET)
        self.assertGreaterEqual(budget, config.CONTEXT_BUDGET_MIN)

    def test_recorded_window_wins_over_the_registry(self):
        config.resolve_model = lambda ref=None: {"contextWindow": 4000}
        rec = {"usage": {"context": {"window": 500_000}}}
        self.assertGreater(agent.context_budget("p/m", rec), config.AGENT_CONTEXT_BUDGET)

    def test_per_model_override_is_honoured(self):
        # `benchmark action=set context_budget=` was stored and never read back.
        config.resolve_model = lambda ref=None: {"contextWindow": 1_000_000}
        benchmarks.profile_for = lambda ref: {"context_budget": 42000}
        self.assertEqual(agent.context_budget("p/m"), 42000)


# ── 6. a delegated report keeps its ending ────────────────────────────────
class TestReportClipping(unittest.TestCase):
    """The parent acts on the conclusion, so the conclusion must survive."""

    def test_short_report_is_untouched(self):
        text = "a short report"
        self.assertEqual(agent._clip_report(text, 4000), text)

    def test_long_report_keeps_head_and_tail(self):
        text = "FINDING-START " + ("middle " * 3000) + " FINDING-END"
        out = agent._clip_report(text, 4000)
        self.assertIn("FINDING-START", out)
        self.assertIn("FINDING-END", out)
        self.assertLessEqual(len(out), 4200)

    def test_says_how_much_was_dropped(self):
        out = agent._clip_report("q" * 20000, 4000)
        self.assertIn("dropped", out)
        self.assertIn("delegation budget", out)


# ── 7. read-only means read-only ──────────────────────────────────────────
class TestReadOnlyGuard(Isolated):
    """The Assistant's switch and the sub-agent both claim they cannot write."""

    def test_mutating_tools_are_not_offered(self):
        names = {t["function"]["name"] for t in agent.tools_for(readonly=True)}
        for blocked in ("write_file", "edit_file", "run_shell", "snapshot",
                        "mcp_call", "mcp_activate_tools", "task", "restore"):
            self.assertNotIn(blocked, names)

    def test_reading_tools_are_still_offered(self):
        names = {t["function"]["name"] for t in agent.tools_for(readonly=True)}
        for kept in ("read_file", "list_files", "grep_files", "glob_files", "fetch",
                     "skill", "mcp_search_tools"):
            self.assertIn(kept, names)

    def test_writing_action_of_a_reading_tool_is_refused(self):
        self.assertTrue(agent.readonly_guard("benchmark", {"action": "set"}))
        self.assertTrue(agent.readonly_guard("evidence", {"action": "add"}))
        self.assertEqual(agent.readonly_guard("benchmark", {"action": "list"}), "")
        self.assertEqual(agent.readonly_guard("evidence", {"action": "list"}), "")

    def test_call_tool_refuses_even_when_the_model_names_it(self):
        # Hiding the schema is not the guard: a model that remembers the name can
        # still emit the call.
        out = {}
        for _ in agent.call_tool("snapshot", {"label": "x"},
                                 {"readonly": True, "project": None}, out):
            pass
        self.assertTrue(out["result"].startswith("ERROR:"), out["result"])
        self.assertIn("read-only", out["result"])

    def test_main_agent_is_not_restricted(self):
        # The guard describes what a read-only caller may not do; the enforcement
        # point is call_tool, and only when the context says the caller is
        # read-only. The main agent's context does not.
        out = {}
        for _ in agent.call_tool("list_snapshots", {}, {"project": None}, out):
            pass
        self.assertNotIn("read-only", str(out.get("result")))
        names = {t["function"]["name"] for t in agent.tools_for(readonly=False)}
        for kept in ("snapshot", "write_file", "edit_file", "run_shell", "task"):
            self.assertIn(kept, names)


class TestAssistantReadOnly(Isolated):
    def test_assistant_cannot_run_a_mutating_tool(self):
        rec = {"id": "s1", "project": None}
        out = assistant._run_read_tool("write_file", {"path": "a.txt", "content": "x"}, rec)
        self.assertTrue(out.startswith("ERROR:"), out)

    def test_assistant_settings_can_be_switched_off(self):
        # A checkbox's .value is "on" ticked or not, so every switch used to be
        # sticky once the interface sent it.
        rec = {"id": "s1", "assistantSettings": {"include_tools": True}}
        cfg = assistant.save_settings(rec, {"include_tools": False})
        self.assertFalse(cfg["include_tools"])

    def test_tool_digest_is_off_by_default_and_on_when_asked(self):
        rec = {"id": "s1", "title": "t", "messages": [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "done",
             "tools": [{"id": "c1", "name": "read_file", "args": {"path": "a.py"},
                        "result": "x", "is_error": False}]}]}
        self.assertNotIn("read_file", assistant.digest(rec))
        cfg = assistant.save_settings(rec, {"include_tools": True})
        self.assertIn("read_file", assistant.digest(rec, cfg))
        self.assertIn("a.py", assistant.digest(rec, cfg))


# ── 8. the ledger holds what the README says it holds ─────────────────────
class TestAuditLedger(Isolated):
    def test_tool_call_is_recorded(self):
        agent._audit_tool("read_file", {"path": "a.py"}, "ok", False,
                          {"session": "s1", "depth": 0})
        rows = audit.recent(10)
        self.assertEqual(rows[0]["event"], "tool_call")
        self.assertEqual(rows[0]["tool"], "read_file")
        self.assertEqual(rows[0]["session"], "s1")

    def test_failed_call_is_marked(self):
        agent._audit_tool("read_file", {"path": "a.py"}, "ERROR: no such file", True,
                          {"session": "s1"})
        self.assertEqual(audit.recent(1)[0]["status"], "error")

    def test_shell_is_left_to_the_sandbox_row(self):
        agent._audit_tool("run_shell", {"command": "ls"}, "ok", False, {"session": "s1"})
        self.assertEqual([r for r in audit.recent(10) if r["event"] == "tool_call"], [])

    def test_credentials_in_arguments_are_masked(self):
        agent._audit_tool("fetch", {"url": "https://x.test/a?token=SECRETVALUE"},
                          "ok", False, {"session": "s1"})
        raw = config.AUDIT_FILE.read_text(encoding="utf-8")
        self.assertNotIn("SECRETVALUE", raw)

    def test_clear_rotates_and_keeps_the_history(self):
        audit.record("tool_call", tool="read_file", session="s1")
        before = audit.count()
        res = audit.clear()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["rows"], before)
        archived = config.HOME / res["rotated"]
        self.assertTrue(archived.exists())
        with open(archived, encoding="utf-8") as fh:
            self.assertEqual(sum(1 for _ in fh), before)
        # Nothing was destroyed, and the rotation itself is on the record.
        self.assertEqual(audit.recent(5)[0]["event"], "audit_rotated")
        self.assertEqual(audit.archives()[0]["name"], res["rotated"])


# ── 9. the dashboard rows are reachable ───────────────────────────────────
class TestDashboardCounters(Isolated):
    def test_delegation_bumps_saved_subagent(self):
        rec = {"id": "s1", "model": "p/m", "messages": []}
        out = {"saved": 1200, "spent": 1500}
        metrics.bump(rec, saved_subagent=int(out.get("saved") or 0))
        dash = metrics.dashboard(rec)
        self.assertEqual(dash["saved_subagent"], 1200)
        self.assertIn("saved_subagent", dash)

    def test_memory_budget_saving_is_counted(self):
        rec = {"id": "s1", "model": "p/m", "messages": []}
        metrics.bump(rec, memory_tokens=120, saved_memory_budget=380)
        dash = metrics.dashboard(rec, memory_total=500)
        self.assertEqual(dash["saved_memory_budget"], 380)
        self.assertEqual(dash["saved_total"], 380)

    def test_usage_event_does_not_blank_the_context_meter(self):
        orig = chat.config.resolve_model
        self.addCleanup(setattr, chat.config, "resolve_model", orig)
        chat.config.resolve_model = lambda ref=None: {"contextWindow": 100000}
        rec = {"id": "s1", "model": "p/m", "messages": [{"role": "user", "content": "hi"}]}
        chat._usage_event(rec, {"input": 5000, "output": 100, "total": 5100})
        self.assertEqual(rec["usage"]["context"]["tokens"], 5100)
        # A later call that reports no usage used to overwrite this with None, so
        # the interface showed no percentage on a session that had billed plenty.
        chat._usage_event(rec, {"input": 0, "output": 0, "total": 0})
        self.assertEqual(rec["usage"]["context"]["tokens"], 5100)
        self.assertIsNotNone(rec["usage"]["context"]["percent"])

    def test_subagent_usage_is_billed_but_does_not_move_the_meter(self):
        orig = chat.config.resolve_model
        self.addCleanup(setattr, chat.config, "resolve_model", orig)
        chat.config.resolve_model = lambda ref=None: {"contextWindow": 100000}
        rec = {"id": "s1", "model": "p/m", "messages": [{"role": "user", "content": "hi"}]}
        chat._usage_event(rec, {"input": 5000, "output": 100, "total": 5100})
        chat._usage_event(rec, {"input": 900, "output": 50, "total": 950}, subagent=True)
        self.assertEqual(rec["usage"]["tokens"]["input"], 5900)
        self.assertEqual(rec["usage"]["context"]["tokens"], 5100)


# ── 10. guidance describes the tools that exist ───────────────────────────
class TestGuidanceCards(unittest.TestCase):
    def test_every_card_names_a_real_tool(self):
        known = {t["function"]["name"] for t in agent.TOOLS}
        for name in guidance.CARDS:
            self.assertIn(name, known)

    def test_edit_file_card_names_its_real_parameters(self):
        card = guidance.CARDS["edit_file"]
        self.assertIn("find", card)
        self.assertIn("replace", card)
        self.assertNotIn("oldText", card)

    def test_cards_mention_only_real_parameters(self):
        # A card that invents a parameter name costs tokens and misleads.
        known = {t["function"]["name"] for t in agent.TOOLS}
        for name, card in guidance.CARDS.items():
            self.assertIn(name, known)
            for token in ("oldText", "newText", "maxChars"):
                self.assertNotIn(token, card, f"{name} card names {token}")


# ── 11. the published prompt figures are the real ones ────────────────────
class TestPublishedFigures(Isolated):
    """The comparison table is measured from this code, so it must still match."""

    def test_base_prompt_is_about_980_characters(self):
        from backend.ai import prompts
        self.assertLessEqual(len(prompts.system_prompt(None, False, chat=False)), 1050)

    def test_minimal_profile_is_seven_core_tools(self):
        from backend import profiles
        core = [t for t in agent.TOOLS
                if t["function"]["name"] in profiles.CORE_TOOLS]
        self.assertEqual(len(core), 7)
        cost = profiles.cost_of(profiles.BUILTIN["minimal"])
        self.assertEqual(cost["tool_count"], 7)
        self.assertLess(cost["total"], 900)

    def test_default_profile_tool_count_matches_the_readme(self):
        from backend import profiles
        cost = profiles.cost_of(profiles.BUILTIN["silent"])
        self.assertEqual(cost["tool_count"], len(agent.TOOLS))
        self.assertEqual(len(agent.TOOLS), 24)

    def test_memory_profiles_report_their_plugin_tools(self):
        # The README's profile table said ~2,100 for these; the memory vault adds
        # five tool schemas, and a profile must not understate its own price.
        from backend import profiles
        silent = profiles.cost_of(profiles.BUILTIN["silent"])["total"]
        for name in ("safe", "power-isolation", "power-memory", "full"):
            cost = profiles.cost_of(profiles.BUILTIN[name])
            self.assertGreater(cost["total"], silent, name)
            self.assertGreater(cost["plugins"], 0, name)

    def test_token_estimator_is_labelled_when_inexact(self):
        if not tokens.exact():
            self.assertTrue(tokens.label(1234).startswith("~"))


# ── 12. the loop itself, end to end ───────────────────────────────────────
class ScriptedEngine:
    """Stands in for the model so run_turn can be driven without a provider.

    A closing report is requested with ``tools=None``, and a real model in that
    position can only produce prose. Answering it with another tool call would
    model a provider that ignores the request, so the final text step is served
    whenever no tools are offered.
    """

    def __init__(self, script, closing=None):
        self.script = script
        self.closing = closing if closing is not None else _text_step("closing report")
        self.step = 0
        self.prompts = []
        self.no_tool_calls = 0

    def __call__(self, messages, **kwargs):
        self.prompts.append(messages)
        if kwargs.get("tools") is None:
            self.no_tool_calls += 1
            for event in self.closing:
                yield event
            return
        index = min(self.step, len(self.script) - 1)
        self.step += 1
        for event in self.script[index]:
            yield event


def _tool_step(*calls):
    return [{"type": "tool_calls", "calls": [
        {"id": cid, "name": name, "arguments": json.dumps(args)}
        for cid, name, args in calls]},
        {"type": "usage", "usage": {"input": 100, "output": 5, "total": 105}},
        {"type": "done", "model": {"ref": "p/m"}}]


def _text_step(text):
    return [{"type": "text", "delta": text},
            {"type": "usage", "usage": {"input": 120, "output": 8, "total": 128}},
            {"type": "done", "model": {"ref": "p/m"}}]


class TestRunTurnWiring(ProjectFixture):
    """The pieces above only matter if the loop actually connects them."""

    def _drive(self, script, **kwargs):
        fake = ScriptedEngine(script)
        orig = agent.engine.stream_chat
        agent.engine.stream_chat = fake
        self.addCleanup(setattr, agent.engine, "stream_chat", orig)
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "read alpha.py"}]
        trace = []
        events = list(agent.run_turn(msgs, project=self.proj(), ref="p/m",
                                     trace=trace, **kwargs))
        return events, trace, fake

    def test_every_tool_call_reaches_the_ledger_with_its_session(self):
        self._drive([_tool_step(("c1", "read_file", {"path": "alpha.py"}),
                                ("c2", "list_files", {"path": "."})),
                     _text_step("done")], session="SID-1")
        rows = [r for r in audit.recent(20) if r["event"] == "tool_call"]
        self.assertEqual({r["tool"] for r in rows}, {"read_file", "list_files"})
        self.assertEqual({r["session"] for r in rows}, {"SID-1"})
        self.assertTrue(all(r["status"] == "ok" for r in rows))

    def test_a_failed_call_is_recorded_as_an_error(self):
        self._drive([_tool_step(("c1", "read_file", {"path": "absent.py"})),
                     _text_step("done")], session="SID-2")
        rows = [r for r in audit.recent(20) if r["event"] == "tool_call"]
        self.assertEqual(rows[0]["status"], "error")

    def test_readonly_caller_is_refused_at_the_call_not_just_the_schema(self):
        events, _, _ = self._drive(
            [_tool_step(("c1", "snapshot", {"label": "x"})), _text_step("done")],
            session="SID-3", readonly=True)
        ended = [e for e in events if e["type"] == "tool_end"]
        self.assertTrue(ended[0]["is_error"])
        self.assertIn("read-only", ended[0]["result"])
        # ...and nothing was written to the checkpoint store to refuse it.
        self.assertEqual(list(config.CHECKPOINT_DIR.iterdir()), [])

    def test_the_task_reaches_the_model_on_every_step(self):
        # Pinning only messages[0] let elision drop the question. Drive a
        # transcript large enough to force the window and check the prompt.
        fake_budget = config.AGENT_CONTEXT_BUDGET
        self.addCleanup(setattr, config, "AGENT_CONTEXT_BUDGET", fake_budget)
        config.AGENT_CONTEXT_BUDGET = 400
        orig = agent.context_budget
        agent.context_budget = lambda ref=None, rec=None: 400
        self.addCleanup(setattr, agent, "context_budget", orig)
        big = [{"role": "system", "content": "standing prompt"},
               {"role": "user", "content": "MAP THE RUNTIME LOOP"}]
        big += [{"role": "assistant", "content": "w" * 300} for _ in range(12)]
        fake = ScriptedEngine([_text_step("done")])
        orig_stream = agent.engine.stream_chat
        agent.engine.stream_chat = fake
        self.addCleanup(setattr, agent.engine, "stream_chat", orig_stream)
        list(agent.run_turn(big, project=self.proj(), ref="p/m"))
        sent = " ".join(m.get("content") or "" for m in fake.prompts[0])
        self.assertIn("MAP THE RUNTIME LOOP", sent)
        self.assertIn("standing prompt", sent)


# ── 13. the cost governor ────────────────────────────────────────────────
class TestMidTurnCompaction(ProjectFixture):
    """A long turn must compact as it grows, not elide at the end of one."""

    def test_compact_messages_keeps_the_pinned_prefix_and_tail(self):
        msgs = ([{"role": "system", "content": "standing prompt"},
                 {"role": "user", "content": "THE TASK STATEMENT"}]
                + [{"role": "assistant", "content": f"step {i} " + "w" * 500} for i in range(40)])
        orig = agent.engine.chat
        agent.engine.chat = lambda m, **kw: {"content": "SUMMARY OF THE WORK"}
        self.addCleanup(setattr, agent.engine, "chat", orig)
        # A budget well under the transcript's size, so there is a real tail split.
        info = agent.compact_messages(msgs, ref="p/m", budget_chars=20000)
        self.assertIsNotNone(info)
        self.assertGreater(info["kept"], 0)
        self.assertLess(info["kept"], 40)
        self.assertLess(info["chars_after"], info["chars_before"])
        self.assertEqual(msgs[0]["content"], "standing prompt")
        self.assertIn("THE TASK STATEMENT", msgs[1]["content"])
        self.assertIn("SUMMARY OF THE WORK", " ".join(m["content"] for m in msgs))
        self.assertEqual(len(msgs), 2 + 1 + info["kept"])

    def test_the_tail_is_a_token_budget_not_a_message_count(self):
        # Six one-line answers and six huge tool results are not the same tail.
        # The budget keeps more of the cheap one and less of the expensive one.
        cheap = [{"role": "assistant", "content": f"ok {i}"} for i in range(40)]
        dear = []
        for i in range(20):
            dear.append({"role": "assistant", "content": "",
                         "tool_calls": [{"id": f"c{i}", "type": "function",
                                         "function": {"name": "grep_files",
                                                      "arguments": "{}"}}]})
            dear.append({"role": "tool", "tool_call_id": f"c{i}", "content": "z" * 9000})
        _, keep_cheap = agent._tail_split(cheap, 2000)
        old_dear, keep_dear = agent._tail_split(dear, 2000)
        self.assertGreater(len(keep_cheap), len(keep_dear))
        self.assertGreater(len(keep_dear), 0)
        self.assertGreater(len(old_dear), 0)
        # The tail opens on the call, not on its orphaned result.
        self.assertNotEqual(keep_dear[0]["role"], "tool")

    def test_a_tail_of_only_orphaned_results_is_not_emptied(self):
        # Degenerate, but dropping every result to satisfy the pairing rule would
        # lose more than the compaction saves.
        orphans = [{"role": "tool", "tool_call_id": f"c{i}", "content": "z" * 500}
                   for i in range(20)]
        old, tail = agent._tail_split(orphans, 2000)
        self.assertEqual(old, [])
        self.assertEqual(len(tail), 20)

    def test_the_tail_never_exceeds_its_share_of_a_small_window(self):
        # An absolute 8,000-token tail is bigger than an 8K model's whole usable
        # budget; a tail that swallows everything leaves nothing to summarise.
        self.assertLessEqual(agent._tail_budget(4000), int(4000 * config.COMPACT_TAIL_SHARE))
        self.assertEqual(agent._tail_budget(1_000_000), config.COMPACT_TAIL_TOKENS)

    def test_compact_messages_refuses_when_there_is_nothing_to_summarise(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
        self.assertIsNone(agent.compact_messages(msgs, ref="p/m"))

    def test_a_tail_that_fits_whole_is_not_summarised(self):
        # Nothing to reclaim, so no model call is spent finding that out.
        msgs = ([{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
                + [{"role": "assistant", "content": "short"} for _ in range(12)])
        called = []
        orig = agent.engine.chat
        agent.engine.chat = lambda m, **kw: (called.append(1), {"content": "S"})[1]
        self.addCleanup(setattr, agent.engine, "chat", orig)
        self.assertIsNone(agent.compact_messages(msgs, ref="p/m", budget_chars=200000))
        self.assertEqual(called, [])

    def test_digest_wire_describes_calls_and_results(self):
        rows = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "the file body"}]
        text = "\n".join(agent._digest_wire(m) for m in rows)
        self.assertIn("read_file", text)
        self.assertIn("a.py", text)
        self.assertIn("the file body", text)

    def test_wire_chars_measures_the_whole_message(self):
        msgs = [{"role": "tool", "tool_call_id": "c", "content": "z" * 5000}]
        self.assertGreaterEqual(agent.wire_chars(msgs), 5000)

    def test_mid_turn_compaction_fires_and_reaches_the_ledger(self):
        # Force the budget tiny so one step crosses it, and stub the summariser.
        orig_budget, orig_chat = agent.context_budget, agent.engine.chat
        agent.context_budget = lambda ref=None, rec=None: 1500
        agent.engine.chat = lambda m, **kw: {"content": "SUMMARY"}
        self.addCleanup(setattr, agent, "context_budget", orig_budget)
        self.addCleanup(setattr, agent.engine, "chat", orig_chat)
        steps = [[{"type": "tool_calls", "calls": [
            {"id": f"c{i}", "name": "read_file",
             "arguments": json.dumps({"path": "long.txt", "offset": i * 40})}]},
            {"type": "usage", "usage": {"input": 800, "output": 10, "total": 810}},
            {"type": "done", "model": {"ref": "p/m"}}] for i in range(6)]
        steps.append(_text_step("done"))
        fake = ScriptedEngine(steps)
        orig_stream = agent.engine.stream_chat
        agent.engine.stream_chat = fake
        self.addCleanup(setattr, agent.engine, "stream_chat", orig_stream)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "read it"}]
        events = list(agent.run_turn(msgs, project=self.proj(), ref="p/m", session="S"))
        kinds = [e["type"] for e in events]
        self.assertIn("compaction", kinds)
        comp = next(e for e in events if e["type"] == "compaction")
        self.assertGreater(comp["saved"], 0)
        self.assertGreater(comp["kept"], 0)
        # The pinned task survived the compaction.
        self.assertIn("read it", " ".join(m.get("content") or "" for m in msgs))

    def test_mid_turn_compaction_can_be_switched_off(self):
        orig_budget, orig_chat, orig_flag = (agent.context_budget, agent.engine.chat,
                                             config.COMPACT_MID_TURN)
        agent.context_budget = lambda ref=None, rec=None: 1500
        agent.engine.chat = lambda m, **kw: {"content": "SUMMARY"}
        config.COMPACT_MID_TURN = False
        self.addCleanup(setattr, agent, "context_budget", orig_budget)
        self.addCleanup(setattr, agent.engine, "chat", orig_chat)
        self.addCleanup(setattr, config, "COMPACT_MID_TURN", orig_flag)
        fake = ScriptedEngine([_tool_step(("c1", "read_file", {"path": "long.txt"})),
                               _tool_step(("c2", "read_file", {"path": "long.txt"})),
                               _text_step("done")])
        orig_stream = agent.engine.stream_chat
        agent.engine.stream_chat = fake
        self.addCleanup(setattr, agent.engine, "stream_chat", orig_stream)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "read it"}]
        events = list(agent.run_turn(msgs, project=self.proj(), ref="p/m"))
        self.assertNotIn("compaction", [e["type"] for e in events])

    def test_compaction_needs_a_tail_worth_keeping(self):
        # A transcript shorter than the tail floor has nothing to summarise, and
        # must not spend a model call finding that out.
        msgs = ([{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
                + [{"role": "assistant", "content": "w" * 900} for _ in range(3)])
        called = []
        orig = agent.engine.chat
        agent.engine.chat = lambda m, **kw: (called.append(1), {"content": "S"})[1]
        self.addCleanup(setattr, agent.engine, "chat", orig)
        self.assertIsNone(agent.compact_messages(msgs, ref="p/m"))
        self.assertEqual(called, [])


class TestCostGovernor(ProjectFixture):
    """Showing what it costs has to hold while the turn is running, not after."""

    def _run_with_window(self, window, usage_total, budget=0):
        orig_resolve, orig_stream, orig_budget = (config.resolve_model,
                                                  agent.engine.stream_chat,
                                                  config.TURN_TOKEN_BUDGET)
        config.resolve_model = lambda ref=None: {"contextWindow": window,
                                                 "reasoning": False}
        config.TURN_TOKEN_BUDGET = budget
        self.addCleanup(setattr, config, "resolve_model", orig_resolve)
        self.addCleanup(setattr, agent.engine, "stream_chat", orig_stream)
        self.addCleanup(setattr, config, "TURN_TOKEN_BUDGET", orig_budget)
        steps = [[{"type": "tool_calls", "calls": [
            {"id": f"c{i}", "name": "list_files",
             "arguments": json.dumps({"path": "."})}]},
            {"type": "usage", "usage": {"input": usage_total, "output": 10,
                                        "total": usage_total + 10}},
            {"type": "done", "model": {"ref": "p/m"}}] for i in range(4)]
        steps.append(_text_step("done"))
        fake = ScriptedEngine(steps)
        agent.engine.stream_chat = fake
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
        return list(agent.run_turn(msgs, project=self.proj(), ref="p/m"))

    def test_warns_at_the_configured_fractions_of_the_window(self):
        events = self._run_with_window(window=1000, usage_total=300)
        warns = [e["message"] for e in events if e["type"] == "notify"
                 and e.get("level") == "warn" and "billed" in (e.get("message") or "")]
        self.assertTrue(warns, "no cost warning was emitted")
        self.assertIn("25%", warns[0])
        # Each fraction warns once, not on every subsequent step.
        self.assertEqual(len([w for w in warns if "25%" in w]), 1)

    def test_a_sub_percent_threshold_is_still_legible(self):
        # int(0.002 * 100) is 0, which read as "0% of the window" — a warning that
        # looks like a rounding error is a warning nobody acts on.
        orig = config.COST_WARN_FRACTIONS
        config.COST_WARN_FRACTIONS = (0.002,)
        self.addCleanup(setattr, config, "COST_WARN_FRACTIONS", orig)
        events = self._run_with_window(window=1_000_000, usage_total=5000)
        warns = [e["message"] for e in events if e["type"] == "notify"
                 and "billed" in (e.get("message") or "")]
        self.assertTrue(warns)
        self.assertIn("0.2%", warns[0])

    def test_no_warning_while_the_turn_is_cheap(self):
        events = self._run_with_window(window=1_000_000, usage_total=50)
        warns = [e for e in events if e["type"] == "notify" and e.get("level") == "warn"
                 and "billed" in (e.get("message") or "")]
        self.assertEqual(warns, [])

    def test_turn_budget_stops_the_turn_and_still_reports(self):
        events = self._run_with_window(window=1_000_000, usage_total=5000, budget=6000)
        kinds = [e["type"] for e in events]
        msgs = [e["message"] for e in events if e["type"] == "notify"]
        self.assertTrue(any("turn budget reached" in (m or "") for m in msgs), msgs)
        self.assertEqual(kinds[-1], "done")
        # It reports rather than stopping silent.
        self.assertTrue(any(e["type"] == "text" for e in events))


# ── 14. snapshots happen without being asked for ──────────────────────────
class TestAutoSnapshot(ProjectFixture):
    """README: the agent takes a snapshot before risky edits."""

    def test_first_edit_of_a_turn_snapshots_the_project(self):
        fake = ScriptedEngine([
            _tool_step(("c1", "write_file", {"path": "new.txt", "content": "hello"}),
                       ("c2", "write_file", {"path": "new2.txt", "content": "hi"})),
            _text_step("wrote two files")])
        orig = agent.engine.stream_chat
        agent.engine.stream_chat = fake
        self.addCleanup(setattr, agent.engine, "stream_chat", orig)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "write"}]
        events = list(agent.run_turn(msgs, project=self.proj(), ref="p/m", session="S"))
        notes = [e["message"] for e in events if e["type"] == "notify"]
        self.assertTrue(any("snapshot taken" in (n or "") for n in notes), notes)
        # Once per turn, not once per edit.
        self.assertEqual(len([n for n in notes if "snapshot taken" in (n or "")]), 1)
        saved = list(config.CHECKPOINT_DIR.iterdir())
        self.assertEqual(len(saved), 1)
        self.assertTrue((self.root / "new.txt").exists())
        # The snapshot is attributed to the session that caused it, so the ledger
        # can be filtered per conversation.
        rows = [r for r in audit.recent(20) if r["event"] == "auto_snapshot"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session"], "S")
        self.assertEqual(rows[0]["tool"], "write_file")

    def test_a_read_only_turn_takes_no_snapshot(self):
        fake = ScriptedEngine([_tool_step(("c1", "read_file", {"path": "alpha.py"})),
                               _text_step("read it")])
        orig = agent.engine.stream_chat
        agent.engine.stream_chat = fake
        self.addCleanup(setattr, agent.engine, "stream_chat", orig)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "read"}]
        list(agent.run_turn(msgs, project=self.proj(), ref="p/m"))
        self.assertEqual(list(config.CHECKPOINT_DIR.iterdir()), [])

    def test_no_project_means_no_snapshot_of_the_home_directory(self):
        fake = ScriptedEngine([_tool_step(("c1", "write_file",
                                          {"path": "x.txt", "content": "x"})),
                               _text_step("done")])
        orig = agent.engine.stream_chat
        agent.engine.stream_chat = fake
        self.addCleanup(setattr, agent.engine, "stream_chat", orig)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "w"}]
        events = list(agent.run_turn(msgs, project=None, ref="p/m"))
        notes = [e.get("message") or "" for e in events if e["type"] == "notify"]
        self.assertFalse(any("snapshot taken" in n for n in notes))

    def test_a_failed_snapshot_does_not_block_the_edit(self):
        orig_snap = agent.extras.snapshot
        agent.extras.snapshot = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("disk full"))
        self.addCleanup(setattr, agent.extras, "snapshot", orig_snap)
        fake = ScriptedEngine([_tool_step(("c1", "write_file",
                                          {"path": "new.txt", "content": "hello"})),
                               _text_step("done")])
        orig = agent.engine.stream_chat
        agent.engine.stream_chat = fake
        self.addCleanup(setattr, agent.engine, "stream_chat", orig)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "w"}]
        events = list(agent.run_turn(msgs, project=self.proj(), ref="p/m"))
        ended = [e for e in events if e["type"] == "tool_end"]
        self.assertFalse(ended[0]["is_error"], ended[0]["result"])
        self.assertTrue((self.root / "new.txt").exists())


# ── 15. guidance does not anchor on "what you changed" ────────────────────
class TestGuidanceWording(unittest.TestCase):
    def test_budget_and_closing_lead_with_the_finding(self):
        for text in (guidance._BUDGET, guidance._CLOSING):
            self.assertLess(text.index("finding"), text.index("changed"),
                            "the report prompt still leads with 'what you changed'")

    def test_a_timeout_gets_its_own_advice(self):
        block = guidance.for_step(step=1, steps=24,
                                  errors=[("run_shell", "ERROR: timed out after 180s")])
        self.assertIn("timed out", block)
        self.assertIn("Do not re-run the same command shape", block)

    def test_an_ordinary_failure_gets_the_ordinary_card(self):
        block = guidance.for_step(step=1, steps=24,
                                  errors=[("read_file", "ERROR: does not exist")])
        self.assertIn("read_file", block)
        self.assertNotIn("pipe buffer", block)


# ── 16. the compaction trigger follows the window, not a flat fraction ────
class TestReserveTrigger(unittest.TestCase):
    """What must stay free is room for the next turn, which is roughly constant."""

    def test_large_window_fills_almost_completely(self):
        # Claude Code, read out of 2.1.222: a 1M window triggers near 967,000.
        target = config.context_fill_target(1_000_000)
        self.assertEqual(target, 1_000_000 - config.CONTEXT_RESERVE_TOKENS)
        self.assertGreater(target / 1_000_000, 0.95)

    def test_200k_window_lands_near_83_percent(self):
        target = config.context_fill_target(200_000)
        self.assertEqual(target, 200_000 - config.CONTEXT_RESERVE_TOKENS)
        self.assertAlmostEqual(target / 200_000, 0.835, places=2)

    def test_a_window_smaller_than_the_reserve_is_floored_not_negative(self):
        # Subtracting 33K from an 8K window is nonsense; the floor takes over and
        # the model still gets half its context to work in.
        self.assertEqual(config.context_fill_target(8_000), 4_000)
        self.assertEqual(config.context_fill_target(32_000), 16_000)

    def test_a_medium_window_is_not_over_filled(self):
        # Where the reserve is small relative to the window it still binds, and the
        # free space is exactly the reserve — never less.
        target = config.context_fill_target(40_000)
        self.assertEqual(target, 20_000)      # floored at half
        self.assertEqual(config.context_fill_target(100_000),
                         100_000 - config.CONTEXT_RESERVE_TOKENS)

    def test_an_explicit_fraction_overrides_the_reserve_model(self):
        orig = config.COMPACT_AT
        config.COMPACT_AT = 0.65
        self.addCleanup(setattr, config, "COMPACT_AT", orig)
        self.assertEqual(config.context_fill_target(1_000_000), 650_000)

    def test_unknown_window_has_no_target(self):
        self.assertEqual(config.context_fill_target(0), 0)

    def test_the_reported_reserve_matches_the_trigger(self):
        # The interface shows a reserve; it has to be the same arithmetic the
        # compactor uses, or the bar and the behaviour disagree.
        for window in (8_000, 32_000, 200_000, 1_000_000):
            reserve = window - config.context_fill_target(window)
            self.assertGreaterEqual(reserve, 0)
            self.assertLessEqual(reserve, window)


class TestCompactionMechanics(Isolated):
    """The cheap pre-pass, and what happens when the summariser fails."""

    def test_old_tool_output_is_pruned_before_paying_for_a_summary(self):
        old = [{"role": "assistant", "content": "read it",
                "tools": [{"id": "c1", "name": "grep_files", "args": {},
                           "result": "z" * 40000, "is_error": False}]}]
        reclaimed = agent._prune_old_output(old)
        self.assertGreater(reclaimed, 30000)
        self.assertIn("cleared to save context", old[0]["tools"][0]["result"])

    def test_small_results_are_left_alone(self):
        old = [{"role": "assistant", "content": "x",
                "tools": [{"id": "c1", "name": "list_files", "args": {},
                           "result": "a.py\nb.py", "is_error": False}]}]
        self.assertEqual(agent._prune_old_output(old), 0)
        self.assertEqual(old[0]["tools"][0]["result"], "a.py\nb.py")

    def test_wire_shape_results_are_pruned_too(self):
        old = [{"role": "tool", "tool_call_id": "c1", "content": "z" * 40000}]
        self.assertGreater(agent._prune_old_output(old), 30000)
        self.assertIn("cleared to save context", old[0]["content"])

    def test_a_failed_summariser_falls_back_instead_of_doing_nothing(self):
        # Returning None here meant the transcript stayed full, the window cap
        # elided it, and the work was lost with no record of why.
        orig = agent.engine.chat

        def boom(*a, **kw):
            raise agent.engine.EngineError("HTTP 503: summariser down")
        agent.engine.chat = boom
        self.addCleanup(setattr, agent.engine, "chat", orig)
        digest = "user: fix calc.py\n  tool edit_file {'path': 'src/calc.py'}\n  tool run_shell FAILED"
        summary, err = agent._summarise(digest, ref="p/m")
        self.assertIn("summariser was unavailable", summary)
        self.assertIn("src/calc.py", summary)
        self.assertIn("FAILED", summary)
        self.assertIn("503", err)

    def test_an_empty_summary_also_falls_back(self):
        orig = agent.engine.chat
        agent.engine.chat = lambda *a, **kw: {"content": "   "}
        self.addCleanup(setattr, agent.engine, "chat", orig)
        summary, err = agent._summarise("user: do x\n  tool read_file {'path': 'a/b.py'}")
        self.assertIn("a/b.py", summary)
        self.assertTrue(err)

    def test_fallback_extracts_paths_and_tool_tally(self):
        digest = ("user: investigate\n  tool read_file {'path': 'backend/agent.py'}\n"
                  "  tool grep_files {'pattern': 'x'}\n  tool read_file {'path': 'README.md'}")
        out = agent._fallback_summary(digest)
        self.assertIn("backend/agent.py", out)
        self.assertIn("README.md", out)
        self.assertIn("read_file x2", out)

    def test_the_summary_input_is_capped(self):
        # The summariser call must not itself overflow the window it relieves.
        seen = []
        orig = agent.engine.chat
        agent.engine.chat = lambda m, **kw: (seen.append(m), {"content": "s"})[1]
        self.addCleanup(setattr, agent.engine, "chat", orig)
        agent._summarise("q" * (config.SUMMARY_INPUT_MAX_CHARS + 50000))
        sent = seen[0][1]["content"]
        self.assertLessEqual(len(sent), config.SUMMARY_INPUT_MAX_CHARS)

    def test_a_nominated_cheap_model_does_the_summarising(self):
        seen = []
        orig = agent.engine.chat
        agent.engine.chat = lambda m, **kw: (seen.append(kw.get("ref")),
                                             {"content": "s"})[1]
        self.addCleanup(setattr, agent.engine, "chat", orig)
        orig_model = config.COMPACT_MODEL
        config.COMPACT_MODEL = "cheap/summariser"
        self.addCleanup(setattr, config, "COMPACT_MODEL", orig_model)
        agent._summarise("digest", ref="expensive/working-model")
        self.assertEqual(seen[0], "cheap/summariser")

    def test_the_working_model_is_used_when_none_is_nominated(self):
        seen = []
        orig = agent.engine.chat
        agent.engine.chat = lambda m, **kw: (seen.append(kw.get("ref")),
                                             {"content": "s"})[1]
        self.addCleanup(setattr, agent.engine, "chat", orig)
        agent._summarise("digest", ref="p/m")
        self.assertEqual(seen[0], "p/m")


class TestSummaryFraming(unittest.TestCase):
    """A summary that reads as instructions gets acted on, not consulted."""

    def test_the_marker_says_reference_only(self):
        from backend.ai import prompts
        self.assertIn("reference only", prompts.SUMMARISED.lower())
        self.assertIn("not a new instruction", prompts.SUMMARISED.lower())

    def test_the_marker_prefix_is_stable_for_old_sessions(self):
        # agent._is_summary recognises summaries written by earlier builds, so the
        # prefix cannot change without orphaning them.
        from backend.ai import prompts
        self.assertEqual(prompts.SUMMARY_MARK, "[Compacted history")
        self.assertTrue(prompts.SUMMARISED.startswith(prompts.SUMMARY_MARK))
        self.assertTrue(agent._is_summary({"role": "system",
                                          "content": prompts.SUMMARISED + "\nthe work"}))
        self.assertTrue(agent._is_summary({"role": "summary",
                                          "content": "[Compacted history of earlier work]"}))
        self.assertFalse(agent._is_summary({"role": "system", "content": "standing prompt"}))

    def test_the_compact_prompt_forbids_next_step_language(self):
        from backend.ai import prompts
        self.assertIn("not a set of instructions", prompts.COMPACT)
        self.assertIn("Resolved", prompts.COMPACT)
        self.assertIn("Pending", prompts.COMPACT)
        self.assertIn("verbatim", prompts.COMPACT)


# ── 17. what the provider served from cache is reported, not discarded ────
class TestCacheAccounting(Isolated):
    """A turn re-sends its transcript every step, so cache hits are most of the bill."""

    def test_openai_shape_cache_read_is_extracted(self):
        u = engine._usage({"prompt_tokens": 5000, "completion_tokens": 100,
                           "total_tokens": 5100,
                           "prompt_tokens_details": {"cached_tokens": 4608}})
        self.assertEqual(u["cache_read"], 4608)
        self.assertEqual(u["cache_write"], 0)

    def test_anthropic_shape_both_directions_are_extracted(self):
        u = engine._usage({"prompt_tokens": 5000, "completion_tokens": 100,
                           "total_tokens": 5100,
                           "cache_read_input_tokens": 4000,
                           "cache_creation_input_tokens": 900})
        self.assertEqual(u["cache_read"], 4000)
        self.assertEqual(u["cache_write"], 900)

    def test_no_cache_information_is_not_an_error(self):
        u = engine._usage({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
        self.assertEqual(u["cache_read"], 0)
        self.assertEqual(u["cache_write"], 0)

    def test_a_malformed_details_block_is_tolerated(self):
        u = engine._usage({"prompt_tokens": 10, "completion_tokens": 2,
                           "total_tokens": 12, "prompt_tokens_details": "nonsense"})
        self.assertEqual(u["cache_read"], 0)

    def test_cache_figures_accumulate_on_the_session(self):
        orig = chat.config.resolve_model
        self.addCleanup(setattr, chat.config, "resolve_model", orig)
        chat.config.resolve_model = lambda ref=None: {"contextWindow": 100000}
        rec = {"id": "s1", "model": "p/m", "messages": [{"role": "user", "content": "hi"}]}
        chat._usage_event(rec, {"input": 5000, "output": 100, "total": 5100,
                                "cache_read": 4600, "cache_write": 300})
        chat._usage_event(rec, {"input": 6000, "output": 100, "total": 6100,
                                "cache_read": 5500, "cache_write": 0})
        tok = rec["usage"]["tokens"]
        self.assertEqual(tok["cacheRead"], 10100)
        self.assertEqual(tok["cacheWrite"], 300)
        self.assertEqual(rec["metrics"]["cached_tokens"], 10100)

    def test_the_dashboard_reports_a_hit_rate(self):
        rec = {"id": "s1", "model": "p/m", "messages": [],
               "metrics": {"prompt_tokens": 10000, "cached_tokens": 8000}}
        d = metrics.dashboard(rec)
        self.assertEqual(d["cached_tokens"], 8000)
        self.assertEqual(d["cache_hit_rate"], 80.0)

    def test_a_hit_rate_is_not_a_division_by_zero(self):
        d = metrics.dashboard({"id": "s", "messages": []})
        self.assertEqual(d["cache_hit_rate"], 0.0)

    def test_the_snapshot_carries_the_cache_fields_to_the_interface(self):
        rec = {"id": "s1", "model": "p/m", "messages": [],
               "usage": {"tokens": {"input": 1, "output": 1, "total": 2,
                                    "cacheRead": 500, "cacheWrite": 20}}}
        snap = chat._usage_snapshot(rec)
        self.assertEqual(snap["tokens"]["cacheRead"], 500)
        self.assertEqual(snap["tokens"]["cacheWrite"], 20)


# ── 18. project instruction files ─────────────────────────────────────────
class TestProjectContext(unittest.TestCase):
    """Found rather than guessed at, and priced before it is switched on."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def write(self, name, text):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_finds_conventional_instruction_files(self):
        self.write("AGENTS.md", "# Rules\nnever touch legacy/\n")
        self.write("CLAUDE.md", "# Claude\n")
        names = [f["name"] for f in project_context.discover(str(self.root))]
        self.assertEqual(names[:2], ["AGENTS.md", "CLAUDE.md"])

    def test_finds_a_readme_when_there_are_no_instructions(self):
        # Most real projects have no AGENTS.md and put everything in the README, so
        # a scan that ignored it reported finding nothing at all.
        self.write("README.md", "# Project\n" + "body\n" * 500)
        found = project_context.discover(str(self.root))
        self.assertEqual([f["name"] for f in found], ["README.md"])
        self.assertEqual(found[0]["kind"], "docs")

    def test_no_project_and_no_files_are_both_empty(self):
        self.assertEqual(project_context.discover(None), [])
        self.assertEqual(project_context.discover(str(self.root)), [])
        self.assertEqual(project_context.block(str(self.root))["text"], "")

    def test_index_mode_is_cheaper_than_the_file_it_names(self):
        self.write("README.md", "# Project\n" + "body\n" * 3000)
        block = project_context.block(str(self.root),
                                      {"mode": "index", "budget": 1200})
        self.assertIn("README.md", block["text"])
        self.assertLess(block["tokens"], 80)
        self.assertGreater(block["held_back"], 1000)

    def test_off_mode_injects_nothing(self):
        self.write("AGENTS.md", "# Rules\n")
        block = project_context.block(str(self.root), {"mode": "off", "budget": 1200})
        self.assertEqual(block["text"], "")
        self.assertEqual(block["tokens"], 0)

    def test_inline_mode_respects_its_budget_and_reports_what_it_held_back(self):
        self.write("AGENTS.md", "short rules\n")
        self.write("README.md", "# Big\n" + "x\n" * 9000)
        block = project_context.block(str(self.root), {"mode": "inline", "budget": 300})
        self.assertIn("AGENTS.md", block["text"])
        self.assertNotIn("# Big", block["text"])
        self.assertGreater(block["held_back"], 0)

    def test_inline_prefers_instructions_over_documentation(self):
        self.write("README.md", "# Big\n" + "x\n" * 4000)
        self.write("AGENTS.md", "# Rules\nthe real instructions\n")
        block = project_context.block(str(self.root), {"mode": "inline", "budget": 400})
        self.assertIn("the real instructions", block["text"])

    def test_the_preview_prices_every_mode_before_you_choose(self):
        self.write("AGENTS.md", "# Rules\n" + "y\n" * 200)
        prev = project_context.cost_preview(str(self.root))
        self.assertEqual(prev["found"], 1)
        self.assertEqual(prev["cost_off"], 0)
        self.assertLess(prev["cost_index"], prev["cost_inline"])
        self.assertEqual(prev["modes"], ["off", "index", "inline"])

    def test_an_unknown_mode_falls_back_to_the_default(self):
        from backend import providers
        orig = providers.load
        providers.load = lambda: {"context": {"instructions": "nonsense"}}
        self.addCleanup(setattr, providers, "load", orig)
        self.assertEqual(project_context.settings()["mode"], project_context.DEFAULT_MODE)


# ── 19. task state outside the context window ─────────────────────────────
class TestTaskList(Isolated):
    """Session state, so it survives compaction by construction."""

    def test_an_empty_list_costs_nothing(self):
        block = task_list.block("s1")
        self.assertEqual(block["text"], "")
        self.assertEqual(block["tokens"], 0)

    def test_add_done_and_list(self):
        task_list.add("s1", "read the README")
        task_list.add("s1", "fix calc.py")
        out = task_list.call("tasks", {"action": "done", "index": 1}, {"session": "s1"})
        self.assertIn("[x] read the README", out)
        self.assertIn("[ ] fix calc.py", out)
        self.assertIn("1 open of 2", out)

    def test_a_duplicate_open_task_is_refused(self):
        task_list.add("s1", "do the thing")
        res = task_list.add("s1", "Do The Thing")
        self.assertFalse(res["ok"])
        self.assertIn("already open", res["error"])

    def test_an_out_of_range_index_is_an_error_not_a_silent_noop(self):
        task_list.add("s1", "only task")
        out = task_list.call("tasks", {"action": "done", "index": 9}, {"session": "s1"})
        self.assertTrue(out.startswith("ERROR:"), out)

    def test_the_list_lives_outside_the_transcript(self):
        # The point of it: a compaction rewrites the messages, and this is not in
        # them, so it cannot be summarised away.
        task_list.add("s1", "survive compaction")
        rec = {"id": "s1", "messages": [{"role": "user", "content": "go"}]}
        agent.compact_history(rec, force=True)
        self.assertIn("survive compaction", task_list.render("s1"))

    def test_the_block_is_budgeted_and_drops_finished_items_first(self):
        for i in range(30):
            task_list.add("s1", f"task number {i} " + "padding " * 12)
            task_list.set_done("s1", i + 1, True)
        task_list.add("s1", "the one still open")
        block = task_list.block("s1")
        self.assertLessEqual(block["tokens"], 400)
        self.assertIn("the one still open", block["text"])

    def test_sessions_do_not_share_a_list(self):
        task_list.add("s1", "only in s1")
        self.assertEqual(task_list.render("s2"), "")

    def test_the_tool_schema_is_small_enough_to_be_worth_it(self):
        cost = tokens.estimate_tools_tokens(task_list.tools())
        self.assertLess(cost, 200)


# ── 20. plugin pricing is measured, not declared ──────────────────────────
class TestPluginPricing(Isolated):
    def test_a_disabled_plugin_still_shows_what_it_would_cost(self):
        rows = {r["id"]: r for r in plugin_manager.token_impact()["rows"]}
        self.assertIn("memory_vault", rows)
        self.assertFalse(rows["memory_vault"]["enabled"])
        self.assertEqual(rows["memory_vault"]["tokens"], 0)
        self.assertGreater(rows["memory_vault"]["tokens_if_enabled"], 0)

    def test_an_enabled_plugin_reports_its_real_schema_cost(self):
        # It declared a budget of zero, and the panel believed it.
        plugin_manager.enable("memory_vault")
        rows = {r["id"]: r for r in plugin_manager.token_impact()["rows"]}
        measured = tokens.estimate_tools_tokens(plugin_manager.tool_schemas("memory_vault"))
        self.assertEqual(rows["memory_vault"]["tokens"], measured)
        self.assertGreater(measured, 0)
        self.assertEqual(plugin_manager.token_impact()["total"], measured)


# ── 21. independent delegations run concurrently ──────────────────────────
class TestParallelDelegation(Isolated):
    def test_two_delegations_overlap_rather_than_run_in_series(self):
        # A barrier both workers must reach. If they ran one after the other the
        # first would block forever and the join would time out.
        barrier = threading.Barrier(2, timeout=10)
        order = []

        def fake_call(name, args, ctx, out):
            barrier.wait()
            order.append(args.get("n"))
            out["result"] = f"report {args.get('n')}"
            return iter(())

        orig = agent.call_tool
        agent.call_tool = fake_call
        self.addCleanup(setattr, agent, "call_tool", orig)
        results = {}
        specs = [("c1", "task", {"n": 1}), ("c2", "task", {"n": 2})]
        list(agent._run_parallel(specs, {"session": "s"}, results))
        self.assertEqual(sorted(results), ["c1", "c2"])
        self.assertEqual(results["c1"]["result"], "report 1")
        self.assertEqual(results["c2"]["result"], "report 2")
        self.assertEqual(sorted(order), [1, 2])

    def test_a_failing_delegation_does_not_take_the_other_down(self):
        def fake_call(name, args, ctx, out):
            if args.get("boom"):
                raise RuntimeError("sub-agent exploded")
            out["result"] = "fine"
            return iter(())

        orig = agent.call_tool
        agent.call_tool = fake_call
        self.addCleanup(setattr, agent, "call_tool", orig)
        results = {}
        specs = [("c1", "task", {"boom": True}), ("c2", "task", {})]
        list(agent._run_parallel(specs, {}, results))
        self.assertTrue(results["c1"]["result"].startswith("ERROR:"))
        self.assertEqual(results["c2"]["result"], "fine")

    def test_a_single_delegation_is_not_threaded(self):
        # One call has nothing to overlap with, and a thread costs a queue.
        seen = []
        orig = agent._run_parallel
        agent._run_parallel = lambda *a, **kw: seen.append(1) or iter(())
        self.addCleanup(setattr, agent, "_run_parallel", orig)
        fake = ScriptedEngine([_tool_step(("c1", "task", {"prompt": "look"})),
                               _text_step("done")])
        orig_stream = agent.engine.stream_chat
        agent.engine.stream_chat = fake
        self.addCleanup(setattr, agent.engine, "stream_chat", orig_stream)
        orig_sub = agent.run_subagent

        def fake_sub(task, ctx, out):
            out["result"] = "a report"
            return iter(())
        agent.run_subagent = fake_sub
        self.addCleanup(setattr, agent, "run_subagent", fake_sub)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
        list(agent.run_turn(msgs, project=None, ref="p/m"))
        self.assertEqual(seen, [])
        del orig_sub


class TestSalvageWordingNamesEveryFile(unittest.TestCase):
    """A task scored on two files must be asked for both in the one round it has left."""

    def test_one_missing_file_is_asked_for_in_the_singular(self):
        out = guidance.salvage(["attack.py"])
        self.assertIn("attack.py is not written", out)
        self.assertIn("Write it now with write_file", out)

    def test_two_missing_files_are_both_asked_for_in_one_round(self):
        out = guidance.salvage(["task_file/output_data/plan_b1.jsonl",
                                "task_file/output_data/plan_b2.jsonl"])
        self.assertIn("plan_b1.jsonl", out)
        self.assertIn("plan_b2.jsonl", out)
        self.assertIn("every one of them", out)
        self.assertNotIn("Write it now", out, "the singular phrasing implies one file")

    def test_no_names_still_produces_an_instruction(self):
        self.assertIn("the file", guidance.salvage([]))


if __name__ == "__main__":
    unittest.main()

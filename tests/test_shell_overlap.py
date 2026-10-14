"""The shell/tool overlap table: refuse what is only a worse tool call, advise on
everything else, and stay silent where the tool cannot do the job.

The feature has to bite on the commands the agent actually types, so the cases
below are the ones from the recorded runs (`type`, `dir`, `findstr`, `cat` of a
whole file). The other half of the point is what must NOT be touched: a refusal
that blocks real work teaches the model to distrust the tool list, which is the
exact cost the deleted plan warns about.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import agent, audit, config, guidance, sandbox  # noqa: E402
from backend.routers import api  # noqa: E402
from tests.helpers import state_paths  # noqa: E402


class Isolated(unittest.TestCase):
    """Never read the operator's own prefs file to decide whether a test passed."""

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


class TestSwitchSurface(Isolated):
    """Settings > Tools: the two endpoints the Shell-overlap select reads/writes.

    The plan puts the switch on `/api/tools` and `/api/tools/toggle`, so the wire
    shape is part of the feature, not plumbing. A select bound to a field the
    server never sends renders `block`, saves nothing, and looks entirely correct.
    """

    def _body(self, res):
        """`_ok` answers a plain dict, `_fail` a JSONResponse — both are what the
        browser reads. Normalise, or a test passes by asserting on the wrong object."""
        return res if isinstance(res, dict) else json.loads(res.body.decode())

    def _get(self):
        return self._body(asyncio.run(api.get_tools()))

    def _toggle(self, payload):
        class Req:
            def __init__(self, b):
                self._b = b

            async def json(self):
                return self._b

        return self._body(asyncio.run(api.toggle_tool(Req(payload))))

    def test_the_endpoint_reports_the_mode_and_the_vocabulary(self):
        d = self._get()
        self.assertEqual(d["shellOverlap"], "block")   # the default, read from config
        self.assertEqual(list(d["shellOverlapModes"]), list(config.SHELL_OVERLAP_MODES))
        self.assertIn(d["shellOverlap"], d["shellOverlapModes"])
        self.assertFalse(d["shellOverlapPinned"])

    def test_saving_a_mode_moves_the_interception_itself(self):
        # The point is behaviour, not a stored string: `advise` has to stop
        # refusing, and the endpoint must be what changed it.
        r = self._toggle({"name": "shellOverlap", "mode": "advise"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(config.shell_overlap(), "advise")
        self.assertEqual(config.prefs().get("shellOverlap"), "advise")
        self.assertEqual(self._get()["shellOverlap"], "advise")

    def test_an_unknown_mode_is_refused_and_changes_nothing(self):
        r = self._toggle({"name": "shellOverlap", "mode": "sometimes"})
        self.assertFalse(r["ok"])
        self.assertIsNone(config.prefs().get("shellOverlap"))
        self.assertEqual(config.shell_overlap(), "block")

    def test_a_boolean_never_becomes_a_mode(self):
        # `enabled: true` is the tool-toggle shape. Coercing it would turn a
        # request for `off` into `block` somewhere in transit, silently.
        r = self._toggle({"name": "shellOverlap", "enabled": True})
        self.assertFalse(r["ok"])
        self.assertIsNone(config.prefs().get("shellOverlap"))

    def test_the_tool_toggle_still_takes_a_boolean(self):
        # The new branch must not swallow the old contract.
        r = self._toggle({"name": "run_shell", "enabled": False})
        self.assertTrue(r["ok"], r)
        self.assertIn("run_shell", self._get()["disabled"])

    def test_an_environment_override_says_so_instead_of_lying(self):
        os.environ["TACIT_SHELL_OVERLAP"] = "off"
        self.addCleanup(os.environ.pop, "TACIT_SHELL_OVERLAP", None)
        self.assertTrue(self._get()["shellOverlapPinned"])
        r = self._toggle({"name": "shellOverlap", "mode": "block"})
        self.assertFalse(r["ok"])
        self.assertIn("TACIT_SHELL_OVERLAP", r["error"])
        # Nothing written behind the override: a save that appears to succeed but
        # cannot take effect is the failure this whole branch exists to avoid.
        self.assertIsNone(config.prefs().get("shellOverlap"))


class TestOverlapTable(Isolated):
    def test_the_commands_the_agent_actually_types_are_pure(self):
        for cmd, tool in (("type backend\\agent.py", "read_file"),
                          ("cat backend/agent.py", "read_file"),
                          ("dir backend", "list_files"),
                          ("ls src", "list_files"),
                          ("findstr TODO backend", "grep_files"),
                          ("grep -r TODO backend", "grep_files"),
                          ("echo note > NOTES.txt", "write_file"),
                          ("dir *.py", "glob_files")):
            hit = guidance.overlap(cmd)
            self.assertTrue(hit, cmd)
            self.assertTrue(hit["pure"], f"{cmd} should be refused before it runs")
            self.assertEqual(hit["tool"], tool, cmd)

    def test_a_chained_command_is_never_refused(self):
        # The trap named in the deleted plan: refusing half a chain loses the half that
        # mattered. `dir` here is an overlap; `python build.py` is the job.
        hit = guidance.overlap("dir && python build.py")
        self.assertTrue(hit)
        self.assertFalse(hit["pure"])
        self.assertEqual(hit["verbs"], ["dir"])

    def test_a_command_with_no_overlap_is_none(self):
        for cmd in ('python -c "print(\'x\')"', "python -m unittest tests.test_x",
                    "npm run build", "git status", "pytest -q"):
            self.assertIsNone(guidance.overlap(cmd), cmd)

    def test_a_pipe_is_the_shell_doing_something_no_tool_does(self):
        # Found by dogfooding: this very test run, filtered through findstr, was
        # advised to use grep_files. findstr here reads *stdin* — refusing it would
        # have destroyed the test run to save one spawn.
        for cmd in ("python -m unittest 2>&1 | findstr OK",
                    "cat interp.py | grep def",
                    "curl -sL https://example.com/x.tgz | tar xz",
                    "dir | findstr .py"):
            self.assertIsNone(guidance.overlap(cmd), cmd)

    def test_a_flag_the_tool_cannot_honour_is_not_an_overlap(self):
        # grep_files has no case-insensitive option and list_files returns no sizes,
        # so pointing at them for these commands is wrong advice, and wrong advice
        # on every shell call is worse than silence.
        for cmd in ("grep -i TODO backend", "ls -la", "dir /s /b", "cat -A x.py",
                    "findstr /i todo backend", "rg TODO"):
            hit = guidance.overlap(cmd)
            self.assertTrue(hit is None or not hit["pure"], cmd)
            if hit:
                self.assertEqual(hit["verbs"], ["rg"], cmd)   # rg is advise-only, never refused

    def test_a_redirect_on_a_read_verb_is_a_copy_not_a_read(self):
        for cmd in ("cat a.txt > b.txt", "dir > listing.txt", "cat x 2>&1"):
            self.assertIsNone(guidance.overlap(cmd), cmd)

    def test_a_heredoc_is_not_refused(self):
        for cmd in ("cat > file << 'EOF'\nbody\nEOF", "echo hi > f.txt"):
            hit = guidance.overlap(cmd)
            if "<<" in cmd:
                self.assertIsNone(hit, cmd)

    def test_sed_is_advisory_only_and_only_when_it_edits_one_match(self):
        self.assertIsNone(guidance.overlap("sed -n 1,80p interp.py"))   # a dump
        self.assertIsNone(guidance.overlap("sed -i s/a/b/g x.py"))      # replaces every match
        hit = guidance.overlap("sed -i s/a/b/ x.py")
        self.assertTrue(hit)
        self.assertFalse(hit["pure"], "edit_file refuses a snippet appearing more than once")

    def test_environment_expansion_is_not_a_tool_call(self):
        for cmd in ("cat %USERPROFILE%\\x.txt", "echo $HOME > f", "dir `ls`"):
            self.assertIsNone(guidance.overlap(cmd), cmd)

    def test_the_refusal_names_the_call_to_make(self):
        refusal = guidance.overlap_refusal(guidance.overlap("type backend\\agent.py"))
        self.assertTrue(refusal.startswith("ERROR:"))
        self.assertIn("read_file(path='backend\\agent.py')", refusal)
        self.assertIn("Shell overlap", refusal, "a refusal must say how to turn it off")

    def test_a_note_is_one_line_and_names_the_tool(self):
        note = guidance.overlap_note(guidance.overlap("dir && python build.py"))
        self.assertTrue(note.startswith("\n[overlap] "), note)
        self.assertIn("list_files", note)
        self.assertLessEqual(note.count("\n"), 1, "a note that grows is a new standing prompt")


class TestRunShellWiring(Isolated):
    """The table only matters if it changes what `run_shell` returns."""

    def setUp(self):
        super().setUp()
        self._orig_run = sandbox.run
        self.ran = []

        def fake_run(command, project=None, timeout=None, session=None, **kw):
            # sandbox.run hands back a plain dict — `changed` is a report with a
            # `count`, and the backend notes are a list, not one string.
            self.ran.append(command)
            return {"ok": True, "backend": "test", "code": 0,
                    "stdout": "RAN " + command, "stderr": "",
                    "changed": {"count": 0}, "notes": []}
        sandbox.run = fake_run
        self.addCleanup(lambda: setattr(sandbox, "run", self._orig_run))

    def _shell(self, cmd, mode=None):
        """Run the tool the way the agent loop does, and give back its result text.

        `call_tool` is a *generator* that writes the result into the `out` dict it
        was handed: calling it and returning the call returns progress events, not
        the string under test, and the drain is part of invoking it at all.
        """
        if mode is not None:
            os.environ["TACIT_SHELL_OVERLAP"] = mode
        self.addCleanup(os.environ.pop, "TACIT_SHELL_OVERLAP", None)
        out: dict = {}
        list(agent.call_tool("run_shell", {"command": cmd},
                             {"session": "s1", "project": None}, out))
        return out.get("result") or ""

    def test_block_refuses_before_the_command_ever_runs(self):
        out = self._shell("dir backend", "block")
        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertIn("list_files(path='backend')", out)
        self.assertEqual(self.ran, [], "a refused command must not be spawned")

    def test_advise_runs_it_and_appends_the_line(self):
        out = self._shell("dir backend", "advise")
        self.assertEqual(self.ran, ["dir backend"])
        self.assertIn("RAN dir backend", out)
        self.assertIn("[overlap]", out)

    def test_block_lets_a_real_command_run_and_notes_it(self):
        out = self._shell("dir && python build.py", "block")
        self.assertEqual(self.ran, ["dir && python build.py"], "half a chain is not refused")
        self.assertIn("[overlap]", out)

    def test_off_is_silent(self):
        out = self._shell("dir backend", "off")
        self.assertEqual(self.ran, ["dir backend"])
        self.assertNotIn("[overlap]", out)
        self.assertNotIn("ERROR:", out)

    def test_the_default_is_block(self):
        os.environ.pop("TACIT_SHELL_OVERLAP", None)
        self.addCleanup(os.environ.pop, "TACIT_SHELL_OVERLAP", None)
        self.assertEqual(config.shell_overlap(), "block")
        out = self._shell("type x.py")
        self.assertTrue(out.startswith("ERROR:"), out)

    def test_a_clipped_dump_still_gets_the_precise_hint_and_not_two_lines(self):
        # _dump_hint says which file and what the ceiling was. A generic overlap line
        # beside it would be the same sentence twice.
        long = "x" * (config.TOOL_OUTPUT_LIMIT + 500)
        orig = sandbox.run

        def big(command, project=None, timeout=None, session=None, **kw):
            return {"ok": True, "backend": "test", "code": 0, "stdout": long,
                    "stderr": "", "changed": {"count": 0}, "notes": []}
        sandbox.run = big
        try:
            out = self._shell("cat big.txt", "advise")
        finally:
            sandbox.run = orig
        self.assertEqual(out.count("[overlap]"), 0, out[-300:])
        self.assertIn("read_file", out)
        self.assertIn("big.txt", out)

    def test_the_cost_is_on_the_record(self):
        # run_shell skips the ordinary tool ledger row (the sandbox writes a richer
        # one), so without this the injected characters belong to no record.
        seen = []
        orig = audit.record
        audit.record = lambda kind, **kw: seen.append((kind, kw))
        try:
            self._shell("dir && python build.py", "advise")
        finally:
            audit.record = orig
        rows = [kw for kind, kw in seen if kind == "shell_overlap"]
        self.assertEqual(len(rows), 1, seen)
        self.assertGreater(rows[0]["chars"], 0)
        self.assertEqual(rows[0]["tools"], "list_files")

    def test_a_refusal_is_recorded_too(self):
        seen = []
        orig = audit.record
        audit.record = lambda kind, **kw: seen.append((kind, kw))
        try:
            self._shell("dir backend", "block")
        finally:
            audit.record = orig
        rows = [kw for kind, kw in seen if kind == "shell_overlap"]
        self.assertEqual(len(rows), 1, seen)
        self.assertEqual(rows[0]["status"], "blocked")


class TestDumpHintAbsorbed(Isolated):
    """One vocabulary, two consumers: the hint's verbs come from the table."""

    def test_the_table_supplies_the_dump_verbs(self):
        for verb in ("cat", "type"):
            self.assertEqual(guidance.SHELL_OVERLAP[verb][0], "read_file", verb)
            self.assertIn(verb, agent._DUMPERS, verb)

    def test_type_is_now_covered_at_all(self):
        # `type` is the file printer on the platform Tacit runs on, and the old hint
        # list did not contain it: a clipped `type` read as a short file.
        self.assertEqual(agent._dumped_file("type backend\\agent.py"), "backend\\agent.py")
        cut = agent._clip("x" * 9000)
        hint = agent._dump_hint("type backend\\agent.py", cut)
        self.assertIn("read_file", hint)
        self.assertIn("agent.py", hint)


if __name__ == "__main__":
    unittest.main()

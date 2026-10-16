"""Where a session's tools work: the project if it has one, otherwise its own scratch folder.

This replaces the characterisation test that pinned the old behaviour, in which
`agent._root` fell back to `Path(config.USER_HOME)`. That meant an Agent session with no
project — and a session whose project had since been deleted or moved — read and wrote in
the user's home folder, with a seven-entry blocklist as the only thing in the way.

The rule now lives in exactly one function, `config.work_root`, because three files used
to re-implement the home fallback independently (`agent`, `sandbox`, `extras`) and any of
them could have drifted back:

* **A project that exists always wins.** A session with a workdir is never re-rooted into
  a scratch folder. `ProjectAlwaysWins` exists to defend exactly that.
* With no usable project the session gets `WORKSPACES_DIR/<session id>`, created on demand.
* A project that is *named but missing* counts as no project and the turn proceeds in the
  scratch folder without announcing it — failing a turn over a moved folder is worse than
  working somewhere harmless, and stale paths arrive out of stored sessions.
* The folder is deleted with the session, and the Git panel now answers the same question
  the tools do.

Two things this does **not** do, asserted so nobody reads more into the change than is
there:

* It is not a sandbox. `_safe` still accepts an absolute path and still has no containment
  check — "you are not walled in" is the standing prompt's own words. The *default* moved.
* A caller with neither a project nor a session id — `run_terminal` called directly, the
  legacy two-argument `run_turn`, the prompt-size estimates — lands in one shared
  `workspaces/default`. Still not the home directory, but not isolated either.

`gateways.py:159` also falls back to `USER_HOME`, and deliberately stays that way: that is
the working directory an operator configures for a gateway process, not a session's root.
"""
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from backend import agent, config, extras, sandbox, store, vcs
from backend.ai import prompts


class Isolated(unittest.TestCase):
    """Puts every directory the policy can reach inside one temporary tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tacit-root-test-")
        self.addCleanup(self._tmp.cleanup)
        self.base = pathlib.Path(self._tmp.name)
        self.home = self.base / "home"
        self.workspaces = self.base / "workspaces"
        self.project = self.base / "project"
        for d in (self.home, self.workspaces, self.project):
            d.mkdir()
        tacit = self.base / ".tacit"
        patches = [
            mock.patch.object(config, "USER_HOME", self.home),
            mock.patch.object(config, "WORKSPACES_DIR", self.workspaces),
            mock.patch.object(config, "HOME", tacit),
            mock.patch.object(config, "SESSIONS_DIR", tacit / "sessions"),
            mock.patch.object(config, "CHECKPOINT_DIR", tacit / "checkpoints"),
            mock.patch.object(config, "AUDIT_FILE", tacit / "audit.jsonl"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        (tacit / "sessions").mkdir(parents=True, exist_ok=True)
        self.scratch = (self.workspaces / "s-abc").resolve()


class ProjectAlwaysWins(Isolated):
    """The rule the change is built around."""

    def test_a_usable_project_beats_the_scratch_folder(self):
        self.assertEqual(agent._root(str(self.project), "s-abc"), self.project.resolve())

    def test_the_scratch_folder_is_not_created_when_a_project_is_given(self):
        """A session with a workdir must not leave a stray folder behind either."""
        agent._root(str(self.project), "s-abc")
        self.assertEqual(list(self.workspaces.iterdir()), [])

    def test_the_rule_holds_through_the_tools(self):
        agent.t_write_file("notes.txt", "hello", project=str(self.project), session="s-abc")
        self.assertTrue((self.project / "notes.txt").is_file())
        self.assertEqual(list(self.workspaces.iterdir()), [])


class ScratchFolderWhenThereIsNoProject(Isolated):

    def test_no_project_means_the_sessions_own_folder(self):
        root = agent._root(None, "s-abc")
        self.assertEqual(root, self.scratch)
        self.assertTrue(root.is_dir())

    def test_two_sessions_do_not_share_a_folder(self):
        self.assertNotEqual(agent._root("", "s-one"), agent._root("", "s-two"))
        agent.t_write_file("notes.txt", "one", session="s-one")
        agent.t_write_file("notes.txt", "two", session="s-two")
        self.assertEqual((self.workspaces / "s-one" / "notes.txt").read_text(), "one")
        self.assertEqual((self.workspaces / "s-two" / "notes.txt").read_text(), "two")

    def test_the_home_directory_is_never_the_answer(self):
        """The old fallback, pinned as gone. The change turns on this assertion."""
        for project, session in ((None, ""), ("", "s-abc"), ("   ", "s-abc"),
                                 (str(self.project / "gone"), "s-abc")):
            self.assertNotEqual(agent._root(project, session), self.home,
                                f"{project!r}/{session!r} still roots at home")

    def test_a_project_that_was_deleted_counts_as_no_project_and_says_nothing(self):
        """Proceed in the scratch folder rather than fail the turn — quietly, by choice."""
        gone = self.base / "moved-project"
        gone.mkdir()
        before = agent._root(str(gone), "s-abc")
        gone.rmdir()
        self.assertEqual(before, gone.resolve())
        self.assertEqual(agent._root(str(gone), "s-abc"), self.scratch)

    def test_a_file_is_not_a_directory(self):
        f = self.base / "notes.txt"
        f.write_text("x", encoding="utf-8")
        self.assertEqual(agent._root(str(f), "s-abc"), self.scratch)

    def test_no_session_id_lands_in_one_shared_folder(self):
        self.assertEqual(agent._root("", ""), (self.workspaces / "default").resolve())


class ASessionIdCannotClimbOut(Isolated):
    """Ids arrive from the wire, so the folder name is derived and never trusted."""

    def test_traversal_collapses_instead_of_escaping(self):
        for bad in ("../../etc", "..\\..\\Windows", "/absolute/path", "s-..-s",
                    "a" * 300):
            root = config.workspace_dir(bad, create=False)
            self.assertTrue(root.is_relative_to(self.workspaces.resolve()),
                            f"{bad!r} resolved outside the workspaces dir: {root}")

    def test_an_id_that_is_only_dots_falls_to_default(self):
        self.assertEqual(config._workspace_name(".."), "default")
        self.assertEqual(config._workspace_name(""), "default")

    def test_a_real_id_passes_through_untouched(self):
        self.assertEqual(config._workspace_name("s-ab12cd34"), "s-ab12cd34")


class EveryoneAgreesOnTheRoot(Isolated):
    """`agent`, `sandbox` and `extras` resolved this three different ways."""

    def test_the_shell_sandbox_uses_the_same_root(self):
        """An empty command reports the cwd it would have used, running nothing."""
        res = sandbox.run("", project=None, session="s-abc")
        self.assertEqual(pathlib.Path(res["cwd"]).resolve(), self.scratch)

    def test_the_shell_sandbox_still_prefers_a_real_project(self):
        res = sandbox.run("", project=str(self.project), session="s-abc")
        self.assertEqual(pathlib.Path(res["cwd"]).resolve(), self.project.resolve())

    def test_the_home_fallback_does_not_live_in_three_files_any_more(self):
        """A drift guard, not a behaviour test: the fallback must not come back in one."""
        for mod in (agent, sandbox, extras):
            src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
            hits = re.findall(r"^[^#]*config\.USER_HOME", src, re.MULTILINE)
            self.assertEqual(hits, [], f"{mod.__name__} still reaches for USER_HOME: {hits}")


class DenyListStillApplies(Isolated):
    """Narrowing the default root did not loosen any guard."""

    def test_ordinary_paths_under_the_scratch_root_are_reachable(self):
        p = agent._safe(pathlib.Path("Documents") / "scratch.txt", None, write=True,
                        session="s-abc")
        self.assertEqual(p.parent, self.scratch / "Documents")

    def test_the_deny_list_is_named_and_short(self):
        self.assertEqual(agent.DENY_NAMES, {".env", "models.json"})
        self.assertEqual(agent.DENY_PARTS,
                         {".git", "__pycache__", "node_modules", ".venv", "venv"})

    def test_a_denied_name_raises_even_in_a_scratch_folder(self):
        """Arguably over-cautious now the root is disposable, and left as it was:
        loosening a guard is a separate decision from moving a root."""
        with self.assertRaises(agent.ToolError):
            agent._safe(".env", None, session="s-abc")

    def test_an_absolute_path_is_still_not_blocked(self):
        """Not walled in, on purpose. The default moved; the reach did not."""
        elsewhere = self.base / "somewhere-else.txt"
        self.assertEqual(agent._safe(str(elsewhere), None, write=True, session="s-abc"),
                         elsewhere.resolve())


class RelativePathsAreRelativeToTheRightThing(Isolated):
    """Every tool has to carry the session id, or its output lies about the path."""

    def test_a_write_into_the_scratch_folder_reports_a_bare_name(self):
        self.assertIn("to notes.txt", agent.t_write_file("notes.txt", "hi", session="s-abc"))

    def test_list_read_glob_and_grep_all_resolve_the_same_root(self):
        agent.t_write_file("a/thing.txt", "needle", session="s-abc")
        self.assertIn("a/thing.txt", agent.t_list_files("a", None, "s-abc"))
        self.assertIn("needle", agent.t_read_file("a/thing.txt", session="s-abc"))
        self.assertIn("a/thing.txt", agent.t_glob_files("thing.txt", None, "s-abc"))
        self.assertIn("a/thing.txt", agent.t_grep_files("needle", ".", None, "s-abc"))

    def test_a_session_without_an_id_does_not_report_another_sessions_path(self):
        agent.t_write_file("notes.txt", "hi", session="s-abc")
        listing = agent.t_list_files(".", None, "")
        self.assertNotIn("notes.txt", listing)
        self.assertEqual(listing, "(empty)")


class ThePromptNamesTheRoot(Isolated):
    """The model must be told, or it says "I wrote the file" about a scratch folder."""

    def test_a_project_less_session_is_told_where_it_is(self):
        text = prompts.system_prompt(None, False, chat=False, session="s-abc")
        self.assertIn("scratch folder", text)
        self.assertIn("s-abc", text)

    def test_describing_the_root_does_not_create_it(self):
        prompts.system_prompt(None, False, chat=False, session="s-never-used")
        self.assertFalse((self.workspaces / "s-never-used").exists())

    def test_a_caller_with_no_session_gets_no_root_block(self):
        """The prompt-size estimates must measure the prompt that actually ships."""
        with_session = prompts.system_prompt(None, False, chat=False, session="s-abc")
        without = prompts.system_prompt(None, False, chat=False)
        self.assertNotIn("scratch folder", without)
        self.assertLess(len(without), len(with_session))

    def test_a_real_project_still_gets_the_project_block(self):
        text = prompts.system_prompt(str(self.project), False, chat=False, session="s-abc")
        self.assertIn("Working project", text)
        self.assertNotIn("scratch folder", text)


class ThePanelAndTheToolsAgree(Isolated):
    """`vcs.resolve_workdir` used to answer "no project" with someone else's folder."""

    def setUp(self):
        super().setUp()
        p1 = mock.patch.object(config, "project_roots", lambda: [str(self.project)])
        p2 = mock.patch.object(store, "list_sessions", lambda: [])
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_a_session_with_no_project_points_at_its_own_scratch_folder(self):
        rec = store.create(title="no project", mode="agent", sid="s-abc")
        self.assertEqual(pathlib.Path(vcs.resolve_workdir(None, rec["id"])).resolve(),
                         self.scratch)

    def test_a_session_with_a_project_still_points_at_the_project(self):
        rec = store.create(title="has one", mode="agent", project=str(self.project),
                           sid="s-abc")
        self.assertEqual(pathlib.Path(vcs.resolve_workdir(None, rec["id"])).resolve(),
                         self.project.resolve())

    def test_an_explicit_workdir_beats_everything(self):
        self.assertEqual(pathlib.Path(vcs.resolve_workdir(str(self.base), "s-abc")).resolve(),
                         self.base.resolve())

    def test_no_session_at_all_keeps_the_old_chain(self):
        """`hosting.py` asks which repo holds these credentials. A scratch folder is not
        a repo, and there is no session to root, so the configured root still answers."""
        self.assertEqual(pathlib.Path(vcs.resolve_workdir(None, None)).resolve(),
                         self.project.resolve())


class TheUndoTimelineReachesTheScratchFolder(Isolated):
    """The concrete defect: a project-less turn could not snapshot what it edited."""

    def test_an_auto_snapshot_with_no_project_succeeds(self):
        agent.t_write_file("notes.txt", "v1", session="s-abc")
        out = agent._auto_snapshot("", "write_file", session="s-abc")
        self.assertTrue(out.startswith("snapshot taken"), out)

    def test_the_snapshot_is_a_checkpoint_of_the_scratch_folder(self):
        agent.t_write_file("notes.txt", "v1", session="s-abc")
        agent._auto_snapshot("", "write_file", session="s-abc")
        rows = config.CHECKPOINT_DIR.glob("*")
        self.assertTrue(any((d / "notes.txt").is_file() for d in rows if d.is_dir()),
                        "the snapshot did not capture the scratch folder")

    def test_a_project_snapshot_is_unchanged(self):
        (self.project / "src.txt").write_text("v1", encoding="utf-8")
        out = agent._auto_snapshot(str(self.project), "edit_file", session="s-abc")
        self.assertTrue(out.startswith("snapshot taken"), out)
        self.assertFalse(self.workspaces.exists() and
                         (self.workspaces / "s-abc").exists(),
                         "a session with a project must not gain a scratch folder")


class TheWorkspaceGoesWithTheSession(Isolated):
    """Nobody else collects these folders, so deletion has to."""

    def test_deleting_a_session_deletes_its_scratch_folder(self):
        store.create(title="temp", mode="agent", sid="s-del")
        agent.t_write_file("notes.txt", "secret", session="s-del")
        self.assertTrue((self.workspaces / "s-del").is_dir())
        self.assertTrue(store.remove("s-del"))
        self.assertFalse((self.workspaces / "s-del").exists())

    def test_deleting_a_session_never_deletes_its_project(self):
        """The safety half: the folder the user chose is not ours to remove."""
        (self.project / "keep.txt").write_text("mine", encoding="utf-8")
        store.create(title="has one", mode="agent", project=str(self.project), sid="s-keep")
        store.remove("s-keep")
        self.assertTrue(self.project.is_dir())
        self.assertEqual((self.project / "keep.txt").read_text(), "mine")

    def test_an_empty_id_cannot_delete_the_shared_folder(self):
        (self.workspaces / "default").mkdir()
        self.assertFalse(config.remove_workspace(""))
        self.assertTrue((self.workspaces / "default").is_dir())

    def test_removing_a_session_that_never_had_one_is_not_a_failure(self):
        store.create(title="no folder", sid="s-none")
        self.assertTrue(store.remove("s-none"))


class NothingValidatesTheProjectOnTheWayIn(Isolated):
    """The scratch folder is applied at *use*, not stamped into the record."""

    def test_create_stores_an_empty_project_as_given(self):
        self.assertEqual(store.create(title="no project", mode="agent",
                                      project="").get("project"), "")

    def test_create_does_not_invent_a_project(self):
        self.assertEqual(store.create(title="nothing given", mode="agent").get("project"), "")

    def test_a_session_with_a_project_is_untouched_by_the_policy(self):
        rec = store.create(title="has one", mode="agent", project=str(self.project))
        self.assertEqual(rec["project"], str(self.project))


if __name__ == "__main__":
    unittest.main()

"""An approved plan is the session's file, not the project's.

PLAN item 4 (the plan-mode rewrite) turns the two copies inside out. Approving a
plan used to write the project's PLAN.md as the *live* copy — the one injected as
an instruction, the one Implement was told to keep updated — while the harness's
own record under ``~/.tacit/plans`` was a note nothing read. Now the session file
is the plan of record, nothing is written into the project, and what keeps the plan
alive across compaction is a ~100-token pointer rebuilt into the standing system
prefix every turn, not a copy of the text.

This file checks four things: that approval writes exactly one file in the right
place and nothing in the project; that the pointer exists for a session that has a
plan and says nothing for one that does not; that ``planFile`` survives the record
rewrites a socket performs; and that a project's own PLAN.md is now a document to
be found rather than an instruction to be obeyed — including when it still carries
the marker stamp the old writer used to promote it.
"""

import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION_RUNTIME = (ROOT / "backend" / "session_runtime.py").read_text(encoding="utf-8")
PLAN_SRC = (ROOT / "backend" / "plan.py").read_text(encoding="utf-8")

from backend import config, plan, project_context, store  # noqa: E402
from tests.helpers import state_paths  # noqa: E402


class Isolated(unittest.TestCase):
    """Points every storage path under the user's home at a temporary one."""

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
        self._proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._proj, True)
        self.root = Path(self._proj)
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")

    def _restore_config(self):
        config.HOME = self._home
        for key, value in self._orig.items():
            setattr(config, key, value)

    def _plan(self, sid="s1", text="## Steps\n\n1. do the thing\n"):
        p = plan.Plan(sid, "make the thing work", str(self.root), None)
        p.text = text
        return p


class TestOneFileWhere(Isolated):
    """Approval writes one file, into the plans folder, and touches nothing else."""

    def test_approving_writes_the_sessions_own_file(self):
        res = plan.approve(self._plan(), True)
        self.assertTrue(res["saved"], res)
        path = Path(res["plan_file"])
        self.assertEqual(path.parent.resolve(), Path(config.PLANS_DIR).resolve())
        self.assertTrue(path.is_file())

    def test_approving_writes_nothing_into_the_project(self):
        plan.approve(self._plan(), True)
        left = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(left, ["a.py"],
                         "the approval created a file in the project: %s" % left)

    def test_a_rejected_plan_writes_no_file_anywhere(self):
        p = self._plan()
        res = plan.approve(p, False)
        self.assertFalse(res["saved"])
        self.assertFalse(Path(config.PLANS_DIR).exists() and
                         list(Path(config.PLANS_DIR).glob("*.md")))

    def test_an_empty_plan_is_not_saved(self):
        res = plan.approve(self._plan(text="   "), True)
        self.assertFalse(res["saved"])
        self.assertIn("no plan", res["message"])

    def test_a_sid_cannot_escape_the_plans_folder(self):
        path = plan.plan_path("../../evil")
        self.assertNotIn("/", path.name)
        self.assertNotIn("..", str(path))
        self.assertEqual(path.parent.resolve(), Path(config.PLANS_DIR).resolve())


class TestTheBodyIsAnInstruction(Isolated):
    """What the file says, because the file is what the agent obeys."""

    def test_the_request_rides_with_the_plan(self):
        text = plan.body(self._plan())
        self.assertIn("## Request", text)
        self.assertIn("make the thing work", text)
        self.assertIn("## Plan", text)
        self.assertIn("do the thing", text)

    def test_divergence_is_reported_and_the_file_is_updated(self):
        text = plan.body(self._plan())
        self.assertIn("implement it as written", text)
        self.assertIn("Update this file", text)
        self.assertIn("rather than in the project", text)


class TestThePointer(Isolated):
    """The line that keeps the file reachable after compaction has eaten the rest."""

    def test_a_session_with_no_plan_gets_no_pointer(self):
        self.assertEqual(plan.pointer({}), "")
        self.assertEqual(plan.pointer(None), "")
        self.assertEqual(plan.pointer({"planState": {"saved": False}}), "")

    def test_planfile_wins_because_it_is_the_session_doing_the_work(self):
        out = plan.pointer({"planFile": r"C:\home\.tacit\plans\s1.md",
                           "planState": {"saved": True, "plan_file": "other.md"}})
        self.assertIn(r"C:\home\.tacit\plans\s1.md", out)
        self.assertNotIn("other.md", out)

    def test_a_sessions_own_approval_is_the_fallback(self):
        out = plan.pointer({"planState": {"saved": True, "plan_file": r"D:\p\s1.md"}})
        self.assertIn(r"D:\p\s1.md", out)
        self.assertIn("read tool", out)


class TestPlanFileSurvivesARewrite(Isolated):
    """A socket saves its own stale copy of the record constantly."""

    def test_planfile_is_preserved_when_a_stale_record_is_saved(self):
        rec = store.create("plan durability")
        store.patch(rec["id"], planFile=str(config.PLANS_DIR / f"{rec['id']}.md"))
        stale = store.get(rec["id"])
        del stale["planFile"]
        store.save(stale)
        again = store.get(rec["id"])
        self.assertTrue(again.get("planFile"),
                        "planFile dropped by a record rewrite — the pointer dies")


class TestTheProjectFileIsADocumentNow(Isolated):
    """PLAN.md left the instruction list, marker promotion with it."""

    def test_plan_md_is_listed_as_docs_not_instructions(self):
        (self.root / "PLAN.md").write_text(
            "<!-- tacit:approved-plan -->\n\n# Old harness plan\n", encoding="utf-8")
        found = {f["name"]: f["kind"] for f in project_context.discover(str(self.root))}
        self.assertEqual(found.get("PLAN.md"), "docs",
                         "the marker still promotes a project file to instruction")

    def test_a_plan_shaped_name_the_harness_did_not_write_is_invisible(self):
        (self.root / "PLAN-october.md").write_text("roadmap\n", encoding="utf-8")
        names = [f["name"] for f in project_context.discover(str(self.root))]
        self.assertNotIn("PLAN-october.md", names)

    def test_the_harness_keeps_no_route_into_a_project(self):
        for gone in ("write_context", "context_file", "PLAN_MARK", "PLAN_GLOB"):
            self.assertNotIn(gone, PLAN_SRC, f"plan.py still mentions {gone}")
        self.assertNotIn("PLAN.md", project_context.NAMES)


class TestThePointerIsInTheStandingPrefix(Isolated):
    """Source-shaped, because reaching the prefix means driving a socket.

    The pointer's whole value is that it is rebuilt every turn, so it must be built
    where the project's own instructions are built, and it must not be built for
    chat mode, which has no read tool and would only pay for the line.
    """

    def test_it_is_appended_with_the_instructions_not_with_the_history(self):
        at = SESSION_RUNTIME.index('held_back=int(instructions.get("held_back") or 0))')
        pointer = SESSION_RUNTIME.index("plan_mod.pointer(disk)")
        history = SESSION_RUNTIME.index("messages.extend(_history(rec))")
        self.assertTrue(at < pointer < history,
                        "the pointer is not in the rebuilt prefix (it can be summarised)")

    def test_it_is_read_from_disk_not_the_sockets_copy(self):
        self.assertIn("disk = store.get(sid) or {}", SESSION_RUNTIME)
        self.assertLess(SESSION_RUNTIME.index("disk = store.get(sid) or {}"),
                        SESSION_RUNTIME.index("plan_mod.pointer(disk)"))

    def test_chat_mode_is_skipped(self):
        guard = SESSION_RUNTIME.rindex("if not chat:", 0,
                                      SESSION_RUNTIME.index("plan_mod.pointer(disk)"))
        self.assertLess(guard, SESSION_RUNTIME.index("plan_mod.pointer(disk)"))


class TestImplementHandsThePathOver(Isolated):
    def test_the_first_message_names_the_file_it_just_wrote(self):
        p = self._plan()
        plan.approve(p, True)
        prompt = plan.implement_prompt(p)
        self.assertIn(p.plan_file, prompt)
        self.assertIn("Read it with the read tool", prompt)

    def test_no_path_no_promise(self):
        p = self._plan()
        self.assertNotIn("Read it with the read tool", plan.implement_prompt(p))


if __name__ == "__main__":
    unittest.main()

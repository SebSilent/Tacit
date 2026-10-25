"""The frozen archive: what a compaction folds away is written verbatim, before
the fold, and stays readable until the session itself is deleted.

The bar this file was written to, stated here so it stands on its own:

    A compaction is destructive by design, so the archive is the promise that
    the destruction is not the last copy. Three claims have to hold at once:

    1.  The archive is written BEFORE the fold touches the rows. The prune
        clears tool output in place, so an archive taken after it would
        preserve the head it kept and lose the body it dropped — the exact
        part the archive exists to keep. The tests below prove the ordering
        with a counterfactual, not an assertion that happens to pass.
    2.  A fold that cannot write its archive still folds. The compaction is
        the urgent job; the archive is the record of it. An archive write
        that could abort a fold would trade the cure for the record of the
        cure.
    3.  The reader is a capability the person opts into. Off by default, the
        schema is not offered and the call is refused at the dispatch point —
        hiding the schema alone is not a guard, because a model that
        remembers the name can still emit the call.

Deletion is covered where it belongs (`test_deleted_session_stays_deleted.py`
holds the store's contract); the one archive-specific claim — that removing a
session removes its archive, so no transcript outlives its session — is
repeated here because it is the archive's own docstring promise.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import agent, archive, config, store  # noqa: E402
from backend.routers import api  # noqa: E402
from tests.helpers import state_paths  # noqa: E402

ARCHIVE_TOOL = agent.ARCHIVE_TOOL


class Isolated(unittest.TestCase):
    """Never read the operator's own prefs file or sessions directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self.home = home
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

    # -- helpers ------------------------------------------------------------

    def rows_with_results(self, n, chars=3000):
        """Stored-shape rows whose tool results are big enough that the prune
        bites (COMPACT_PRUNE_MIN_CHARS is 1200), each carrying a marker at the
        END of its result. The prune keeps the head, so a marker at the end is
        the canary: present in the archive means verbatim, absent means the
        archive was taken after the prune."""
        out = []
        for i in range(n):
            body = "x" * chars + f" TAILMARK-{i}"
            out.append({"role": "assistant", "content": f"step {i}",
                        "tools": [{"name": "run_shell", "args": {"command": f"cmd {i}"},
                                   "result": body}]})
        return out

    def archived_msgs(self, sid):
        """The archived rows as they sit on disk, not through the renderer."""
        path = config.SESSIONS_DIR / f"{sid}.archive.jsonl"
        if not path.exists():
            return None
        return [json.loads(line)["msg"] for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TheWriter(Isolated):
    """append_rows: the only writer, and the guarantees the folds lean on."""

    def test_rows_land_verbatim_with_why_and_ts(self):
        rows = self.rows_with_results(2)
        n = archive.append_rows("w1", rows, why="fold")
        self.assertEqual(n, 2)
        raw = (config.SESSIONS_DIR / "w1.archive.jsonl").read_text(encoding="utf-8")
        first = json.loads(raw.splitlines()[0])
        self.assertEqual(first["why"], "fold")
        self.assertIsInstance(first["ts"], float)
        self.assertEqual(first["msg"]["tools"][0]["result"],
                         rows[0]["tools"][0]["result"])

    def test_two_appends_accumulate_never_overwrite(self):
        archive.append_rows("w2", self.rows_with_results(2), why="fold")
        archive.append_rows("w2", self.rows_with_results(1), why="mid-turn fold")
        self.assertEqual(len(self.archived_msgs("w2")), 3)

    def test_empty_calls_write_nothing(self):
        self.assertEqual(archive.append_rows("", self.rows_with_results(2)), 0)
        self.assertEqual(archive.append_rows("w3", []), 0)
        self.assertFalse((config.SESSIONS_DIR / "w3.archive.jsonl").exists())

    def test_non_dict_rows_are_skipped_not_fatal(self):
        n = archive.append_rows("w4", ["junk", self.rows_with_results(1)[0], 7])
        self.assertEqual(n, 1)

    def test_an_unwritable_path_returns_zero_and_never_raises(self):
        # SESSIONS_DIR pointed at a *file*: every open under it fails. This is
        # the portable stand-in for a full disk or a locked directory.
        blocker = self.home / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        config.SESSIONS_DIR = blocker
        self.assertEqual(archive.append_rows("w5", self.rows_with_results(2)), 0)


class TheFoldArchivesFirst(Isolated):
    """compact_history: the ordering claim, with the counterfactual that makes
    it mean something."""

    def make_rec(self, sid, n=8):
        msgs = [{"role": "user", "content": "go"}] + self.rows_with_results(n)
        return {"id": sid, "messages": msgs}

    def test_the_folded_region_is_archived_verbatim_before_the_prune(self):
        rec = self.make_rec("f1")
        res = agent.compact_history(rec, force=True)
        self.assertIsNotNone(res, "the fold did not happen, so this test proves nothing")
        self.assertEqual(res["archived"], len(self.archived_msgs("f1")))
        for i, m in enumerate(self.archived_msgs("f1")):
            self.assertIn(f"TAILMARK-{i}", m["tools"][0]["result"],
                          "the archived result lost its body: archived after the prune")

    def test_the_counterfactual_pruning_first_would_lose_the_body(self):
        """A plant, so the test above cannot pass by accident.

        This is the archive as it would have been written had the call sat
        after `_prune_old_output`: same rows, prune applied, then archived. If
        this ever stops losing the marker, the ordering test above is proving
        nothing and needs rewriting.
        """
        rows = self.rows_with_results(3)
        pruned = json.loads(json.dumps(rows))          # deep copy: prune in place
        agent._prune_old_output(pruned)
        archive.append_rows("f2-plant", pruned, why="fold")
        for i, m in enumerate(self.archived_msgs("f2-plant")):
            self.assertNotIn(f"TAILMARK-{i}", m["tools"][0]["result"],
                             "the plant no longer reproduces the loss; update this test")

    def test_a_fold_whose_archive_cannot_be_written_still_folds(self):
        rec = self.make_rec("f3")
        config.SESSIONS_DIR = self.home / "not-a-dir"   # a file, from TheWriter
        (self.home / "not-a-dir").write_text("x", encoding="utf-8")
        res = agent.compact_history(rec, force=True)
        self.assertIsNotNone(res, "the fold must go on when the archive cannot be written")
        self.assertEqual(res["archived"], 0)

    def test_no_fold_no_archive(self):
        # Early returns must not create an empty sidecar: an archive with zero
        # rows is a file that lies about what happened.
        rec = {"id": "f4", "messages": [{"role": "user", "content": "go"}]}
        self.assertIsNone(agent.compact_history(rec, force=True))
        self.assertFalse((config.SESSIONS_DIR / "f4.archive.jsonl").exists())


class TheMidTurnFoldArchives(Isolated):
    """apply_stored_summary: the path that used to lose everything at the
    process boundary, so these rows are the ones the archive exists for."""

    def test_folded_rows_are_archived_unpruned(self):
        rows = self.rows_with_results(6)
        rec = {"id": "m1", "messages": [{"role": "user", "content": "go"}] + rows}
        saved = agent.apply_stored_summary(rec, "NOTE", kept_tokens=100)
        self.assertGreater(saved, 0, "nothing was folded, so this test proves nothing")
        archived = self.archived_msgs("m1")
        self.assertTrue(archived)
        for i, m in enumerate(archived):
            self.assertIn(f"TAILMARK-{i}", m["tools"][0]["result"],
                          "the mid-turn archive is not verbatim")
        # The label is part of the record: it is what tells a reader that these
        # rows left mid-turn, not at the end-of-turn fold, which writes "fold".
        # Asserted on the raw file because archived_msgs() returns only the msg.
        raw = (config.SESSIONS_DIR / "m1.archive.jsonl").read_text(encoding="utf-8")
        for line in raw.splitlines():
            if line.strip():
                self.assertEqual(json.loads(line)["why"], "mid-turn fold",
                                 "the mid-turn fold lost its label")
        # And the record really was folded: the summary sits where the rows were.
        self.assertEqual(rec["messages"][1]["role"], "summary")

    def test_when_the_tail_covers_everything_nothing_is_archived(self):
        # i <= 0 returns before the append: a fold that folds nothing must not
        # write an archive that says it did.
        rows = self.rows_with_results(3)
        rec = {"id": "m2", "messages": [{"role": "user", "content": "go"}] + rows}
        self.assertEqual(agent.apply_stored_summary(rec, "NOTE", kept_tokens=10**9), 0)
        self.assertFalse((config.SESSIONS_DIR / "m2.archive.jsonl").exists())


class TheReader(Isolated):
    """read(): the tool's whole surface, and the errors that teach the model
    the right next call instead of leaving it guessing."""

    def seed(self, sid, n=4):
        archive.append_rows(sid, self.rows_with_results(n), why="fold")

    def test_info_on_an_empty_archive_says_so(self):
        self.assertIn("archive: empty", archive.read("r0"))

    def test_info_reports_the_size(self):
        self.seed("r1", 3)
        out = archive.read("r1")
        self.assertIn("3 rows", out)
        self.assertIn("action=search", out)

    def test_search_finds_and_misses(self):
        self.seed("r2", 3)
        out = archive.read("r2", action="search", query="cmd 1")
        self.assertIn("cmd 1", out)
        self.assertNotIn("cmd 2", out)
        self.assertIn("no row matches", archive.read("r2", action="search", query="zzz"))

    def test_search_without_a_query_says_what_to_do_instead(self):
        self.seed("r2b", 1)
        out = archive.read("r2b", action="search")
        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertIn("action=info", out)

    def test_read_pages_and_offers_the_continuation(self):
        # The render is bounded (600 chars of a result), so the marker at the
        # end of a 3000-char result cannot appear here — verbatim-ness is
        # asserted on the file in TheFoldArchivesFirst, not through the renderer.
        self.seed("r3", 5)
        out = archive.read("r3", action="read", offset=0, limit=2)
        self.assertIn("rows 0..1 of 5", out)
        self.assertIn("offset=2", out)
        self.assertIn("cmd 0", out)
        self.assertNotIn("cmd 2", out)

    def test_an_offset_past_the_end_is_a_message_not_a_crash(self):
        self.seed("r4", 2)
        self.assertIn("past the end", archive.read("r4", action="read", offset=99))

    def test_the_limit_is_clamped_both_ways(self):
        # 0 is falsy, so it means "the default", not "one row"; a negative
        # limit is the case the floor exists for. The cap needs more rows than
        # the cap to be visible.
        self.seed("r5", 3)
        self.assertIn("rows 0..0 of 3", archive.read("r5", action="read", limit=-5))
        self.assertIn("rows 0..2 of 3", archive.read("r5", action="read", limit=0))
        self.seed("r5b", 60)
        out = archive.read("r5b", action="read", limit=999)
        self.assertIn("rows 0..49 of 60", out)
        self.assertIn("offset=50", out)

    def test_an_unknown_action_is_an_error(self):
        self.seed("r6")
        self.assertIn("unknown action", archive.read("r6", action="delete"))

    def test_a_torn_line_is_skipped_not_fatal(self):
        # The writer holds a lock so its own lines cannot interleave, but a
        # half-written line from a killed process must not blind the reader.
        # Asserted through the reader, not the raw file: the claim is that the
        # tool still reports the two good rows.
        self.seed("r7", 2)
        path = config.SESSIONS_DIR / "r7.archive.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write('{"ts": 1, "why": "fold", "msg": {"rol')
        self.assertIn("2 rows", archive.read("r7", action="info"))


class TheSwitch(Isolated):
    """The switch is what makes the reader off, and off means refused at the call.

    The default is on — a fold the model cannot read back is the loss the person
    feels, and the reader is what makes a fold a reorganisation instead of a
    deletion — so these tests set the pref off explicitly: they test the off
    state, not the default. TheDefault below pins the default itself.
    """

    def setUp(self):
        super().setUp()
        config.save_prefs({"archiveTool": False})

    def test_the_schema_is_hidden_while_the_switch_is_off(self):
        self.assertNotIn(ARCHIVE_TOOL, {t["function"]["name"] for t in agent.tools_for()})
        config.save_prefs({"archiveTool": True})
        self.assertIn(ARCHIVE_TOOL, {t["function"]["name"] for t in agent.tools_for()})

    def test_a_call_while_off_is_refused_at_dispatch(self):
        # Hiding the schema is not the guard: a model that remembers the name
        # can still emit the call (the same rule the read-only guard tests).
        out = {}
        for _ in agent.call_tool(ARCHIVE_TOOL, {"action": "info"},
                                 {"session": "s1", "project": None}, out):
            pass
        self.assertTrue(out["result"].startswith("ERROR:"), out["result"])
        self.assertIn("archive", out["result"])

    def test_a_call_while_on_reads_the_real_archive(self):
        archive.append_rows("s2", self.rows_with_results(2), why="fold")
        config.save_prefs({"archiveTool": True})
        out = {}
        for _ in agent.call_tool(ARCHIVE_TOOL, {"action": "search", "query": "cmd 1"},
                                 {"session": "s2", "project": None}, out):
            pass
        self.assertIn("cmd 1", out["result"], out["result"])

    def test_removing_a_session_removes_its_archive(self):
        """The docstring promise: an archive that outlived its session would be
        a resurrection path with a transcript attached."""
        rec = store.create(title="t", model="m", mode="agent", sid="s3")
        store.save(rec)
        archive.append_rows("s3", self.rows_with_results(2), why="fold")
        self.assertTrue((config.SESSIONS_DIR / "s3.archive.jsonl").exists())
        self.assertTrue(store.remove("s3"))
        self.assertFalse((config.SESSIONS_DIR / "s3.archive.jsonl").exists())


class TheDefault(Isolated):
    """The reader ships on: the archive is written for the model to read back.

    It shipped off because an agent that can page through its own past was
    treated as a capability the person opts into; what that default bought was
    the opposite of the archive's purpose — folds still rewrote the transcript
    and the model had no way back. The switch above still turns it off.
    """

    def test_the_schema_is_offered_with_no_pref_set(self):
        self.assertIsNone(config.prefs().get("archiveTool"))
        self.assertIn(ARCHIVE_TOOL, {t["function"]["name"] for t in agent.tools_for()})

    def test_a_call_with_no_pref_set_reads_the_archive(self):
        archive.append_rows("s4", self.rows_with_results(2), why="fold")
        out = {}
        for _ in agent.call_tool(ARCHIVE_TOOL, {"action": "search", "query": "cmd 1"},
                                 {"session": "s4", "project": None}, out):
            pass
        self.assertIn("cmd 1", out["result"], out["result"])


class TheSettingsSurface(Isolated):
    """Settings > Tools: the endpoints the archive toggle reads/writes.

    The switch is its own pref, not a disabledTools entry, so the wire shape is
    part of the feature: a toggle bound to the wrong list would report success
    and change nothing.
    """

    def _body(self, res):
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

    def _archive_row(self):
        """The switch's state rides on its tool row, not a top-level key: the
        tab renders one toggle per tool, and this one reads its own pref."""
        d = self._get()
        return d, next(r for r in d["tools"] if r["name"] == ARCHIVE_TOOL)

    def test_the_list_reports_the_switch_and_its_pin(self):
        d, row = self._archive_row()
        # The row shows the state the switch owns, and the default is on: the
        # archive is written for the model to read back, so the reader ships
        # offered. The pin is still false — no env var has spoken.
        self.assertTrue(row["enabled"])
        self.assertFalse(d["archiveToolPinned"])

    def test_the_toggle_moves_its_own_pref(self):
        r = self._toggle({"name": ARCHIVE_TOOL, "enabled": True})
        self.assertTrue(r["ok"], r)
        self.assertTrue(config.prefs().get("archiveTool"))
        _, row = self._archive_row()
        self.assertTrue(row["enabled"])
        r = self._toggle({"name": ARCHIVE_TOOL, "enabled": False})
        self.assertTrue(r["ok"], r)
        _, row = self._archive_row()
        self.assertFalse(row["enabled"])

    def test_an_environment_override_says_so_instead_of_lying(self):
        os.environ["TACIT_ARCHIVE_TOOL"] = "1"
        self.addCleanup(os.environ.pop, "TACIT_ARCHIVE_TOOL", None)
        self.assertTrue(self._get()["archiveToolPinned"])
        r = self._toggle({"name": ARCHIVE_TOOL, "enabled": False})
        self.assertFalse(r["ok"])
        self.assertIn("TACIT_ARCHIVE_TOOL", r["error"])
        self.assertIsNone(config.prefs().get("archiveTool"),
                          "a save that cannot take effect must not be written")

    def test_the_toggle_does_not_swallow_the_old_contract(self):
        r = self._toggle({"name": "run_shell", "enabled": False})
        self.assertTrue(r["ok"], r)
        self.assertIn("run_shell", self._get()["disabled"])


if __name__ == "__main__":
    unittest.main()
"""Item 3 — a deleted session stays deleted.

The bar this test was written to, stated here so the file stands on its own:

    A regression test: create -> delete -> re-open the store from disk in a fresh
    process -> assert gone. The fresh-process step is the point; a test that deletes
    and re-reads the same in-memory list passes today and proves nothing.

The bug was never `remove()`. `remove()` unlinks the record and rebuilds the index,
so in this process it looks correct. The resurrection came from a *second writer*
holding a `rec` dict — a socket keeps one for the life of the connection — and
calling `store.save()` minutes after the delete, which re-created the file and
re-appended the index row. That is why it read as "sometimes": it only happened
while something was still live.

So the tests here are ordered by how much they prove:

1. `TheFreshProcess` — the required test. A new interpreter, pointed at the same
   directory by `TACIT_HOME`, reads the disk and says whether the session exists.
   It also reads *before* deleting, which is what keeps the passing case honest:
   a reader pointed at the wrong store would report "gone" for every session ever
   written, and a test that only ever asserts absence would cheerfully agree.
2. `TheSecondWriter` — the exact interleaving that caused it, in process, with a
   counterfactual showing the pre-fix code really does resurrect the session.
   Without that pair, a green suite could mean "the bug is fixed" or "nothing in
   this file ever wrote after a delete".
3. `TheInterleave` — the ghost signature the plan names (a row in the index whose
   file is gone opens as a session with an empty message list), checked after real
   concurrent save/remove traffic.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, store  # noqa: E402
from tests.helpers import state_paths  # noqa: E402

ROOT = str(Path(__file__).resolve().parent.parent)

# Runs in a child interpreter. argv[1] is the session id; argv[2] the id the test
# expects to still exist, so one process answers both the before and the after
# question. Prints the three views the UI could reach the session through: the
# raw index, the record itself, and registry() — the payload /api/sessions serves.
_READER = r"""
import json, sys
sys.path.insert(0, %r)
from backend import store
sid, other = sys.argv[1], sys.argv[2]
print(json.dumps({
    "index": [r.get("id") for r in store.index()],
    "record": store.get(sid) is not None,
    "registry": [s.get("id") for s in store.registry()["sessions"]],
    "other": store.get(other) is not None,
}))
"""


class Isolated(unittest.TestCase):
    """Points every storage path under the user's home at a temporary one."""

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

    def make(self, sid, msgs=2, title="session"):
        """Create a session the way the app does: record on disk, row in index."""
        rec = store.create(title=title, model="m", mode="agent", sid=sid)
        for i in range(msgs):
            store.append(rec, "user" if i % 2 == 0 else "assistant", f"turn {i}")
        return store.save(rec)

    def read_as_new_process(self, sid, other=""):
        """Ask a fresh interpreter what is on disk. Never re-use this process's view."""
        env = dict(os.environ)
        env["TACIT_HOME"] = str(self.home)   # config.py:29 reads this at import
        proc = subprocess.run(
            [sys.executable, "-c", _READER % ROOT, sid, other],
            capture_output=True, text=True, timeout=120, env=env, cwd=ROOT,
            stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0,
                         f"reader failed: {proc.stderr[-2000:]}")
        return json.loads(proc.stdout.strip().splitlines()[-1])


class TheFreshProcess(Isolated):
    """The test the plan asks for, with the control that makes it mean something."""

    def test_a_deleted_session_is_gone_to_a_process_that_never_saw_it(self):
        self.make("gone-1")
        self.make("kept-1")

        before = self.read_as_new_process("gone-1", "kept-1")
        # The control. If the child were reading some other directory — a stale
        # TACIT_HOME, a path that drifted — this is where the test says so, rather
        # than passing later on an absence that has nothing to do with the delete.
        self.assertIn("gone-1", before["index"],
                      "the fresh process is not reading the store this test writes")
        self.assertTrue(before["record"])
        self.assertIn("gone-1", before["registry"])

        self.assertTrue(store.remove("gone-1"))

        after = self.read_as_new_process("gone-1", "kept-1")
        self.assertNotIn("gone-1", after["index"], "row left in the index: a ghost")
        self.assertFalse(after["record"], "record file still on disk after delete")
        self.assertNotIn("gone-1", after["registry"],
                         "still served to the browser by registry()")
        # And the delete must not be a wrecking ball: the neighbour survives with
        # its transcript, which is what separates `remove()` from `clear()`.
        self.assertIn("kept-1", after["registry"])
        self.assertTrue(after["other"])

    def test_a_session_deleted_while_a_socket_held_it_stays_deleted(self):
        """The bug as filed: delete, then a live connection saves its stale copy.

        `routers/chat.py` keeps one `rec` for the life of the connection and saves
        it from set_model / truncate / compact. Before the guard at store.py:113
        each of those writes minutes after the delete put both halves back, and a
        hard refresh showed the session again.
        """
        rec = self.make("stale-1")
        self.assertTrue(store.remove("stale-1"))

        store.save(rec)                      # the second writer, still holding it
        store.patch("stale-1", title="hail mary")   # and a patch by id, for good measure

        seen = self.read_as_new_process("stale-1")
        self.assertNotIn("stale-1", seen["index"])
        self.assertFalse(seen["record"])
        self.assertNotIn("stale-1", seen["registry"])


class TheSecondWriter(Isolated):
    """In-process, with the counterfactual: the guard is what stops it."""

    def test_save_refuses_when_both_halves_are_gone(self):
        rec = self.make("refuse-1")
        store.remove("refuse-1")
        out = store.save(rec)
        self.assertIs(out["id"], "refuse-1", "save() still returns the caller's record")
        self.assertIsNone(store.get("refuse-1"))
        self.assertNotIn("refuse-1", [r.get("id") for r in store.index()])

    def test_the_pre_fix_code_really_did_resurrect_it(self):
        """A plant, so the test above cannot pass by accident.

        This is `save()` as it behaved before the fix: write the record, then
        read-edit-write the index, which appends a row for an id nothing removed
        from *this* copy of the list. If this assertion ever stops resurrecting the
        session, the other test in this class is proving nothing and needs rewriting.
        """
        rec = self.make("plant-1")
        store.remove("plant-1")

        config.write_json(store._path("plant-1"), rec)     # the old save(), guard removed
        rows = store.index()
        rows.append({k: rec.get(k) for k in ("id", "title", "model", "mode",
                                             "thinking", "project", "created", "updated")})
        store._write_index(rows)

        self.assertIsNotNone(store.get("plant-1"),
                             "the plant no longer reproduces the bug — update this test")
        self.assertIn("plant-1", [r.get("id") for r in store.index()])

    def test_a_half_written_record_is_still_healed(self):
        """The guard must be narrow: file gone + row present is a repair, not a delete."""
        rec = self.make("heal-1")
        store._path("heal-1").unlink()          # only one half is missing
        store.save(rec)
        self.assertIsNotNone(store.get("heal-1"),
                             "save() refused a record the index still lists")

    def test_a_lost_index_row_is_still_restored(self):
        """...and row gone + file present likewise, or a lost index nukes real sessions."""
        rec = self.make("row-1")
        store._write_index([r for r in store.index() if r.get("id") != "row-1"])
        store.save(rec)
        self.assertIn("row-1", [r.get("id") for r in store.index()])


class TheBrowserAsksItBack(Isolated):
    """Hypothesis 3: the delete holds on disk and is then undone by `create()`.

    `save()` refuses a record whose two halves are gone (store.py:107-124), which
    is the writer `test_a_session_deleted_while_a_socket_held_it_stays_deleted`
    covers. It cannot cover this one: `create()` writes both halves itself and
    never reaches that guard, and the interface calls it with the browser's own
    id when a socket answers 4404 (static/app.js `?create=1`, backend/routers/
    chat.py). So a tab that still held the id in its localStorage copy of the
    list asked the store to re-materialise the session the person had just
    deleted, under that id and with that title.

    The store now remembers the ids it has deleted and refuses them, while an
    unknown id that was never deleted is still adopted — the recovery that
    `test_a_stale_session_is_adopted_under_the_id_the_browser_holds` exists for.
    """

    def test_a_deleted_id_is_not_created_back(self):
        self.make("resurrect-1", title="read alpha.py")
        self.assertTrue(store.remove("resurrect-1"))
        self.assertIsNone(store.create(title="read alpha.py", model="m",
                                       mode="agent", sid="resurrect-1"),
                          "create() re-made a session this store deleted")
        seen = self.read_as_new_process("resurrect-1")
        self.assertFalse(seen["record"], "record file written back by create()")
        self.assertNotIn("resurrect-1", seen["index"], "index row written back")
        self.assertNotIn("resurrect-1", seen["registry"], "served to the browser again")

    def test_an_id_that_was_never_deleted_is_still_adopted(self):
        """The refusal must not eat the recovery it sits next to."""
        rec = store.create(title="a tab that outlived its store", sid="adopt-me-9")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["id"], "adopt-me-9")

    def test_a_new_session_is_never_blocked_by_the_tombstones(self):
        """Only an id the caller supplied can be refused, so `create()` with no
        sid still makes a session however many deletes have happened."""
        for i in range(5):
            self.make(f"bulk-{i}")
            store.remove(f"bulk-{i}")
        fresh = store.create(title="New session")
        self.assertIsNotNone(fresh)
        self.assertTrue(store.get(fresh["id"]))

    def test_the_registry_tells_the_browser_what_is_deleted(self):
        """A stale localStorage copy is the other half of the resurrection, so the
        list has to reach the page that holds it."""
        self.make("listed-1")
        store.remove("listed-1")
        reg = store.registry()
        self.assertIn("listed-1", reg["deleted"])
        self.assertNotIn("listed-1", [s["id"] for s in reg["sessions"]])

    def test_the_browser_drops_deleted_rows_before_rescuing_local_ones(self):
        """The server refusing is only half of it.

        `merge()` never creates an unknown id, but the browser holds its own copy
        in `tacit.sessions`, and a hard refresh reads that before the server
        answers. `loadServerRegistry()` then treats any row the server does not
        list as a local-only session worth rescuing (static/app.js:272) -- so a
        deleted session is resurrected by the page, and pushed straight back to
        the server on the next persist. The filter at :267 has to run before that
        loop, and it has to consult both tombstone sources: the ids the server
        lists, and the ones this browser saw deleted while the store was not
        listening.

        Verified end-to-end against a real DOM (jsdom, seeded localStorage, fake
        socket): with the filter the ghost is absent from the drawer, from
        localStorage and from the push; delete the filter and all three come back.
        """
        src = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(
            encoding="utf-8")
        boot = src[src.index("async function loadServerRegistry"):
                   src.index("async function loadServerRegistry") + 4000]
        self.assertIn("for (const id of (reg.deleted || [])) deletedIds.add(id)", boot,
                      "the server's tombstones must reach the browser's set")
        filter_at = boot.index("sessions = sessions.filter(s => byId.has(s.id) || "
                               "!deletedIds.has(s.id));")
        rescue_at = boot.index("if (ls.messages && ls.messages.length)")
        self.assertLess(filter_at, rescue_at,
                        "the filter must precede the loop that rescues local-only rows")

    def test_the_tombstone_list_is_bounded(self):
        """It is a file that grows one entry per delete and is read on every
        create; an unbounded one is a slow leak with the shape of a feature."""
        for i in range(store.DELETED_CAP + 40):
            store._note_deleted(f"flood-{i}")
        rows = store.deleted()
        self.assertLessEqual(len(rows), store.DELETED_CAP)
        self.assertIn(f"flood-{store.DELETED_CAP + 39}", rows, "the newest delete is kept")
        self.assertNotIn("flood-0", rows, "the cap is not trimming the oldest")


class TheInterleave(Isolated):
    """Hypothesis 2: save() and remove() racing, and the ghost it used to leave."""

    def race(self, rounds=6):
        """Hammer one session with saves while another thread deletes it repeatedly."""
        sid = "race-1"
        self.make(sid)
        errors = []

        def writer():
            try:
                for i in range(rounds * 4):
                    rec = store.get(sid)
                    if rec:
                        store.append(rec, "user", f"ping {i}")
                        store.save(rec)
            except Exception as exc:              # noqa: BLE001 - reported below
                errors.append(exc)

        def destroyer():
            try:
                for _ in range(rounds):
                    store.remove(sid)
            except Exception as exc:              # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=fn) for fn in (writer, writer, destroyer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [], f"the store raised under concurrency: {errors[:3]}")
        return sid

    def test_no_ghost_row_survives_concurrent_save_and_remove(self):
        """A row whose file is gone opens as a session with no transcript. That is the
        signature the plan names, so it is the invariant worth asserting."""
        sid = self.race()
        rows = {r.get("id") for r in store.index()}
        orphans = [r for r in store.index() if store.get(r.get("id", "")) is None]
        self.assertEqual(orphans, [],
                         f"index rows with no record file: {orphans}")
        if sid not in rows:
            self.assertIsNone(store.get(sid), "dropped from the index but still on disk")

    def test_registry_never_hands_the_browser_an_empty_ghost(self):
        sid = self.race()
        served = {s.get("id"): s for s in store.registry()["sessions"]}
        if sid in served:
            self.assertTrue(served[sid]["messages"],
                            "a session served with an empty transcript: the ghost")
        else:
            self.assertNotIn(sid, {r.get("id") for r in store.index()})


if __name__ == "__main__":
    unittest.main()

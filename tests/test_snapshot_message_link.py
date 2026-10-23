"""The undo point a turn left behind, hung on the message that caused it.

PLAN item 4's "Done when": *a snapshot taken during turn N renders a control on message N
that opens compare, and a session with no snapshots renders none.*

The link is a message index written **beside** the snapshot, never inside the row. That
distinction is the whole design: the transcript sent to a model is rebuilt as
`{role, content}` by `agent.transcript_messages`, and the markdown export is written from
the rows, so a field smuggled into a message would change what the model sees and what
the person saves. A sidecar file changes neither.

This file checks three things: that the index is recorded and survives the read path, that
the renderer draws a control only where a real index exists, and that rewinding a session
does not leave old undo points pointing at rows they never made.
"""

import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
PLATFORM = (ROOT / "static" / "platform.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
API = (ROOT / "backend" / "routers" / "api.py").read_text(encoding="utf-8")
CHAT = (ROOT / "backend" / "routers" / "chat.py").read_text(encoding="utf-8")

from backend import agent, config, extras, store  # noqa: E402
from tests.helpers import state_paths  # noqa: E402


def code(src):
    """Source with comments removed, so prose about a thing is not counted as the thing."""
    out = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//[^\n]*", "", out)


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


class TestMarkerIsWritten(Isolated):
    """The index is recorded beside the copy, and nowhere else."""

    def test_a_snapshot_remembers_which_row_it_belongs_to(self):
        self.assertTrue(extras.snapshot(str(self.root), "before edit",
                                         session="s1", message=7).startswith("snapshot"))
        row = extras.snapshot_index(session="s1")[0]
        self.assertEqual(row["message"], 7)

    def test_a_snapshot_with_no_message_has_no_link_rather_than_a_wrong_one(self):
        # `None`, never 0: index 0 is a real row, and pointing an undo point at the
        # first message of a session because a caller passed nothing is the failure
        # mode this whole feature has to avoid.
        extras.snapshot(str(self.root), "manual", session="s1")
        row = extras.snapshot_index(session="s1")[0]
        self.assertIsNone(row["message"])
        self.assertFalse((config.CHECKPOINT_DIR / row["name"] / ".message").exists())

    def test_the_marker_is_not_counted_as_a_project_file(self):
        extras.snapshot(str(self.root), "l", session="s1", message=2)
        row = extras.snapshot_index(session="s1")[0]
        self.assertEqual(row["file_count"], 1)
        self.assertEqual(row["files"], ["a.py"])

    def test_a_corrupt_marker_degrades_to_no_link_instead_of_raising(self):
        extras.snapshot(str(self.root), "l", session="s1", message=2)
        name = extras.snapshot_index(session="s1")[0]["name"]
        (config.CHECKPOINT_DIR / name / ".message").write_text("not a number",
                                                               encoding="utf-8")
        self.assertIsNone(extras.snapshot_index(session="s1")[0]["message"])

    def test_the_endpoint_hands_the_link_through_unprojected(self):
        # The router returns the rows it got; if it ever reshapes them, the index has
        # to be listed there or the transcript stops being able to find its undo points.
        self.assertIn("rows = extras.snapshot_index(limit, session=session)", API)
        self.assertIn('"snapshots": rows', API)


class TestRewindDropsStaleLinks(Isolated):
    """A rewound session must not let old undo points claim new rows.

    PLAN item 4's "Done when" asks what happens when the message a snapshot points
    at is deleted afterwards. `truncate_from` deletes stored rows (edit-and-resend),
    so every marker at or past the cut counts rows that are gone, and the
    transcript then grows back, landing that index on a *different* message. The
    control would open compare against a snapshot from a discarded branch and look
    entirely credible. Dropping the marker keeps the undo point and removes only
    the claim, which is the "shows it's stale" branch of that sentence.
    """

    def _take(self, session, index):
        # The name comes back in the tool's own line ("snapshot <name>: n file(s)")
        # rather than by re-listing: two snapshots taken in the same second sort
        # adjacently, and "the newest" is not a safe way to name the one I just made.
        return extras.snapshot(str(self.root), f"turn {index}",
                               session=session,
                               message=index).split()[1].rstrip(":")

    def test_rows_at_and_past_the_cut_lose_their_link(self):
        keep = self._take("s1", 2)
        drop = self._take("s1", 5)
        self.assertEqual(extras.drop_message_links("s1", 3), 1)
        rows = {r["name"]: r["message"] for r in extras.snapshot_index(session="s1")}
        self.assertEqual(rows[keep], 2)
        self.assertIsNone(rows[drop])

    def test_the_cut_itself_is_dropped_not_kept(self):
        # chat.py stores msgs[:cut], so `cut` is the index of the FIRST deleted row.
        # Treating it as still-alive is the off-by-one that reintroduces the wrong
        # link, just one row later.
        at_cut = self._take("s1", 3)
        self.assertEqual(extras.drop_message_links("s1", 3), 1)
        self.assertFalse((config.CHECKPOINT_DIR / at_cut / ".message").exists())

    def test_the_undo_point_itself_survives(self):
        # Deleting the copy would turn a rewind into data loss. The snapshot exists
        # precisely to recover files the discarded branch changed, and the Snapshots
        # panel still lists it.
        name = self._take("s1", 5)
        extras.drop_message_links("s1", 3)
        d = config.CHECKPOINT_DIR / name
        self.assertTrue(d.is_dir())
        self.assertTrue((d / "a.py").exists())
        self.assertEqual((d / ".session").read_text(encoding="utf-8"), "s1")
        self.assertIn(name, [r["name"] for r in extras.snapshot_index(session="s1")])

    def test_another_sessions_link_is_untouched(self):
        other = self._take("s2", 9)
        self._take("s1", 5)
        self.assertEqual(extras.drop_message_links("s1", 3), 1)
        rows = {r["name"]: r["message"] for r in extras.snapshot_index(session="s2")}
        self.assertEqual(rows[other], 9)

    def test_no_marker_and_a_corrupt_marker_are_not_errors(self):
        # Snapshots from the agent's own `snapshot` tool have no link to lose, and a
        # half-written marker must not raise inside a rewind the user asked for.
        extras.snapshot(str(self.root), "manual", session="s1")
        broken = self._take("s1", 4)
        (config.CHECKPOINT_DIR / broken / ".message").write_text("??", encoding="utf-8")
        self.assertEqual(extras.drop_message_links("s1", 0), 0)

    def test_an_empty_session_drops_nothing(self):
        # `truncate_from` knows its own sid, but a caller passing "" must not be
        # read as "every snapshot with no session marker".
        kept = self._take("s1", 1)
        self.assertEqual(extras.drop_message_links("", 0), 0)
        self.assertEqual(extras.snapshot_index(session="s1")[0]["message"], 1)
        self.assertEqual(extras.snapshot_index(session="s1")[0]["name"], kept)

    def test_the_truncate_handler_drops_before_acking(self):
        # Source-level: this suite has no websocket harness for this branch. The
        # ordering is the substance; app.js redraws and re-fetches /api/snapshots
        # when it sees `truncated`, so dropping after the send races the redraw.
        call = CHAT.index('extras.drop_message_links(rec["id"], cut)')
        ack = CHAT.index('"type": "truncated"')
        self.assertLess(call, ack)


class TestIndexIsTheRightNumber(Isolated):
    """The number written is the row the answer will actually land at."""

    def test_turn_index_is_the_count_of_rows_already_stored(self):
        rec = store.create(title="t")
        for i in range(3):
            store.append(rec, "user" if i % 2 == 0 else "assistant", f"m{i}")
        store.save(rec)
        self.assertEqual(len(store.get(rec["id"])["messages"]), 3)
        self.assertEqual(agent._turn_msg_index(rec["id"]), 3)

    def test_no_session_means_no_link(self):
        # A headless run or a sub-agent has no transcript row to hang on. Guessing 0
        # would put its undo point on someone's first message.
        self.assertIsNone(agent._turn_msg_index(""))
        self.assertIsNone(agent._turn_msg_index("does-not-exist"))

    def test_the_auto_snapshot_passes_the_index_it_computed(self):
        rec = store.create(title="t")
        store.append(rec, "user", "do the thing")
        store.append(rec, "assistant", "working")
        store.save(rec)
        seen = {}
        real = agent.extras.snapshot

        def spy(root, label="", session="", message=None):
            seen["message"] = message
            seen["session"] = session
            return real(root, label, session=session, message=message)

        agent.extras.snapshot = spy
        self.addCleanup(setattr, agent.extras, "snapshot", real)
        agent._auto_snapshot(str(self.root), "write_file", rec["id"])
        self.assertEqual(seen["session"], rec["id"])
        self.assertEqual(seen["message"], 2)

    def test_the_link_never_enters_the_rows_a_model_reads(self):
        rows = [{"role": "user", "content": "hi", "ts": 1.0, "message": 3},
                {"role": "assistant", "content": "yo", "ts": 2.0, "reason": "think",
                 "tools": [{"id": "c1", "name": "read_file", "args": {}, "result": "x"}]}]
        ALLOWED = {"role", "content", "reasoning_content", "tool_calls", "tool_call_id"}
        for out in agent.transcript_messages(rows, reasoning=True):
            self.assertFalse(set(out) - ALLOWED,
                             f"provider row grew {set(out) - ALLOWED}")
        for out in agent.transcript_messages(rows):
            self.assertNotIn("ts", out)
            self.assertNotIn("message", out)


class TestDesktopDrawsTheControl(unittest.TestCase):
    """A real index draws one button; nothing draws zero buttons."""

    def test_every_drawn_row_carries_its_stored_index(self):
        app = code(APP)
        self.assertIn('wrap.dataset.msgi = String(msgi)', app)
        # A grouped transcript must use the group's first row, not its last: the
        # snapshot belongs to the turn, and the turn is the rows combined here.
        self.assertIn("groups.push({ role: msg.role, parts: [msg], at: i })", app)
        self.assertIn("head.origin, head.ts, g.at)", app)

    def test_the_live_bubble_gets_its_index_before_the_row_is_written(self):
        app = code(APP)
        # The live card is created with its own row index and its own clock. It moved
        # out of ensureStreamingBubble() when the card-flow fix made one function own
        # "which bubble does this turn write into"; the index still has to be passed
        # at creation, because the snapshot marker reads it off the element.
        self.assertIn("appendMsg('assistant', '', null, row.ts, s.messages.indexOf(row))",
                      app)
        self.assertIn("const el = appendMsg('assistant', '', null, sent, at);", app)

    def test_the_link_is_read_from_the_index_and_only_from_the_index(self):
        app = code(APP)
        self.assertIn("if (typeof s.message !== 'number') continue;", app)
        # No fallback to 0 anywhere in the painting path.
        self.assertNotIn("s.message || 0", app)
        self.assertNotIn("message || 0", app)

    def test_a_session_with_no_snapshots_paints_nothing(self):
        app = code(APP)
        self.assertIn("if (!rows || !rows.length) return;", app)
        self.assertIn("snapRows = [];", app)

    def test_painting_is_idempotent_because_it_runs_twice_per_turn(self):
        # renderTranscript decorates, and endStream decorates again so this turn's
        # undo point appears without a reload. Without the clear, every turn would
        # add a button to a row that already has one.
        app = code(APP)
        self.assertIn("el.querySelectorAll('.msg-snap').forEach((b) => b.remove());", app)

    def test_only_assistant_rows_are_offered(self):
        self.assertIn(".msg.assistant[data-msgi]", code(APP))

    def test_the_control_opens_compare_through_the_overlay_not_a_new_one(self):
        app = code(APP)
        self.assertIn("const snap = e.target.closest('.msg-snap');", app)
        self.assertIn("window.TacitPlatform.openSnapshot(snap.dataset.snap)", app)
        plat = code(PLATFORM)
        self.assertIn("window.TacitPlatform = { render, openSnapshots, openSnapshot };",
                      plat)
        self.assertIn('[data-act="compare"]', plat)
        # Matched by dataset, never by a selector built out of the name.
        self.assertIn("c.dataset.name === want", plat)
        self.assertNotIn("querySelector(`.snap-card[data-name=", plat)

    def test_the_control_is_written_with_properties_not_interpolation(self):
        # The snapshot name comes off disk. Building it into innerHTML would make a
        # filename a markup injection point in the transcript.
        app = code(APP)
        self.assertIn("b.dataset.snap = s.name;", app)
        self.assertIn("b.textContent = 'snapshot';", app)
        self.assertIn("b.title = `${s.name} · ${s.file_count} file(s)", app)

    def test_the_clock_keeps_the_right_edge(self):
        # The button sits after the role name with a fixed gap. If it took
        # margin-left:auto it would split the free space with .msg-time and the
        # column of times would drift by the width of the label.
        block = re.search(r"\.msg-snap \{([^}]*)\}", CSS)
        self.assertTrue(block, ".msg-snap has no rule in style.css")
        self.assertNotIn("margin-left: auto", block.group(1))
        self.assertIn("margin-left: var(--sp-4)", block.group(1))


class TestTheGuardsCanFail(Isolated):
    """A guard that cannot fail is not a guard."""

    def test_dropping_the_index_costs_the_link_and_nothing_else(self):
        # The regression to catch is the quiet one: snapshots keep working, edits stay
        # undoable from the overlay, and the transcript silently loses every control.
        # So this asserts both halves -- that the caller really does pass an index, and
        # that a callee which ignores it stores no link rather than a zero.
        rec = store.create(title="t")
        store.append(rec, "user", "hi")
        store.save(rec)
        seen = {}
        real = agent.extras.snapshot

        def spy(root, label="", session="", message=None):
            seen["message"] = message
            return real(root, label, session=session)      # forwards without the index

        agent.extras.snapshot = spy
        try:
            agent._auto_snapshot(self._proj, "write_file", rec["id"])
        finally:
            agent.extras.snapshot = real
        self.assertEqual(seen["message"], 1)
        self.assertIsNone(extras.snapshot_index()[0]["message"])

    def test_the_source_guard_would_notice_a_renamed_control(self):
        # Proves the string matches are load-bearing, not incidental: the painting
        # test above keys on the class name the handler routes on.
        self.assertEqual(len(re.findall(r"closest\('\.msg-snap'\)", code(APP))), 1)
        self.assertEqual(len(re.findall(r"className = 'msg-snap'", code(APP))), 1)


if __name__ == "__main__":
    unittest.main()

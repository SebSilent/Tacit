"""The session list is a preview; the transcript stays whole.

PLAN item 8's first symptom was a boot stall: one `/api/sessions/registry`
response carried every message of every session — a 300-message session arrived
with ~200 KB of tool output the sidebar never draws, and the page could not paint
a word until it landed. The fix has two halves, and the second is the dangerous
one: making the list a preview is worthless (and actively data-losing) unless the
browser can still get at the whole transcript before it acts on it.

So these tests pin, in order:

  * `store.registry()` sends the TAIL and says how much it left out, and does not
    touch the session file;
  * the per-session endpoint really serves the full record — `ensureFull()` in
    app.js fetches exactly that URL, and if it were wrong every session would
    silently stay a fragment;
  * the client is wired to upgrade at boot, on switching, and before an edit.

The rewind handler's own refusal of a fragment is in tests/test_contracts.py
(`chat._adoptable`), because that is a contract of `truncate_from`.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, store  # noqa: E402
from tests.helpers import state_paths  # noqa: E402

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"
STYLE_CSS = Path(__file__).resolve().parent.parent / "static" / "style.css"


def _transcript(count, *, size=40):
    """A plausible transcript: alternating turns, each with something to render."""
    rows = []
    for i in range(count):
        if i % 2 == 0:
            rows.append({"role": "user", "content": f"question {i} " + "x" * size,
                         "ts": 1700000000 + i})
        else:
            rows.append({"role": "assistant",
                         "content": f"answer {i} " + "y" * size,
                         "reason": f"because {i}",
                         "tools": [{"id": f"c{i}", "name": "read_file",
                                    "args": {"path": "a.py"},
                                    "result": "z" * size, "is_error": False}],
                         "ts": 1700000000 + i})
    return rows


class IsolatedStore(unittest.TestCase):
    """A throwaway home directory, so nothing here reads or writes real sessions."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
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
        self.addCleanup(self._restore)

    def _restore(self):
        config.HOME = self._home
        for key, value in self._orig.items():
            setattr(config, key, value)

    def _session(self, sid, count, *, size=40, **extra):
        rec = store.create(title=f"{sid} ({count})", sid=sid, project="/tmp/none")
        rec["messages"] = _transcript(count, size=size)
        rec.update(extra)
        store.save(rec)
        return rec


class ThePreviewTail(IsolatedStore):
    def test_a_long_session_arrives_as_its_tail(self):
        rec = self._session("long", 40)
        row = next(r for r in store.registry()["sessions"] if r["id"] == "long")
        self.assertEqual(len(row["messages"]), store.REGISTRY_PREVIEW)
        self.assertEqual(
            [m["content"] for m in row["messages"]],
            [m["content"] for m in rec["messages"][-store.REGISTRY_PREVIEW:]])

    def test_it_says_how_much_was_left_out(self):
        self._session("long", 40)
        row = next(r for r in store.registry()["sessions"] if r["id"] == "long")
        self.assertEqual(row["messageCount"], 40)
        self.assertTrue(row["trimmed"])

    def test_a_short_session_is_not_marked_trimmed(self):
        rec = self._session("short", 5)
        row = next(r for r in store.registry()["sessions"] if r["id"] == "short")
        self.assertFalse(row["trimmed"])
        self.assertEqual(row["messageCount"], 5)
        self.assertEqual(len(row["messages"]), 5)
        self.assertEqual([m["content"] for m in row["messages"]],
                         [m["content"] for m in rec["messages"]])

    def test_the_tail_keeps_what_the_renderer_needs(self):
        # A preview still has to draw bubbles, timestamps, tool cards and reasoning
        # — it is short, not impoverished. The index fields the drawer row is built
        # from (backend/store.py:59) ride along untouched via `{**r}`.
        rec = self._session("rich", 30, mode="agent")
        row = next(r for r in store.registry()["sessions"] if r["id"] == "rich")
        for key in ("id", "title", "model", "mode", "thinking", "project",
                    "created", "updated"):
            self.assertEqual(row[key], rec[key])
        last = row["messages"][-1]
        self.assertIn("ts", last)
        self.assertIn("reason", last)
        self.assertIn("tools", last)

    def test_the_session_file_is_untouched(self):
        # The bound is a projection of the LIST, never a rewrite of the record.
        # A registry call that trimmed the disk would delete history for everyone.
        self._session("disk", 40)
        self.assertEqual(len(store.get("disk")["messages"]), 40)

    def test_the_bound_is_not_stored_in_the_index(self):
        # Derived at read time, so an index written by an older build still works
        # and a changed bound does not need a migration.
        self._session("idx", 40)
        self.assertNotIn("messageCount", next(
            r for r in store.index() if r["id"] == "idx"))

    def test_the_bound_is_a_parameter_not_a_hardcoded_number(self):
        self._session("param", 40)
        row = next(r for r in store.registry(preview=2)["sessions"] if r["id"] == "param")
        self.assertEqual(len(row["messages"]), 2)
        self.assertTrue(row["trimmed"])

    def test_a_response_is_now_small_enough_to_be_worth_it(self):
        # The symptom being fixed is bytes over the wire at boot. This is the only
        # test here that measures the point of the change; it is a regression
        # alarm, not a benchmark.
        for i in range(6):
            self._session(f"bulk{i}", 60, size=200)
        whole = sum(len(store.get(f"bulk{i}")["messages"]) for i in range(6))
        self.assertGreater(whole, 100)
        rows = store.registry()["sessions"]
        drawn = sum(len(r["messages"]) for r in rows)
        self.assertLess(drawn, whole)


class TheFullRecordIsStillReachable(IsolatedStore):
    """`ensureFull()` depends on this route; pin it rather than trust a note."""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from backend.main import app

        self.client = TestClient(app)

    def test_the_per_session_route_serves_everything(self):
        self._session("whole", 40)
        r = self.client.get("/api/sessions/whole")
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        self.assertEqual(len(body["messages"]), 40)
        self.assertNotIn("trimmed", body)   # the flag belongs to the list, not here

    def test_the_registry_row_carries_the_counts_the_client_compares_against(self):
        self._session("counts", 40)
        row = next(s for s in self.client.get("/api/sessions/registry").json()["sessions"]
                   if s["id"] == "counts")
        self.assertEqual(row["messageCount"], 40)
        self.assertTrue(row["trimmed"])
        self.assertLess(len(row["messages"]), row["messageCount"])

    def test_a_missing_session_cannot_pass_for_an_empty_one(self):
        # `_fail` answers with HTTP 200 and no `id`. ensureFull's guard is
        # `rec.id === s.id`, so this body cannot be adopted — which matters because
        # the adoption path ends in `delete s.trimmed`, and a session that lost its
        # id would otherwise be marked whole with nothing in it.
        r = self.client.get("/api/sessions/no-such-session-id")
        body = r.json()
        self.assertNotIn("messages", body)
        self.assertNotIn("id", body)


class GzipOnTheWire(IsolatedStore):
    """The other half of the boot cost: the same JSON, compressed."""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from backend.main import app

        self.client = TestClient(app)

    def test_a_large_response_arrives_compressed(self):
        self._session("gz", 40)
        r = self.client.get("/api/sessions/registry", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
        # The body is still readable, i.e. the middleware did not double-encode or
        # truncate: the client decodes transparently and the JSON parses.
        self.assertIn("sessions", r.json())

    def test_compression_does_not_cost_the_revalidation_header(self):
        # tests/test_api_no_cache.py pins `no-cache` on this path; a middleware
        # added later in the stack must not drop it.
        self._session("gz", 40)
        r = self.client.get("/api/sessions/registry", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers.get("Cache-Control"), "no-cache")

    # Deliberately no "small responses are not compressed" test. That behaviour is
    # GZipMiddleware's own (main.py registers it with minimum_size=1024, commented
    # there); pinning it here meant asserting against Starlette's internals, and the
    # attempt failed twice for a reason the test itself could not state — httpx
    # reports r.content decoded, so a length guard written against the raw body was
    # comparing a different quantity than the middleware uses.

    def test_a_client_that_cannot_take_gzip_is_still_served(self):
        self._session("gz", 40)
        r = self.client.get("/api/sessions/registry", headers={"Accept-Encoding": "identity"})
        self.assertNotIn("Content-Encoding", r.headers)
        self.assertEqual(len(r.json()["sessions"]), 1)


class TheClientUpgradesBeforeItActs(unittest.TestCase):
    """Source pins on static/app.js — the repo's convention for untestable JS.

    Each of these is a place where acting on a preview tail would be wrong, not
    merely ugly: the edit path rewrites the stored transcript from the bubbles on
    screen, so it must never run against a fragment.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = APP_JS.read_text(encoding="utf-8")

    def _body(self, name):
        start = self.js.index(f"function {name}(")
        return self.js[start:self.js.index("\n}", start)]

    def test_the_registry_is_loaded_before_the_first_paint(self):
        # The reorder is the whole point: painting a transcript that is about to be
        # replaced is what made the sidebar and the clock disagree at boot. Inside
        # initSessions the upgrade sits between the two, and it is awaited: boot
        # shows 24 rows then jumps is worse than showing nothing for a moment.
        body = self._body("initSessions")
        self.assertLess(body.index("await loadServerRegistry();"),
                        body.index("await ensureFull(cur());"))
        self.assertLess(body.index("await ensureFull(cur());"),
                        body.index("renderTranscript(cur());"))

    def test_boot_upgrades_the_session_it_is_about_to_draw(self):
        self.assertIn("await ensureFull(cur())", self.js)

    def test_switching_sessions_upgrades_the_one_it_draws(self):
        self.assertIn("ensureFull(cur())", self._body("switchSession"))
        # and only after the socket is attached, which is what marks a mid-turn
        # session busy so the upgrade cannot clobber a live transcript
        body = self._body("switchSession")
        self.assertLess(body.index("attachCurrent()"), body.index("ensureFull(cur())"))

    def test_editing_refuses_a_fragment(self):
        self.assertIn("if (s.trimmed)", self._body("editUserMessage"))
        self.assertIn("ensureFull(s)", self._body("editUserMessage"))

    def test_the_upgrade_reads_the_route_this_file_pinned(self):
        body = self._body("ensureFull")
        self.assertIn("/api/sessions/${s.id}", body)
        # The guard is the id, not the shape: `_fail` answers a missing session with
        # HTTP 200 and no `id`, so a body check alone would accept it as an empty
        # transcript and `delete s.trimmed` would then claim a blank tail was whole.
        self.assertIn("rec && rec.id === s.id", body)
        self.assertIn("delete s.trimmed", body)

    def test_a_fragment_is_admitted_to_the_user(self):
        self.assertIn("transcript-gap", self.js)
        self.assertIn(".transcript-gap", STYLE_CSS.read_text(encoding="utf-8"))

    def test_the_local_copy_is_still_bounded(self):
        # The comparison counts the server's HISTORY, not its rows. Comparing raw
        # array lengths would make every trimmed row look poorer than a local copy
        # and push a truncated transcript over the full one.
        self.assertIn("(ls.messages || []).length > (ss.messageCount",
                      self._body("loadServerRegistry"))


class TheTailIsBoundedByBytes(IsolatedStore):
    """The preview's unit is bytes, because counting messages never bounded it.

    Measured on the real home with `scripts/measure_registry.py`: the boot payload
    was 95.9 KB, and 94.3 KB of that was ONE session's 24-message tail - a single
    stored message measured 7,856 bytes. `REGISTRY_PREVIEW = 24` bounded the count
    and left the size untouched, because the field that actually grows is
    `tools[].args`: `routers/chat.py` clips `result` (MAX_TOOL_RESULT) and `reason`
    (MAX_REASON) but passes `args` through as a dict, so a `write_file` call drags
    the whole file body into every boot response, forever.
    """

    def _fat(self, sid="fat", *, body=40_000, reason=8_000, content=4_000, count=30):
        """A session whose tail is the worst case the store can really hold."""
        msgs = []
        for i in range(count):
            if i % 2:
                msgs.append({"role": "assistant", "content": "a" * content,
                             "reason": "r" * reason,
                             "tools": [{"id": f"c{i}", "name": "write_file",
                                        "args": {"path": "b.py", "content": "b" * body},
                                        "result": "z" * 6_000, "is_error": False}],
                             "ts": 1700000000 + i})
            else:
                msgs.append({"role": "user", "content": f"q{i} " + "u" * 20,
                             "ts": 1700000000 + i})
        rec = store.create(title=f"{sid} fat", sid=sid, project="/tmp/none")
        rec["messages"] = msgs
        store.save(rec)
        return msgs

    def _row(self, sid, reg=None):
        return next(r for r in (reg or store.registry())["sessions"] if r["id"] == sid)

    def test_a_huge_tool_argument_does_not_ride_the_boot_payload(self):
        self._fat()
        text = json.dumps(self._row("fat")["messages"], ensure_ascii=False)
        self.assertNotIn("b" * 1_000, text,
                         "the file body a tool was called with is still shipping")
        self.assertNotIn("z" * 1_000, text)
        self.assertLess(len(text), store.REGISTRY_TAIL_BUDGET)

    def test_the_tool_is_still_the_tool(self):
        # Bounding size must not lose *which* call happened. The client reconciles a
        # tail against the full record on upgrade, and an anonymous tool list cannot
        # be reconciled.
        self._fat()
        last = self._row("fat")["messages"][-1]
        self.assertEqual([t["name"] for t in last["tools"]], ["write_file"])
        self.assertEqual(last["tools"][0]["id"], "c29")
        self.assertIs(last["tools"][0]["is_error"], False)
        self.assertIn("path", last["tools"][0]["args"])

    def test_reason_is_left_out_without_losing_its_key(self):
        self._fat()
        last = self._row("fat")["messages"][-1]
        self.assertIn("reason", last)      # shape is not a size knob
        self.assertIsNone(last["reason"])

    def test_content_is_capped_and_marks_itself(self):
        # A silent 240-character prefix would be indistinguishable from a message
        # that was always that long, which is how a preview becomes "the history".
        self._fat(content=4_000)
        last = self._row("fat")["messages"][-1]
        self.assertTrue(last["content"].endswith(store.PREVIEW_MARKER))
        self.assertEqual(len(last["content"]),
                         store.PREVIEW_CONTENT_CAP + len(store.PREVIEW_MARKER))

    def test_a_row_that_already_fits_is_shipped_verbatim(self):
        self._session("plain", 6)
        row = self._row("plain")
        src = store.get("plain")["messages"]
        for got, want in zip(row["messages"], src):
            self.assertEqual(got["content"], want["content"])
            self.assertNotIn(store.PREVIEW_MARKER, got["content"])
            if want.get("tools"):
                self.assertEqual(got["tools"][0]["result"], want["tools"][0]["result"])

    def test_the_budget_is_the_whole_response_not_each_session(self):
        # Per-session ceilings multiply: six fat sessions each under a 48 KB row cap
        # is still a 288 KB boot payload, which is the bug in a different hat.
        for n in range(6):
            self._fat(sid=f"fat{n}")
        reg = store.registry()
        shipped = sum(len(json.dumps(r["messages"], ensure_ascii=False))
                      for r in reg["sessions"])
        self.assertLessEqual(shipped, store.REGISTRY_TAIL_BUDGET)
        empty = [r for r in reg["sessions"] if not r["messages"]]
        self.assertTrue(empty, "six fat sessions must exhaust one shared budget")
        for r in empty:
            self.assertEqual(r["messageCount"], 30)
            self.assertTrue(r["trimmed"])

    def test_the_budget_spends_newest_first(self):
        self._fat()
        row = self._row("fat", store.registry(budget=3_000))
        self.assertTrue(row["messages"])
        self.assertEqual(row["messages"][-1]["ts"], 1700000000 + 29)

    def test_a_tail_beyond_the_budget_admits_being_empty(self):
        self._fat()
        row = self._row("fat", store.registry(budget=100))
        self.assertEqual(row["messages"], [])
        self.assertEqual(row["messageCount"], 30)
        self.assertTrue(row["trimmed"])
        self.assertEqual(len(store.get("fat")["messages"]), 30)   # the file is whole

    def test_the_old_projection_would_have_shipped_this_whole(self):
        # Counterfactual, so this file cannot quietly stop measuring: the exact
        # shape that was in `registry()` before, over the same data.
        msgs = self._fat()
        old = [{"role": m.get("role"), "content": m.get("content"),
                "reason": m.get("reason"), "tools": m.get("tools"),
                "ts": m.get("ts")} for m in msgs[-store.REGISTRY_PREVIEW:]]
        new = self._row("fat")["messages"]
        before = len(json.dumps(old, ensure_ascii=False))
        after = len(json.dumps(new, ensure_ascii=False))
        self.assertGreater(before, 100_000)
        self.assertLess(after, before / 10)


if __name__ == "__main__":
    unittest.main()

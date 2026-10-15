"""The transcript's clock is a display row, not a model row.

PLAN item 5's "Done when": *timestamps render in both frontends, and a test proves `ts`
is in the transcript payload but never in the provider payload.*

Both halves matter and they pull in opposite directions. The browser cannot draw a time
it never receives, so `ts` has to survive `store.registry()`'s projection. A provider
should not receive a field it did not ask for, so `ts` has to die in
`agent.transcript_messages`, which rebuilds every row from scratch. The seam between
those two is exactly where a future "cleanup" would drop the clock or leak the field, so
it is pinned here rather than trusted.

The other half of the item is that a time is always visible: a hover-only timestamp does
not exist on a phone, and one frontend already drew it permanently.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
MOBILE = (ROOT / "static" / "mobile.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

from backend import agent, config, store  # noqa: E402
from tests.helpers import state_paths  # noqa: E402


def code(src):
    out = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//[^\n]*", "", out)


class Isolated(unittest.TestCase):
    def setUp(self):
        self._tmp = __import__("tempfile").TemporaryDirectory()
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


class TestTsIsStoredAndSent(Isolated):
    """The field exists on the row and reaches the browser."""

    def test_every_appended_row_is_stamped(self):
        rec = store.create(title="t")
        msg = store.append(rec, "user", "hi")
        self.assertIsInstance(msg["ts"], float)
        store.save(rec)
        self.assertIsInstance(store.get(rec["id"])["messages"][0]["ts"], float)

    def test_the_registry_projection_keeps_the_clock(self):
        # The projection is a hand-written whitelist, which is why a field can vanish
        # from it without anything else breaking. This is that "anything else".
        rec = store.create(title="t")
        store.append(rec, "user", "hi")
        store.append(rec, "assistant", "yo")
        store.save(rec)
        rows = store.registry([store.get(rec["id"])])["sessions"]
        msgs = rows[0]["messages"]
        self.assertTrue(all("ts" in m for m in msgs), msgs)
        self.assertEqual([m["ts"] for m in msgs],
                         [m["ts"] for m in store.get(rec["id"])["messages"]])

    def test_an_older_row_without_a_clock_survives_the_read(self):
        rec = store.create(title="t")
        rec["messages"] = [{"role": "user", "content": "written before ts existed"}]
        store.save(rec)
        rows = store.registry([store.get(rec["id"])])["sessions"]
        self.assertIsNone(rows[0]["messages"][0]["ts"])


class TestTsNeverReachesAProvider(Isolated):
    """The same field that must survive to the browser must not go one step further."""

    def test_no_provider_row_carries_ts(self):
        rows = [{"role": "user", "content": "hi", "ts": 1.0},
                {"role": "assistant", "content": "yo", "ts": 2.0, "reason": "r",
                 "tools": [{"id": "c1", "name": "read_file", "args": {"p": "a"},
                            "result": "x"}]},
                {"role": "summary", "content": "s", "ts": 3.0}]
        for out in agent.transcript_messages(rows):
            self.assertNotIn("ts", out)
        for out in agent.transcript_messages(rows, reasoning=True):
            self.assertNotIn("ts", out)

    def test_the_rebuild_is_from_scratch_not_a_filtered_copy(self):
        # `dict(m)` minus a key would leak the next display field added. Asserting the
        # exact key set is what makes that impossible to do quietly.
        rows = [{"role": "user", "content": "hi", "ts": 1.0, "origin": "queue",
                 "snapshot": "whatever", "width": 90}]
        self.assertEqual(agent.transcript_messages(rows),
                         [{"role": "user", "content": "hi"}])


class TestDesktopDrawsTheTime(unittest.TestCase):
    """A time is on the row, permanently, in both the stored and the live path."""

    def test_the_markup_is_a_visible_span_not_a_title_only_hint(self):
        app = code(APP)
        self.assertIn('<span class="msg-time" title="${esc(d.toLocaleString())}">${t}</span>',
                      app)
        self.assertIn("${tag}${timeHtml(ts)}</div>", app)

    def test_a_row_with_no_clock_draws_nothing_rather_than_a_lie(self):
        self.assertIn("if (!t) return '';", code(APP))

    def test_stored_rows_are_timed_from_the_row_not_from_now(self):
        # Rendering a reload with the clock reading the render time would date every
        # old message as just-said.
        app = code(APP)
        self.assertIn("head.origin, head.ts, g.at)", app)

    def test_a_live_row_is_timed_the_moment_it_is_created(self):
        app = code(APP)
        self.assertIn("const sent = nowTs();", app)
        self.assertIn("appendMsg('user', text, null, sent, at - 1);", app)
        self.assertIn("role: 'assistant', content: '', reason: '', tools: [], ts: nowTs()",
                      app)
        self.assertIn("appendMsg('assistant', '', null, last.ts, s.messages.indexOf(last))",
                      app)

    def test_the_clock_is_never_revealed_by_a_pointer(self):
        block = re.search(r"\.msg-time \{(.*?)\n\}", CSS, re.S)
        self.assertTrue(block, ".msg-time has no rule in style.css")
        self.assertNotIn("opacity: 0", block.group(1))
        self.assertNotIn("visibility: hidden", block.group(1))
        self.assertNotIn(":hover", block.group(1))
        self.assertIsNone(re.search(r"\.msg:hover[^{]*\.msg-time", CSS))


class TestMobileDrawsTheTime(unittest.TestCase):
    def test_time_is_rendered_as_text_on_the_row(self):
        mob = code(MOBILE)
        self.assertIn("function timeOf(ts)", mob)
        self.assertIn("meta.className = 'm-meta'", mob)
        self.assertIn("timeOf(msg.ts)", mob)

    def test_mobile_has_no_hover_affordance_at_all(self):
        # Not a style claim: the page has no pointer to hover with, which is why the
        # desktop's hover-only clock had to go rather than be duplicated here.
        self.assertNotIn(":hover", code(MOBILE))


if __name__ == "__main__":
    unittest.main()

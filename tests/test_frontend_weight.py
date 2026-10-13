"""PLAN item 5, the "Lightweight" slice: what the browser loads, and how little of it.

The plan's rule is precise — *"one delegated listener per surface, no per-message
listeners"* — because the transcript is the one surface that grows without bound.
A listener built into a row is not one listener; it is one listener multiplied by
every message in the longest session you ever open, attached at render time, on the
hot path.

Before this slice, `appendMsg()` attached a click listener to every assistant row
(`.reason-toggle`) and every user row (`.msg-edit`), and each tool card attached one
to its `.tool-head` — at both build sites. Tool cards are the worst of them: a single
turn can produce dozens, so the transcript's most numerous node carried its own
listener. Meanwhile the surface already had a delegated click handler for
`.copy-btn`, so both patterns were live at once — which is how "no per-message
listeners" survives for months: nothing checked it, and the one place that did check
(copy buttons) looked like the rule was followed.

These tests pin the structure, not the behaviour: they can prove a row carries no
listener and that one handler routes the four clickable classes a row can produce.
That a click still opens the right card is a browser question (see PLAN deviation).

Scope line, deliberately drawn: the rule is about what the *renderer* multiplies.
Nodes built when you act — the edit dialog's Save/Cancel, a rename input, a toast —
keep their own listeners and are checked by nothing here. They exist one at a time
and die when the interaction ends; a tool card exists per tool call per turn for the
life of the session. That is the difference the plan is protecting, so the guards
cover `appendMsg()` and the card builders and leave the dialogs alone. Panel rows
(`platform.js`, `harness.js`, `git.js`) still attach one listener per row — bounded,
rebuilt on open, and rewriting ~30 of them is a browser-verification task, recorded
as a deviation rather than done blind.

Weight baselines are measured, not assumed (raw / gzip, 2026-10-12, after the
delegation change): app.js 119,253 / 35,485 · style.css 92,300 / 17,023 ·
platform.js 78,710 / 18,427 · index.html 23,770 / 5,194 · git.js 29,327 / 8,570 ·
harness.js 22,684 / 6,613 · assistant.js 21,614 / 6,684 · delegation.js 15,810 /
5,158. **Total 403,468 raw / 103,154 gzip.** The budgets below are that plus ~4%:
grow past it consciously, with a note, rather than accidentally.
"""
import gzip
import re
import shutil
import subprocess
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

RAW_BASELINE = 403_468
GZIP_BASELINE = 103_154
RAW_BUDGET = 420_000
GZIP_BUDGET = 110_000

# What one delegated handler on the transcript surface is allowed to route.
ROUTED = {".copy-btn", ".reason-toggle", ".tool-head", ".msg-edit"}

# Classes a transcript row makes clickable. Each one MUST be routed by the
# delegated handler and MUST NOT get a listener of its own.
ROW_CLICKABLES = ("tool-head", "reason-toggle", "msg-edit", "copy-btn")


def _code(src):
    """`src` with comments blanked out, strings intact.

    Searching raw source finds the word `addEventListener` in a comment explaining
    why it is gone — which reads as a pass or a failure depending on prose.
    """
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    break
                j += 1
            out.append(src[i:j + 1])
            i = j + 1
        elif c == "/" and src[i + 1:i + 2] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            i = j
        elif c == "/" and src[i + 1:i + 2] == "*":
            j = src.find("*/", i)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _block(src, brace_idx):
    """Source from an opening `{` to its matching `}` (strings/comments skipped)."""
    depth, i, n = 0, brace_idx, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    break
                j += 1
            i = j + 1
            continue
        if c == "/" and src[i + 1:i + 2] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and src[i + 1:i + 2] == "*":
            j = src.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[brace_idx:i + 1]
        i += 1
    raise AssertionError("unbalanced braces — the extractor needs a look")


def _function_body(src, name):
    code = _code(src)
    m = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", code)
    assert m, f"{name}() not found"
    return _block(code, code.index("{", m.start()))


def _delegated_handler(src):
    code = _code(src)
    hits = list(re.finditer(r"messagesEl\.addEventListener\(\s*'click'", code))
    return hits, _block(code, code.index("{", hits[0].end())) if hits else ""


def _served_files():
    """Every file index.html actually loads."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="(/static/[^"?]+)', html)
    return html, refs


class ServedBundleTests(TestCase):
    def test_a_transcript_row_carries_no_listener(self):
        """`appendMsg()` builds a row; it must not attach behaviour to it.

        The two listeners it used to add were one line each and looked like the
        cheapest code in the file. They ran once per message, forever.
        """
        body = _function_body((STATIC / "app.js").read_text(encoding="utf-8"), "appendMsg")
        self.assertNotIn("addEventListener", body,
                         "appendMsg() attaches a listener to a row — that is a "
                         "listener per message. Route it through the delegated "
                         "handler on messagesEl instead.")

    def test_no_row_clickable_gets_its_own_listener(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        pat = re.compile(r"querySelector\(\s*['\"]\.(%s)['\"]\s*\)\s*\.addEventListener"
                         % "|".join(ROW_CLICKABLES))
        self.assertEqual(pat.findall(app), [],
                         "a per-row listener came back; these are built per message")

    def test_the_transcript_has_exactly_one_delegated_click_handler(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        hits, handler = _delegated_handler(app)
        self.assertEqual(len(hits), 1,
                         f"{len(hits)} click listeners on messagesEl — the surface "
                         "is meant to have one")
        self.assertIn("closest", handler,
                      "the handler on the surface is not delegating (no closest())")

    def test_every_clickable_in_a_row_is_routed_by_that_handler(self):
        """The routed set is pinned both ways: nothing missing, nothing unaccounted.

        A new clickable inside a row that nobody added a branch for is a silent
        dead button — the same class of failure as a `<script>` that 404s.

        Only `e.target.closest(...)` counts as routing. The handler also calls
        `closest()` on the element it just matched, to find the container to act
        on (`.code-block`, `.reason-block`, `.tool-card`, `.msg`) — pinning those
        too was my first draft and it failed immediately, because they are not
        clickables, they are the same target's ancestry.
        """
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        _, handler = _delegated_handler(app)
        found = set("." + m for m in re.findall(r"e\.target\.closest\(['\"]\.([\w-]+)", handler))
        self.assertEqual(found, ROUTED,
                         f"handler routes {sorted(found)}, expected {sorted(ROUTED)}")

    def test_the_tool_card_toggle_is_not_rebuilt_per_card(self):
        """Both build sites used to attach it; a turn makes dozens of cards."""
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for q in ("'", '"'):
            self.assertNotIn(f".tool-head{q}).addEventListener", app,
                             "a tool card is attaching its own expander again")


class PageWeightTests(TestCase):
    def test_what_the_browser_loads_stays_inside_its_budget(self):
        raw = gz = 0
        for f in sorted(STATIC.iterdir()):
            if f.suffix in (".js", ".css", ".html"):
                b = f.read_bytes()
                raw += len(b)
                gz += len(gzip.compress(b, 9))
        self.assertLessEqual(raw, RAW_BUDGET,
                             f"{raw:,} B raw vs budget {RAW_BUDGET:,} (baseline "
                             f"{RAW_BASELINE:,}) — raise it on purpose, with a note")
        self.assertLessEqual(gz, GZIP_BUDGET,
                             f"{gz:,} B gzipped vs budget {GZIP_BUDGET:,} "
                             f"(baseline {GZIP_BASELINE:,})")

    def test_nothing_is_loaded_from_a_remote_host(self):
        html, _ = _served_files()
        remote = re.findall(r'(?:src|href)="https?://[^"]+', html)
        self.assertEqual(remote, [],
                         "the app now needs a network to render its own chrome")

    def test_every_referenced_asset_exists(self):
        """A `<script>` that 404s takes its feature down silently.

        The plan's own dead-ends list has this shape: a guard that greps a name no
        longer in the file is a false pass. Here the failure mode is the file being
        renamed and the reference left behind — so resolve references, do not
        count them.
        """
        html, refs = _served_files()
        for r in refs:
            self.assertTrue((ROOT / r.lstrip("/")).is_file(), f"{r} is referenced but absent")
        on_disk = {f.name for f in STATIC.iterdir() if f.suffix in (".js", ".css")}
        served = {Path(r).name for r in refs}
        self.assertEqual(on_disk - served, set(),
                         f"{sorted(on_disk - served)} sit in static/ and are never "
                         "loaded — dead weight, or a missing reference")

    def test_the_bundle_needs_no_build_step(self):
        """"No new dependencies, no build step, no framework" — checked, not assumed.

        `package.json` at the repo root does NOT contradict this: it is the optional
        Playwright driver for the browser tool and the frontend never reads it. An
        earlier draft of this test asserted "no package.json" and would have failed
        a healthy app.
        """
        for f in STATIC.glob("*.js"):
            code = _code(f.read_text(encoding="utf-8"))
            self.assertNotIn("require(", code, f"{f.name} expects a module loader")
            self.assertIsNone(re.search(r"^\s*import[\s(*]", code, re.M),
                              f"{f.name} uses import — it is loaded as a classic script")
        pkg = ROOT / "package.json"
        if pkg.is_file():
            data = pkg.read_text(encoding="utf-8")
            self.assertIn("playwright", data)
            self.assertNotIn("react", data)
            self.assertNotIn("vue", data)
            self.assertNotIn("svelte", data)
            joined = "".join(f.read_text(encoding="utf-8") for f in STATIC.glob("*.js"))
            self.assertNotIn("node_modules", joined)


class BundleParsesTests(TestCase):
    """`node --check` is the only verification here that cannot be satisfied by a
    grep: it parses the real file the browser will run. Skips where node is absent
    (it is only needed for the optional Playwright driver), and says so."""

    def test_every_served_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not on PATH — syntax check unavailable")
        _, refs = _served_files()
        for r in refs:
            if not r.endswith(".js"):
                continue
            p = ROOT / r.lstrip("/")
            res = subprocess.run([node, "--check", str(p)], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL)
            self.assertEqual(res.returncode, 0, f"{r} does not parse: {res.stderr[:400]}")


class GuardHasTeethTests(TestCase):
    """The delegation rules must bite on the exact regression they describe."""

    def setUp(self):
        self.app = (STATIC / "app.js").read_text(encoding="utf-8")

    def _row_pat(self, src):
        return re.compile(r"querySelector\(\s*['\"]\.(%s)['\"]\s*\)\s*\.addEventListener"
                          % "|".join(ROW_CLICKABLES)).findall(src)

    def test_a_planted_per_card_listener_is_caught(self):
        planted = self.app.replace(
            "  tools.appendChild(card);",
            "  card.querySelector('.tool-head').addEventListener('click', () =>"
            " card.classList.toggle('open'));\n  tools.appendChild(card);", 1)
        self.assertNotEqual(planted, self.app, "the anchor moved")
        self.assertTrue(self._row_pat(planted), "a per-row listener slipped through")

    def test_a_planted_second_surface_listener_is_caught(self):
        planted = self.app.replace(
            "function renderModelChip()",
            "messagesEl.addEventListener('click', e => {});\nfunction renderModelChip()", 1)
        self.assertNotEqual(planted, self.app)
        hits, _ = _delegated_handler(planted)
        self.assertEqual(len(hits), 2)

    def test_a_dead_branch_is_caught(self):
        """A button whose class nothing routes is invisible in a screenshot."""
        planted = self.app.replace("const edit = e.target.closest('.msg-edit');",
                                   "const edit = e.target.closest('.msg-editor');", 1)
        self.assertNotEqual(planted, self.app)
        _, handler = _delegated_handler(planted)
        found = set("." + m for m in
                    re.findall(r"e\.target\.closest\(['\"]\.([\w-]+)", handler))
        self.assertNotEqual(found, ROUTED)
        # and the markup still builds the old class, so the button is now dead
        self.assertIn('edit.className = \'msg-edit\'', planted)

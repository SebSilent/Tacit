"""the deleted plan item 5, the "Lightweight" slice: what the browser loads, and how little of it.

The plan's rule is precise: *"one delegated listener per surface, no per-message
listeners"*, because the transcript is the one surface that grows without bound.
A listener built into a row is not one listener; it is one listener multiplied by
every message in the longest session you ever open, attached at render time, on the
hot path.

Before this slice, `appendMsg()` attached a click listener to every assistant row
(`.reason-toggle`) and every user row (`.msg-edit`), and each tool card attached one
to its `.tool-head`; at both build sites. Tool cards are the worst of them: a single
turn can produce dozens, so the transcript's most numerous node carried its own
listener. Meanwhile the surface already had a delegated click handler for
`.copy-btn`, so both patterns were live at once, which is how "no per-message
listeners" survives for months: nothing checked it, and the one place that did check
(copy buttons) looked like the rule was followed.

These tests pin the structure, not the behaviour: they can prove a row carries no
listener and that one handler routes the four clickable classes a row can produce.
That a click still opens the right card is a browser question (see the recorded deviation below).

Scope line, deliberately drawn: the rule is about what the *renderer* multiplies.
Nodes built when you act; the edit dialog's Save/Cancel, a rename input, a toast:
keep their own listeners and are checked by nothing here. They exist one at a time
and die when the interaction ends; a tool card exists per tool call per turn for the
life of the session. That is the difference the deleted plan is protecting, so the guards
cover `appendMsg()` and the card builders and leave the dialogs alone. Panel rows
(`platform.js`, `harness.js`, `git.js`) still attach one listener per row; bounded,
rebuilt on open, and rewriting ~30 of them is a browser-verification task, recorded
as a deviation rather than done blind.

Weight baselines are measured, not assumed (raw / gzip, 2026-10-12, after the
delegation change): app.js 119,253 / 35,485 · style.css 92,300 / 17,023 ·
platform.js 78,710 / 18,427 · index.html 23,770 / 5,194 · git.js 29,327 / 8,570 ·
harness.js 22,684 / 6,613 · assistant.js 21,614 / 6,684 · delegation.js 15,810 /
5,158. **Total 403,468 raw / 103,154 gzip.** The budgets below are that plus ~4%:
grow past it consciously, with a note, rather than accidentally.

Re-measured 2026-10-13, raw / gzip: app.js 125,418 / 37,871 · style.css
100,325 / 17,586 · platform.js 81,861 / 19,168 · index.html 23,775 / 5,198 ·
git.js 29,327 / 8,570 · harness.js 24,181 / 7,117 · assistant.js 21,614 / 6,684 ·
delegation.js 15,817 / 5,163. **Total 422,318 raw / 107,357 gzip.**
Two things moved, and only one of them is this change:
* +16,431 raw happened before it; the 2026-10-12 baseline was already stale, and
  the budget had 101 bytes of headroom left when this started. A budget that is
  nearly spent reads as a passing test, not as a warning; that is the real finding
  here, and it is why the number is re-measured rather than nudged.
* +2,419 raw / +203 gzip is the accessibility and sound work: five `--on-accent`
  overrides and their measured ratios in comments, a `.ps-icon.ok` rule replacing a
  hard-coded `#3fb950`, the `tone()`/`previewChimes()` split, and the audition row.
  All of it is prose in comments; the code is ~40 lines.
"""
import gzip
import re
import shutil
import subprocess
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

RAW_BASELINE = 450_542
GZIP_BASELINE = 115_372
# Re-measured at twenty themes: style.css 120,906 / 23,042, every other file
# unchanged. The ten added theme blocks cost 11,207 raw and 2,696 gzip; about
# 1.1 KB each, which is 29 tokens plus the comment saying what the world is and
# why its hues sit where they do.
# baseline + ~4%, the same margin the 2026-10-12 numbers were set with.
#
# Re-measured after the appearance revamp, raw / gzip: app.js 130,734 / 39,742 ·
# style.css 109,699 / 20,674 · platform.js 83,199 / 19,538 · index.html 23,775 /
# 5,198 · git.js 29,327 / 8,570 · harness.js 24,181 / 7,117 · assistant.js 21,614 /
# 6,684 · delegation.js 15,817 / 5,163.
#
# That is +6,422 raw over the 431,924 this started from, and it is all in the two
# files the revamp touched: +5,336 in style.css and +1,086 in app.js. What it bought
# is eight theme blocks carrying their own measured ratios, three token families that
# did not exist (tracking, motion, elevation) replacing 13 raw letter-spacing values
# and one shadow used for everything, a control-state layer where the file previously
# had one `:disabled` rule and no `:active` rule at all, and font stacks that name
# only faces this app can actually resolve.
#
# The gzip side is the one that moved past its line: the old budget had 8,076 B of
# raw headroom and looked comfortable, but gzip headroom was down to a few hundred
# bytes, and prose comments compress far less than the code they describe. Same
# lesson the 2026-10-13 note records; a budget that is nearly spent reads as a
# passing test rather than a warning, so both numbers are re-measured here rather
# than nudged.
# Re-measured 2026-10-14, after the phone page and its script left static/.
#
# index.html now loads every file in the directory, so the per-page set and the
# shipped union are the same files again and the shipped pair is the page pair plus
# the HTML document itself (23,935 raw / 5,451 gzip). Measured: page assets
# 460,789 raw / 121,871 gzip; union 484,724 raw / 127,322 gzip. Each budget is that
# plus ~4%, the margin every note above was set with.
#
# The previous numbers were 2% and 1.7% of headroom away from the page's real load:
# spent, not warning, which is the failure this file's own notes keep naming.
#
# Raised again for the Settings fallback-model picker: `platform.js` grew by about
# 2.1 KB of JS and the page by 1.7 KB, which put the page at 481,236 raw / 128,018
# gzip and the shipped union at 505,555 / 133,488. Both were over the old lines, the
# gzip one quietly so: the raw assert fired first and hid it. Budgets are the new
# figures plus ~4%: 500,000 / 133,000 for the page, 526,000 / 139,000 shipped.
#
# Raised again for the thinking-ladder polling: `app.js` grew by 4.4 KB (the
# fallback cache now carries the time it was filled, so a bare fallback cannot
# outlive the server-side probe that supersedes it) and `assistant.js` by 2.3 KB
# (the panel polls briefly for a ladder that arrives late). Page measured
# 497,300 raw / 133,467 gzip, union 521,619 / 138,936 - the gzip line was already
# over, the shipped gzip line had 64 bytes of headroom left. Budgets are the new
# figures plus ~4%: 517,000 / 139,000 for the page, 542,000 / 144,500 shipped.
RAW_BUDGET = 517_000
GZIP_BUDGET = 139_000
SHIPPED_RAW_BUDGET = 542_000
SHIPPED_GZIP_BUDGET = 144_500

# What one delegated handler on the transcript surface is allowed to route.
# `.msg-snap` joined this set with PLAN item 4: the undo-point control on a message.
# Reviewed as the guard intends; it is built by appendMsg() with no listener of its
# own, routed by the one handler, and it opens the overlay's existing compare rather
# than growing a second snapshot surface. The set stays closed against new clickables.
ROUTED = {".copy-btn", ".reason-toggle", ".tool-head", ".msg-edit", ".msg-snap"}

# Classes a transcript row makes clickable. Each one MUST be routed by the
# delegated handler and MUST NOT get a listener of its own.
ROW_CLICKABLES = ("tool-head", "reason-toggle", "msg-edit", "copy-btn")


def _code(src):
    """`src` with comments blanked out, strings intact.

    Searching raw source finds the word `addEventListener` in a comment explaining
    why it is gone, which reads as a pass or a failure depending on prose.
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
    raise AssertionError("unbalanced braces; the extractor needs a look")


def _function_body(src, name):
    code = _code(src)
    m = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", code)
    assert m, f"{name}() not found"
    return _block(code, code.index("{", m.start()))


def _delegated_handler(src):
    code = _code(src)
    hits = list(re.finditer(r"messagesEl\.addEventListener\(\s*'click'", code))
    return hits, _block(code, code.index("{", hits[0].end())) if hits else ""


PAGES = ("index.html",)   # the one entry point


def _served_files(page="index.html"):
    """Every file one page actually loads.

    Takes the page as a parameter because the reference set is per entry point, not
    per repository: a `<script>` that 404s is the exact failure the asset test below
    exists to catch, and summing all of static/ would charge a device for chrome it
    never downloads.
    """
    html = (STATIC / page).read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="(/static/[^"?]+)', html)
    return html, refs


def _page_bytes(page):
    """raw / gzip of what a device loading `page` actually downloads."""
    raw = gz = 0
    for name in sorted({r.rsplit("/", 1)[-1] for r in _served_files(page)[1]}):
        f = STATIC / name
        if not f.is_file():
            continue                      # the asset test reports that properly
        b = f.read_bytes()
        raw += len(b)
        gz += len(gzip.compress(b, 9))
    return raw, gz


def _shipped_bytes():
    """Everything in static/, which is still bytes somebody ships and reviews."""
    raw = gz = 0
    for f in sorted(STATIC.iterdir()):
        if f.suffix in (".js", ".css", ".html"):
            b = f.read_bytes()
            raw += len(b)
            gz += len(gzip.compress(b, 9))
    return raw, gz


class ServedBundleTests(TestCase):
    def test_a_transcript_row_carries_no_listener(self):
        """`appendMsg()` builds a row; it must not attach behaviour to it.

        The two listeners it used to add were one line each and looked like the
        cheapest code in the file. They ran once per message, forever.
        """
        body = _function_body((STATIC / "app.js").read_text(encoding="utf-8"), "appendMsg")
        self.assertNotIn("addEventListener", body,
                         "appendMsg() attaches a listener to a row, that is a "
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
                         f"{len(hits)} click listeners on messagesEl; the surface "
                         "is meant to have one")
        self.assertIn("closest", handler,
                      "the handler on the surface is not delegating (no closest())")

    def test_every_clickable_in_a_row_is_routed_by_that_handler(self):
        """The routed set is pinned both ways: nothing missing, nothing unaccounted.

        A new clickable inside a row that nobody added a branch for is a silent
        dead button; the same class of failure as a `<script>` that 404s.

        Only `e.target.closest(...)` counts as routing. The handler also calls
        `closest()` on the element it just matched, to find the container to act
        on (`.code-block`, `.reason-block`, `.tool-card`, `.msg`); pinning those
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
        """Per page, then per shipment.

        It used to sum all of static/ as one figure. With two pages that sum was
        496,117 raw while the desktop page itself loaded 449,290 / 118,937 and the
        phone page 157,556 / 36,037 - so the number charged every device for chrome
        it never downloaded, and it could go over its line on a day when no page got
        heavier. Hence the per-page loop below.
        Measured again after the phone page was withdrawn: index.html loads app.js,
        assistant/delegation/git/harness/platform, the shared transport.js and
        markdown.js, and style.css - 460,789 raw / 121,871 gzip. The union of
        static/ is 484,724 / 127,322, i.e. the same files plus the HTML document,
        because there is one entry point again.
        Budgets are each measured figure plus ~4%, the margin every note above was
        set with. The page budgets had drifted to ~2% of headroom by the time this
        was re-measured: spent, not warning.
        """
        for page in PAGES:
            raw, gz = _page_bytes(page)
            self.assertLessEqual(
                raw, RAW_BUDGET,
                f"{page} loads {raw:,} B raw vs budget {RAW_BUDGET:,} - raise it on "
                "purpose, with a note")
            self.assertLessEqual(
                gz, GZIP_BUDGET,
                f"{page} loads {gz:,} B gzipped vs budget {GZIP_BUDGET:,}")

        raw, gz = _shipped_bytes()
        self.assertLessEqual(raw, SHIPPED_RAW_BUDGET,
                             f"{raw:,} B of static/ shipped vs {SHIPPED_RAW_BUDGET:,}")
        self.assertLessEqual(gz, SHIPPED_GZIP_BUDGET,
                             f"{gz:,} B gzipped shipped vs {SHIPPED_GZIP_BUDGET:,}")

    def test_nothing_is_loaded_from_a_remote_host(self):
        for page in PAGES:
            html, _ = _served_files(page)
            remote = re.findall(r'(?:src|href)="https?://[^"]+', html)
            self.assertEqual(remote, [],
                             f"{page} now needs a network to render its own chrome")

    def test_every_referenced_asset_exists(self):
        """A `<script>` that 404s takes its feature down silently.

        The plan's own dead-ends list has this shape: a guard that greps a name no
        longer in the file is a false pass. Here the failure mode is the file being
        renamed and the reference left behind - so resolve references, do not count
        them, and resolve them for every page that is an entry point.
        """
        served = set()
        for page in PAGES:
            refs = _served_files(page)[1]
            for r in refs:
                self.assertTrue((ROOT / r.lstrip("/")).is_file(),
                                f"{page} references {r} and it is absent")
            served |= {Path(r).name for r in refs}
        on_disk = {f.name for f in STATIC.iterdir() if f.suffix in (".js", ".css")}
        self.assertEqual(on_disk - served, set(),
                         f"{sorted(on_disk - served)} sit in static/ and are never "
                         "loaded by either page - dead weight, or a missing reference")

    def test_the_bundle_needs_no_build_step(self):
        """"No new dependencies, no build step, no framework"; checked, not assumed.

        `package.json` at the repo root does NOT contradict this: it is the optional
        Playwright driver for the browser tool and the frontend never reads it. An
        earlier draft of this test asserted "no package.json" and would have failed
        a healthy app.
        """
        for f in STATIC.glob("*.js"):
            code = _code(f.read_text(encoding="utf-8"))
            self.assertNotIn("require(", code, f"{f.name} expects a module loader")
            self.assertIsNone(re.search(r"^\s*import[\s(*]", code, re.M),
                              f"{f.name} uses import; it is loaded as a classic script")
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
            self.skipTest("node not on PATH; syntax check unavailable")
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

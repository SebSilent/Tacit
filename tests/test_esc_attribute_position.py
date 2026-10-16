"""Item 5, the attribute half of the transcript: can a *path* or a *name* escape the
attribute it was put in?  (the deleted plan item 5, the `innerHTML` audit)

`tests/test_md_escapes.py` settled model prose. It left the other ~180 `esc()` call
sites — the ones fed by the filesystem, git and the planner — unexamined, and its own
docstring said the rest of the audit "needs a browser". It does not.

Measured 2026-10-13 before writing this: `static/` defines **six** `esc()`s, one per
module, and **five of them replace `"`**. `app.js`'s did not — it escaped `&`, `<`, `>`
only. That is not cosmetic. `esc()` is interpolated inside double-quoted attributes in
the workspace listing (`wd-item`/`wd-new` `data-path`), the file browser (`fs-quick-btn`,
`fs-row` `data-path`), the prompt queue (`pq-opt` `data-opt`) and the planner's status
line (`ps-detail` `title`). Inside an attribute, `"` is the only character that matters,
and a bare `"`
closes the value and starts a new attribute **on a live element**:

    <button class="wd-item" data-path="x" onmouseover="fetch(1)">x</button>

No `<` is involved, so escaping `<` and `>` covers none of it.

Why the existing suite could not have seen this: every case in `test_md_escapes.py`
asserts on `md()` output, which lands in *text* position, where a bare `"` is inert. The
positions where `"` is load-bearing are precisely the ones no test executed. So this file
runs the real `esc()` under node, renders it into the real attribute shape, and asks a
**parser** — not a substring match — what attributes the browser would see.

Deliberately *not* pinned here: a source rule that "every interpolation inside a quoted
attribute must be esc-wrapped". Measured against the renderers it fired on
`${r.count}`, `${servers.length}`, `${rows.length}`, `${vcsOn}`, `${settings.turns}`
(numbers and booleans, safe) and on `${profiles}`, `${track}`, `${options}` (fragments
already built by esc-wrapping maps). A guard that fails correct code gets switched off.

Known divergence, unfixed and not asserted: the other five `esc()`s coerce with
`String(t == null ? '' : t)`; `app.js`'s calls `.replace` on its argument, so a
non-string there throws. Robustness gap, not an escape gap.
"""

import html.parser
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
# esc() itself now lives in the shared renderer the page loads; app.js keeps a
# one-line wrapper (asserted by TheWrapperStillDelegates below) and no body of its own.
MD = (ROOT / "static" / "markdown.js").read_text(encoding="utf-8")

# A payload that is inert in text position and load-bearing in attribute position.
PAYLOAD = 'x" onmouseover="fetch(1)'

# The attribute sites this file's premise rests on. If one moves, the behavioural cases
# below stop testing the thing they claim to test.
ATTR_SITES = ('data-path="${esc(String(p.path))}"',
              'title="${esc(info.message || \'\')}"')
# Measured 2026-10-13: interpolations inside double-quoted attributes in app.js.
# Seventh site added with the transcript clock (`title="${esc(d.toLocaleString())}"`
# in timeHtml): reviewed, and it is the safe shape -- esc() is the shared one from
# static/markdown.js, which this file already proves escapes `"` and coerces with
# String(), and the value is a Date method's own output.
ATTR_SITE_COUNT = 7

# Only `esc` is lifted out — app.js as a whole touches `document` at load and would throw
# under node. Sliced from the anchor to the end of its line, then checked.
_TAIL = """
const P = JSON.parse(process.env.TACIT_PAYLOAD);
const OUT = { raw: [], rendered: [] };
for (const p of P) {
  OUT.raw.push(esc(p));
  OUT.rendered.push(
    `<button class="wd-item" data-path="${esc(String(p))}">x</button>` +
    `<span class="ps-detail" title="${esc(p)}">body</span>`);
}
console.log(JSON.stringify(OUT));
"""


def esc_source(source=MD):
    # The renderer sits inside markdown.js's IIFE, so its definitions are indented.
    # Matching the raw line would find nothing and the harness below would then run
    # an empty payload - the vacuous pass this slice exists to prevent.
    lines = [ln.strip() for ln in source.splitlines()
             if ln.lstrip().startswith("function esc(t)")]
    if not lines:
        raise AssertionError("could not slice markdown.js's esc() — it no longer starts with "
                             "'function esc(t)'; the harness would be running nothing")
    body = lines[0]
    for needed in ("&amp;", "&lt;", "&gt;"):
        assert needed in body, f"sliced line is not esc(): {body!r}"
    return body


def run_esc(values, source=MD):
    """Feed `values` through the shared renderer's real `esc()` and render them into attributes."""
    payload = esc_source(source) + _TAIL
    fd, path = tempfile.mkstemp(suffix=".js", prefix="tacit_esc_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        env = {**os.environ, "TACIT_PAYLOAD": json.dumps(values)}
        # stdin=DEVNULL: incident 0.2 was children blocking on an inherited stdin.
        res = subprocess.run([shutil.which("node"), path], capture_output=True,
                             text=True, stdin=subprocess.DEVNULL, timeout=60, env=env)
    finally:
        os.unlink(path)
    if res.returncode != 0:
        raise AssertionError("node failed: " + res.stderr[:800])
    return json.loads(res.stdout)


DEF = re.compile(r"function esc\(|const esc = ")
# app.js's esc is a delegation, not an escaper: counting it as a seventh definition
# would report it as the one missing the quote and the coercion, which is true of a
# line whose whole job is to call the thing that has both.
DELEGATE = re.compile(r"return window\.TacitMarkdown\.esc\(")


def static_sources():
    return {js.name: js.read_text(encoding="utf-8")
            for js in sorted((ROOT / "static").glob("*.js"))}


def esc_definitions(sources):
    """name → the statement body of its `esc` definition.

    The boundary is the `;` that closes the statement, NOT the first one: these bodies
    are full of HTML entities, and `'&amp;'`, `'&lt;'`, `'&gt;'`, `'&quot;'` each end in a
    semicolon. The first draft sliced at the first `;`, so all six came back truncated at
    their first entity and all six were reported as missing the quote — a guard that
    failed correct code, caught by running it rather than by reasoning about it."""
    found = {}
    for name, text in sources.items():
        for m in DEF.finditer(text):
            body = text[m.start():]
            end = None
            for sm in re.finditer(r";", body):
                if re.fullmatch(r"[\s}]*", body[sm.end():].split("\n", 1)[0]):
                    end = sm.end()
                    break
            if end is None:
                raise AssertionError(f"{name}: no closing `;` located for esc() — the "
                                     "definition changed shape and the guard would be "
                                     "silently reading nothing")
            if DELEGATE.search(body):
                continue
            found[name] = body[:end]
    return found


# Four of the six are arrows (`const esc = (s) => String(s == null ? '' : s)`, parameter
# even named `s`), so matching `return String(` reported assistant/delegation/git/platform
# as uncoerced when they were the ones that always were. The invariant is that the value
# is wrapped before the first `.replace`, whichever keyword introduces it.
COERCION = re.compile(r"(?:return|=>)\s*String\(")


_TAIL_COERCE = """
const esc = new Function(SRC + "\\nreturn esc;")();
const VALS = [null, undefined, 0, 12345, false, NaN, '', 'a"b<c>&d'];
const out = [];
for (const v of VALS) {
  try { out.push({ inp: String(v), threw: false, res: esc(v) }); }
  catch (e) { out.push({ inp: String(v), threw: true, res: e.constructor.name }); }
}
console.log(JSON.stringify(out));
"""


def run_coercion(source=MD):
    """Run the shared renderer's real `esc()` over non-strings and report what each one did.

    A fresh `new Function` per value set, because `esc()` is a declaration and the point
    of the exercise is which argument throws — a renderer that throws here is caught by
    the surrounding try/catch and the pane silently loses its content."""
    payload = "const SRC = " + json.dumps(esc_source(source)) + ";\n" + _TAIL_COERCE
    fd, path = tempfile.mkstemp(suffix=".js", prefix="tacit_coerce_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        res = subprocess.run(["node", path], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
    finally:
        os.unlink(path)
    if res.returncode != 0:
        raise AssertionError("node failed: " + res.stderr[:800])
    return {r["inp"]: r for r in json.loads(res.stdout)}


class TagReader(html.parser.HTMLParser):
    """Collects the attributes a parser actually sees. Substring matching cannot answer
    "did the value escape its attribute" — the text `onmouseover` is present in the
    *correct* output too, sitting inertly inside a quoted value."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def attributes_of(html_str):
    r = TagReader()
    r.feed(html_str)
    r.close()
    return r.tags


@unittest.skipUnless(shutil.which("node"), "node not on PATH — esc() cannot be run")
class EscHoldsTheAttribute(unittest.TestCase):
    """Behavioural: these are strings `esc()` actually returned."""

    @classmethod
    def setUpClass(cls):
        cls.out = run_esc([PAYLOAD, '<a href="y">A & B</a>', "plain"])

    def test_the_quote_is_escaped(self):
        self.assertNotIn('"', self.out["raw"][0], f"quote survived: {self.out['raw'][0]!r}")
        self.assertIn("&quot;", self.out["raw"][0])

    def test_the_base_three_are_still_escaped(self):
        """A fix that added `"` and dropped the rest would pass the case above and ship
        the old hole."""
        out = self.out["raw"][1]
        for token in ("&lt;", "&gt;", "&amp;"):
            self.assertIn(token, out)
        self.assertNotIn("<", out)
        self.assertNotIn(">", out)

    def test_a_plain_value_is_untouched(self):
        """The pair of the two above: an escaper that mangled everything would pass the
        security assertions and render every path wrong."""
        self.assertEqual("plain", self.out["raw"][2])

    def test_the_parser_sees_no_injected_attribute(self):
        tags = attributes_of(self.out["rendered"][0])
        self.assertEqual(2, len(tags), f"expected a button and a span, saw {tags}")
        for tag, attrs in tags:
            self.assertNotIn("onmouseover", attrs,
                             f"{tag} carries an attribute the value invented: {attrs}")

    def test_the_value_still_arrives_intact(self):
        """Inert *and* faithful: the browser decodes `&quot;` back to `"` inside the
        attribute value, so the path is usable — it just cannot end the value."""
        tag, attrs = attributes_of(self.out["rendered"][0])[0]
        self.assertEqual("button", tag)
        self.assertEqual(PAYLOAD, attrs["data-path"])
        self.assertEqual("wd-item", attrs["class"])

    def test_the_title_site_holds_too(self):
        _tag, attrs = attributes_of(self.out["rendered"][0])[1]
        self.assertEqual(PAYLOAD, attrs["title"])
        self.assertNotIn("onmouseover", attrs)


class ThePremiseIsStillTrue(unittest.TestCase):
    """The cases above render a *reproduced* template. If the real sites disappear, the
    behavioural proof stops being about the app."""

    def test_the_audited_attribute_sites_are_still_escaped(self):
        for site in ATTR_SITES:
            self.assertIn(site, APP, f"audited attribute site moved: {site!r}")

    def test_the_guard_still_sees_every_attribute_interpolation(self):
        """Anti-vacuity, and the number that makes a seventh site a review."""
        seen = re.findall(r'="\$\{esc\(', APP)
        self.assertEqual(ATTR_SITE_COUNT, len(seen),
                         f"{len(seen)} double-quoted attributes interpolate esc(); the "
                         f"audited number is {ATTR_SITE_COUNT}")


class EveryEscaperEscapesTheQuote(unittest.TestCase):
    """The house standard across all six definitions. `app.js` diverged by one clause and
    that divergence *was* the hole."""

    def test_it_reads_every_definition(self):
        """Anti-vacuity: an empty `missing` list is also what you get when the pattern
        matched no definition at all."""
        defs = esc_definitions(static_sources())
        self.assertEqual(6, len(defs),
                         f"found {sorted(defs)}; static/ defines six esc()s — if this "
                         "changes, the guard below has stopped covering all of them")

    def test_no_definition_is_missing_the_quote(self):
        missing = sorted(name for name, body in esc_definitions(static_sources()).items()
                         if "&quot;" not in body)
        self.assertEqual([], missing,
                         f"{missing} escape &<> but not the quote, so values there can "
                         "end their attribute")

    def test_no_definition_is_missing_the_coercion(self):
        """The second divergence in the same six definitions: every one of them wraps the
        argument in `String()` before calling `.replace`. `app.js` did not, so `esc(0)`
        threw a TypeError into whatever renderer called it — and eight call sites, on six
        lines of the workspace and file-browser renderers, had already started writing
        `esc(String(p.path))` by hand to avoid it. No line numbers here on purpose: this
        file has already had to re-derive them twice after edits landed above them.

        Recorded honestly: no call site passes a non-string *statically*, so this was
        latent, not live — the guard is against a value arriving from JSON as something
        else, which is exactly what those eight hand-written coercions were defending."""
        missing = sorted(name for name, body in esc_definitions(static_sources()).items()
                         if not COERCION.search(body))
        self.assertEqual([], missing,
                         f"{missing} call `.replace` on the raw argument, so a non-string "
                         "throws instead of rendering")

    def test_the_coercion_is_not_an_accident_of_the_slice(self):
        """Anti-vacuity for the test above: a slice truncated before `return String(`
        would report every definition as missing the coercion, which is the exact failure
        this file already had once with the quote clause."""
        bodies = esc_definitions(static_sources())
        self.assertEqual(6, len(bodies))
        for name, body in bodies.items():
            self.assertIn(".replace(", body, f"{name}'s slice does not contain the body")


@unittest.skipUnless(shutil.which("node"), "node not on PATH — esc() cannot be run")
class NonStringsDoNotThrow(unittest.TestCase):
    """Behavioural: what the real `esc()` returns for values it used to throw on."""

    def setUp(self):
        self.out = run_coercion()

    def test_no_non_string_throws(self):
        threw = {k: v["res"] for k, v in self.out.items() if v["threw"]}
        self.assertEqual({}, threw, "esc() threw on these, and the caller's try/catch "
                                    "turns that into a pane with no content")

    def test_the_coerced_values(self):
        """Pinned as strings, not as 'did not throw': `esc(null)` returning `'null'`
        would also not throw, and would print the word null into a path."""
        for inp, want in {"null": "", "undefined": "", "0": "0", "12345": "12345",
                          "false": "false", "NaN": "NaN", "": ""}.items():
            self.assertEqual(want, self.out[inp]["res"], f"esc({inp})")

    def test_strings_are_unaffected_by_the_coercion(self):
        """The coercion must not change what a string escaper does — otherwise this fix
        is a rendering regression wearing a security costume."""
        self.assertEqual('a&quot;b&lt;c&gt;&amp;d', self.out['a"b<c>&d']["res"])


class TheGuardHasTeeth(unittest.TestCase):
    """The fix must be the thing that makes these pass. Put the old `esc()` back and
    require the parser to see the injected attribute — which also proves the finding was
    real rather than theoretical."""

    OLD = ("function esc(t) { return t.replace(/&/g, '&amp;').replace(/</g, '&lt;')"
           ".replace(/>/g, '&gt;'); }")

    def test_the_regression_is_caught(self):
        if not shutil.which("node"):
            self.skipTest("node not on PATH")
        source = MD.replace(esc_source(), self.OLD)
        self.assertIn(self.OLD, source, "could not find esc()'s line to revert")
        out = run_esc([PAYLOAD], source=source)
        tag, attrs = attributes_of(out["rendered"][0])[0]
        self.assertIn("onmouseover", attrs,
                      "the unescaped esc() did NOT inject an attribute — so the passing "
                      "tests above are guarding nothing")
        self.assertEqual("x", attrs["data-path"], "the quote did not close the value")

    UNCOERCED_MARK = "String(t == null ? '' : t)"

    def test_the_coercion_guard_is_caught(self):
        """Put `app.js` back to the pre-fix form and require the six-definition guard to
        name it — the same teeth check the quote clause has, because both guards fail
        vacuously if their slice is wrong."""
        sources = static_sources()
        line = esc_source(sources["markdown.js"])
        self.assertIn(self.UNCOERCED_MARK, line,
                      "app.js's esc() no longer coerces the way this test assumes")
        planted = sources["markdown.js"].replace(line, line.replace(self.UNCOERCED_MARK, "t"))
        sources["markdown.js"] = planted
        missing = sorted(n for n, body in esc_definitions(sources).items()
                         if not COERCION.search(body))
        self.assertEqual(["markdown.js"], missing,
                         "the coercion was removed and the six-definition guard did not "
                         "notice — it is matching something other than the "
                         "coercion, in whichever dialect the definition is written")

    def test_the_uncoerced_form_actually_threw(self):
        """The fix must be observable, not stylistic. Run the pre-fix `esc()` over the
        same values and require the TypeError: if the uncoerced version also returns
        `''`, then nothing was broken and this test file is guarding a preference."""
        if not shutil.which("node"):
            self.skipTest("node not on PATH")
        line = esc_source()
        out = run_coercion(source=MD.replace(line, line.replace(self.UNCOERCED_MARK, "t")))
        self.assertTrue(out["null"]["threw"], "the uncoerced esc() did not throw on null — "
                                             "so `test_no_non_string_throws` guards nothing")
        self.assertEqual("TypeError", out["null"]["res"])
        self.assertTrue(out["0"]["threw"], "and it did not throw on a number either")

    def test_the_quote_guard_is_caught(self):
        """Teeth for `EveryEscaperEscapesTheQuote`: put the old `app.js` esc back in
        memory and require that guard to name the file. Without this, the six-definition
        check is a regex that could be matching nothing and reporting success."""
        sources = static_sources()
        planted = sources["markdown.js"].replace(esc_source(sources["markdown.js"]), self.OLD)
        self.assertIn(self.OLD, planted, "could not plant the regression in app.js")
        sources["markdown.js"] = planted
        missing = sorted(n for n, body in esc_definitions(sources).items()
                         if "&quot;" not in body)
        self.assertEqual(["markdown.js"], missing,
                         "the quote clause was removed from app.js's esc() and the "
                         "six-definition guard did not notice")


class TheWrapperStillDelegates(unittest.TestCase):
    """The move is only safe while app.js has no escaper of its own.

    `EveryEscaperEscapesTheQuote` counts six definitions; if app.js grew a body back,
    it would be a seventh escaper that nothing audits, and the transcript would be
    escaping model text in a place the house standard has never looked at.
    """
    WRAPPER = "function esc(t) { return window.TacitMarkdown.esc(t); }"

    def test_app_js_calls_the_shared_escaper(self):
        self.assertIn(self.WRAPPER, APP, "app.js no longer delegates its esc()")

    def test_app_js_holds_no_escaper_body(self):
        self.assertNotIn("function esc(t) { return String(", APP,
                         "app.js grew its own esc() body back again")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Item 5, the security half of the transcript: can text the model wrote become
markup?  (the deleted plan item 5, "Lightweight" / the `innerHTML` audit)

the deleted plan filed "the `innerHTML` sites' XSS audit" under *needs a browser*. That is the
wrong tool. Clicking a page cannot prove an escaper — a click-through shows you the one
input you thought to try — and it says nothing about the rest of the assignment sites.
What settles the question is **executing the renderer**, node is available here, so this
file lifts the real `esc` / `inlineMd` / `md` out of `static/app.js`, runs them under
node, and asserts on the HTML that comes back.

Measured before writing any of it: `md()` escapes at `app.js:1667` **and** at both
re-insertion sites — fenced code at `:1690`, table cells at `:1695` — and it autolinks
only `https?:`, so `[x](javascript:…)` never becomes an `<a href>`. The renderer is
sound today. Nothing in this repo proved that, and the property is exactly one dropped
`esc()` away from being false, because code blocks and tables are extracted **before**
escaping: re-inserting them unescaped puts raw model text into `innerHTML` at `:585`,
`:1081`, `:1095`, `:1174` and `:1333`, which is the one path where the browser is
rendering something an agent wrote.

Tool results and reasoning are the other two untrusted sinks. They never pass through
`md()` — they are written with `textContent`, which cannot parse markup at all. That is
a claim about source text, so it is guarded statically rather than executed.
"""

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

# ───────────────────────── the renderer, extracted ─────────────────────────
# One contiguous block: esc → inlineMd → the table helpers → md. Sliced forward from
# the first anchor to a comment that follows the function, so a slice cannot silently
# come back empty (an empty slice is the failure mode that looks like missing code).
_START = "function esc(t)"
_END = "// delegated: the ONE click handler"


def renderer_source():
    s = APP.index(_START)
    e = APP.index(_END, s)          # bounded from the anchor forward
    return APP[s:e]


def _js(cases, src):
    """Run `md()` over `cases` (dict name → markdown) and return name → html."""
    payload = (src + "\nconst C = " + json.dumps(cases) +
               "\nconst O = {}; for (const k in C) O[k] = md(C[k]);"
               "\nconsole.log(JSON.stringify(O));\n")
    fd, path = tempfile.mkstemp(suffix=".js", prefix="tacit_md_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        # stdin=DEVNULL is not decoration: incident 0.2 was children blocking on an
        # inherited stdin. A test that hangs the suite for it is the same bug again.
        res = subprocess.run([shutil.which("node"), path], capture_output=True,
                             text=True, stdin=subprocess.DEVNULL, timeout=60)
    finally:
        os.unlink(path)
    if res.returncode != 0:
        raise AssertionError("node failed: " + res.stderr[:800])
    return json.loads(res.stdout)


# ─────────────────────── the static half of the audit ───────────────────────
# Literal markup is stripped before matching. The first draft matched the bare word
# `reason` against any line containing innerHTML and flagged app.js:1135 — a skeleton
# write whose only "payload" was the class name `reason-block`. A class name is not
# text an agent wrote; it is a name the front-end chose.
_LITERAL = re.compile(r"`(?:[^`\\]|\\.)*`|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
# Which names carry agent-written text, measured against app.js rather than guessed:
# `m.result` / `t.result` / `.reason` / `.content` / `.reasoning`, plus — at the four
# re-render sinks — `_raw` (the streaming buffer) and `text` (the argument at :1333).
# `_raw` and `text` were missing from the first draft, which left **4 of the 5 audited
# sinks unexamined**: deleting `md()` from `c.innerHTML = md(text)` passed the guard.
# Widening is free — measured 2026-10-13, the wider list matches 4 live sink lines and
# 0 non-md() lines, so it adds coverage without inventing false positives.
_PAYLOAD = re.compile(r"\b(?:m|t)\.result\b|\.reason\b|\.content\b|\.reasoning\b"
                      r"|_raw\b|\btext\b")
_MD_WRAPPED = re.compile(r"\.innerHTML\s*=\s*md\(")


def payload_violations(source):
    """innerHTML assignments that take model text without passing it through md()."""
    bad = []
    for ln in source.splitlines():
        s = ln.strip()
        if ".innerHTML" not in s or s.startswith("//"):
            continue
        if not _PAYLOAD.search(_LITERAL.sub(" ", s)):
            continue
        if _MD_WRAPPED.search(s):        # allowed: the escaper is in the path
            continue
        bad.append(s)
    return bad


CASES = {
    "raw_html": "<img src=x onerror=alert(1)>",
    "code_breakout": "```\n</code><svg onload=alert(1)>\n```",
    "table_cell": "| a | b |\n| --- | --- |\n| <script> | ok |",
    "js_url": "[click](javascript:alert(1))",
    "real_link": "[ok](https://example.com/a?b=1)",
    "inline_code": "`<b>x</b>`",
    "heading": "# <i>hi</i>",
    "quote": "> <b>quoted</b>",
    "list": "- <em>one</em>",
    "plain": "just words",
}


@unittest.skipUnless(shutil.which("node"), "node not on PATH — renderer cannot be run")
class RendererEscapes(unittest.TestCase):
    """Behavioural: these are strings `md()` actually returned."""

    @classmethod
    def setUpClass(cls):
        cls.out = _js(CASES, renderer_source())

    def test_the_extraction_is_the_real_renderer(self):
        """Anti-vacuity. A slice that grabbed a comment would make every case below
        fail in the *harness*, not in the assertion — so name the parts."""
        src = renderer_source()
        for needed in ("function esc(t)", "function inlineMd(t)", "function md(src)",
                       "const TBL_SEP", "splitTableRow"):
            self.assertIn(needed, src)
        self.assertLess(src.index("function esc(t)"), src.index("function md(src)"))

    def test_it_renders_markdown_at_all(self):
        """If `md()` were stubbed to the identity, the escaping assertions below would
        pass on raw text for the wrong reason. Prove markup is being produced."""
        self.assertIn("<h1>", self.out["heading"])
        self.assertIn("<blockquote>", self.out["quote"])
        self.assertIn("<li>", self.out["list"])
        self.assertIn("<p>", self.out["plain"])
        self.assertIn("<table>", self.out["table_cell"])
        self.assertIn("<code>", self.out["code_breakout"])

    def test_raw_html_in_prose_cannot_survive(self):
        out = self.out["raw_html"]
        self.assertNotIn("<img", out)
        self.assertIn("&lt;img", out)

    def test_a_code_block_cannot_break_out_of_its_own_tag(self):
        """The interesting one. Fenced code is lifted out *before* escaping and put
        back afterwards, so this is where an escaper gets forgotten."""
        out = self.out["code_breakout"]
        self.assertNotIn("<svg", out)
        self.assertIn("&lt;svg", out)
        self.assertNotIn("</code><svg", out)

    def test_a_table_cell_cannot_break_out_either(self):
        out = self.out["table_cell"]
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_inline_code_and_heading_bodies_are_escaped(self):
        self.assertNotIn("<b>", self.out["inline_code"])
        self.assertNotIn("<i>", self.out["heading"])
        self.assertNotIn("<b>", self.out["quote"])
        self.assertNotIn("<em>", self.out["list"])

    def test_javascript_urls_are_never_linked(self):
        """`inlineMd` links only `https?:`, so the scheme cannot be smuggled in. If it
        ever matched `[text](url)` generally, this is the hole.

        The scheme itself is *supposed* to survive: it comes back as inert text inside a
        paragraph, which is what the first draft of this test wrongly forbade. What must
        never appear is an anchor carrying it — that is the actual execution vector.
        """
        out = self.out["js_url"]
        self.assertNotIn("<a", out)
        self.assertNotIn("href", out)
        self.assertIn("javascript:alert(1)", out)   # present, and harmless
        self.assertTrue(out.startswith("<p>"), out)

    def test_a_real_link_still_works(self):
        """The pair of the test above: an escaper that neutered *everything* would pass
        the security assertions and ship a broken transcript."""
        out = self.out["real_link"]
        self.assertIn('<a href="https://example.com/a?b=1"', out)
        self.assertIn('rel="noopener"', out)


class TheGuardHasTeeth(unittest.TestCase):
    """`md()` passing is only evidence if this file can see it fail. Neuter `esc()` in
    an in-memory copy and require the escape assertions to go red."""

    def test_widening_the_link_matcher_is_caught(self):
        """The `javascript:` case means nothing unless a real change to the link rule
        turns it red. `inlineMd` links `https?:` only (`app.js:1629`); generalise the
        target and the scheme becomes a live href — the hole the deleted plan filed under
        *needs a browser*, closed without one."""
        src = renderer_source()
        line = next(l for l in src.splitlines() if "https?" in l and "replace" in l)
        broken = src.replace(line, line.replace(r"(https?:\/\/[^\s)]+)", r"([^\s)]+)"))
        self.assertNotEqual(broken, src, "could not find the autolink regex to widen")
        out = _js(CASES, broken)
        self.assertIn("<a", out["js_url"], "widened link matcher and still no anchor? "
                                           "the assertion below guards nothing")
        self.assertIn("href", out["js_url"])

    def test_neutering_esc_breaks_the_escape_cases(self):
        if not shutil.which("node"):
            self.skipTest("node not on PATH")
        src = renderer_source()
        line = next(l for l in src.splitlines() if l.startswith("function esc(t)"))
        broken = src.replace(line, "function esc(t) { return t; }")
        self.assertNotEqual(broken, src, "could not find esc()'s body to neuter")
        out = _js(CASES, broken)
        self.assertIn("<img", out["raw_html"])          # would fail the real guard
        self.assertIn("<svg", out["code_breakout"])
        self.assertIn("<script>", out["table_cell"])
        # …and the markdown still renders, so this is a silent break, not a loud one
        self.assertIn("<h1>", out["heading"])

    def test_dropping_md_at_any_audited_sink_is_caught(self):
        """Teeth for the *static* guard, on every sink that carries model text. Each
        plant is the realistic regression — the escaper deleted, the render still
        working. The first draft's pattern saw only `last.content`, so the other three
        could regress with the suite green."""
        plants = [
            ("c.innerHTML = md(last.content || '');", "c.innerHTML = last.content || '';"),
            ("cc.innerHTML = md(cc._raw || '');", "cc.innerHTML = cc._raw || '';"),
            ("c.innerHTML = md(c._raw || '');", "c.innerHTML = c._raw || '';"),
            ("c.innerHTML = md(text);", "c.innerHTML = text;"),
        ]
        for good, bad in plants:
            self.assertIn(good, APP, f"audited sink moved: {good!r}")
            hit = payload_violations(APP.replace(good, bad))
            # `any(... in ...)`, not equality: two of these sinks sit inside a larger
            # `if (…) { … }` statement, so the flagged line is the whole statement.
            self.assertTrue(any(bad in h for h in hit),
                            f"md() dropped at {good!r} and the guard did not notice")
        self.assertEqual([], payload_violations(APP))   # clean before the plants


class UntrustedSinksStayOnTextContent(unittest.TestCase):
    """Tool results and reasoning are the other model-authored text in the transcript.
    They bypass `md()` entirely and are assigned with `textContent`, which cannot parse
    markup — so the guarantee does not depend on an escaper at all."""

    def test_no_untrusted_payload_is_written_as_html(self):
        self.assertEqual([], payload_violations(APP),
                         "untrusted text reaches innerHTML without md()")

    def test_the_guard_is_not_looking_at_nothing(self):
        """Anti-vacuity for the guard above: an empty `bad` list is also what you get
        when the payload regex matches no line at all. The audited sinks must actually
        be inside the pattern's view — 4 of the 5 carry model text through a name in
        `_PAYLOAD` (the 5th is the literal `'(empty response)'`, which has no payload)."""
        seen = [s.strip() for s in APP.splitlines()
                if ".innerHTML" in s and not s.strip().startswith("//")
                and _PAYLOAD.search(_LITERAL.sub(" ", s.strip()))]
        self.assertEqual(4, len(seen),
                         f"the payload pattern reaches {len(seen)} innerHTML lines; "
                         "the audited sinks are 4 payload-carrying + 1 literal — if this "
                         "drops, the guard has stopped looking at the things it guards")

    def test_the_three_known_sinks_are_textContent(self):
        for needle in ("textContent = m.result", "textContent = t.result",
                       "querySelector('.reason-body').textContent"):
            self.assertIn(needle, APP, f"{needle!r} no longer writes as text")

    def test_the_model_text_html_sinks_are_few_and_pinned(self):
        """Every place agent prose becomes HTML. A sixth one is a review, not a merge."""
        sites = re.findall(r"\.innerHTML = md\(", APP)
        self.assertEqual(5, len(sites),
                         "model text enters innerHTML through md() at "
                         f"{len(sites)} sites; the audited number is 5")


if __name__ == "__main__":
    unittest.main(verbosity=2)

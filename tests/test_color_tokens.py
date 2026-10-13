"""No hard-coded colours outside `:root` and the theme blocks, and every `var()`
reference resolves.

PLAN item 5's "Done means" says *one token layer drives every surface and every
theme*. Two ways that quietly stop being true, both invisible in the theme you are
looking at:

* a literal copied from the default theme into a shared rule. It renders correctly
  in midnight and freezes midnight's colour in the other seven themes — which is how
  `#fff` ended up as the text on top of `var(--accent)` in `contrast` (white on
  yellow) and `mono` (white on white).
* a `var(--name)` nobody declared. `#snapNote.err` read `var(--danger, #e5534b)`;
  `--danger` does not exist, so the fallback won *every* time and the token layer
  was bypassed with the syntax of a token.

Allowed to be literal: the bare `:root`, every `[data-theme]` block (that is what a
theme *is*), and the eight `.sw-<theme>` swatch rules — a swatch must show its own
palette no matter which theme is active, so `var()` there would be wrong.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

COMMENT = re.compile(r"/\*.*?\*/", re.S)
TOKEN_REGION = re.compile(
    r'^[ \t]*(?::root[ \t]*(?:,[ \t]*\[data-theme="[\w-]+"\])?|\[data-theme="[\w-]+"\])[ \t]*\{')
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
FN = re.compile(r"\b(?:rgba?|hsla?)\([^)]*\)")
VAR = re.compile(r"var\(\s*(--[\w-]+)")
DECL = re.compile(r"(--[\w-]+)\s*:")
SWATCH = re.compile(r"^\s*\.sw-[\w-]+\s*\{")

# Colours the bare :root is allowed to hold. They are theme-independent *by intent*,
# and the list is pinned so a theme-dependent colour cannot sneak in without a test
# change — bare :root holding lengths only was the documented invariant.
BARE_ROOT_COLORS = {"--on-accent", "--shade"}


def blank_comments(text):
    out = list(text)
    for m in COMMENT.finditer(text):
        for i in range(m.start(), m.end()):
            if text[i] not in "\r\n":
                out[i] = " "
    return "".join(out)


def line_index(text):
    """Start offset of each line, for turning an offset into a line number."""
    starts, pos = [], 0
    for line in text.splitlines(True):
        starts.append(pos)
        pos += len(line)
    return starts


def token_spans(text):
    """Byte ranges of the bare :root and every [data-theme] block, flat blocks only."""
    lines = text.splitlines(True)
    starts, spans, pos = line_index(text), [], 0
    for i, line in enumerate(lines):
        if TOKEN_REGION.match(line):
            depth, j = 0, i
            while j < len(lines):
                depth += lines[j].count("{") - lines[j].count("}")
                j += 1
                if depth <= 0:
                    break
            spans.append((pos, pos + sum(len(x) for x in lines[i:j])))
        pos += len(line)
    return spans


def swatch_spans(text):
    lines = text.splitlines(True)
    spans, pos = [], 0
    for i, line in enumerate(lines):
        if SWATCH.match(line):
            end = line.index("}") + 1 if "}" in line else len(line)
            spans.append((pos, pos + end))
        pos += len(line)
    return spans


class TestColorLiterals(unittest.TestCase):
    def setUp(self):
        self.text = blank_comments(CSS)
        self.tokens = token_spans(self.text)
        self.swatches = swatch_spans(self.text)
        self.starts = line_index(self.text)

    def _outside(self):
        out = []
        for m in list(HEX.finditer(self.text)) + list(FN.finditer(self.text)):
            s = m.start()
            if any(a <= s < b for a, b in self.tokens):
                continue
            if "var(" in m.group(0):          # rgba(var(--warn-rgb), .12) is a token
                continue
            line = self.text[:s].count("\n") + 1
            in_swatch = any(a <= s < b for a, b in self.swatches)
            out.append((line, m.group(0), in_swatch))
        return out

    def test_no_color_literals_outside_token_regions(self):
        bad = [r for r in self._outside() if not r[2]]
        self.assertEqual(bad, [],
                         "hard-coded colours in shared rules (they freeze the default "
                         "theme's palette): " + ", ".join(f"L{l} {v}" for l, v, _ in bad))

    def test_the_only_literals_are_the_eight_swatch_previews(self):
        """The exception is pinned, not open-ended.

        A swatch previews a palette the reader is not currently wearing, so it cannot
        use var(). That is the one legitimate reason — and it is exactly eight rules.
        """
        kept = [r for r in self._outside() if r[2]]
        self.assertEqual(len(self.swatches), 8, "expected eight .sw-<theme> rules")
        self.assertEqual(len(kept), 16,
                         f"swatches hold {len(kept)} literals; a swatch is two stops")

    def test_bare_root_holds_only_the_named_theme_independent_colours(self):
        bare = re.search(r"^:root\s*\{(.*?)^\}", self.text, re.M | re.S).group(1)
        colors = {}
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", bare):
            if HEX.search(value) or FN.search(value):
                colors[name] = value.strip()
        self.assertEqual(set(colors), BARE_ROOT_COLORS,
                         f"bare :root colours drifted: {sorted(colors)}")

    def test_a_theme_may_still_override_them(self):
        """--on-accent is white today, which is wrong on a light accent. The fix is a
        per-theme override, so the cascade has to be able to carry one: bare :root
        first, theme blocks after, equal specificity.
        """
        self.assertLess(self.text.index(":root {"), self.text.index('[data-theme="midnight"]'))
        self.assertIn("--on-accent", self.text)


class TestVarReferences(unittest.TestCase):
    def setUp(self):
        self.text = blank_comments(CSS)

    def declared(self):
        names = set()
        for a, b in token_spans(self.text):
            names.update(DECL.findall(self.text[a:b]))
        return names

    def test_every_var_reference_is_declared(self):
        used = set(VAR.findall(self.text))
        # custom properties the script sets at runtime are not in the stylesheet
        js_set = set(re.findall(r'setProperty\(\s*[\'"](--[\w-]+)', JS))
        inline = set(re.findall(r'(--[\w-]+)\s*:', JS))
        missing = sorted(used - self.declared() - js_set - inline)
        self.assertEqual(missing, [],
                         "var() names with no declaration anywhere — a fallback in "
                         "var(--x, literal) hides this, and the literal then always "
                         f"wins: {missing}")

    def test_the_companion_rgb_triplets_are_still_used(self):
        """`rgba(var(--warn-rgb), .12)` is the shape that replaced four frozen tints."""
        for tok in ("--warn-rgb", "--err-rgb", "--accent-rgb"):
            self.assertIn(f"var({tok})", self.text, f"{tok} is declared but never read")

    def test_the_guards_can_fail(self):
        """Mutated COPY only. Both mutations are bugs that were actually in the file."""
        # a literal in a shared rule, outside every allowed region
        planted = self.text + "\n.orphan { color: #ff00ff; }\n"
        spans = token_spans(planted)
        hits = [m.group(0) for m in HEX.finditer(planted)
                if not any(a <= m.start() < b for a, b in spans)
                and not SWATCH.match(planted[planted.rindex("\n", 0, m.start()) + 1:])]
        self.assertIn("#ff00ff", hits, "a hard-coded colour slipped through")

        # var(--danger) again: referenced, never declared
        ghost = self.text.replace("color: var(--err);", "color: var(--danger);", 1)
        used = set(VAR.findall(ghost))
        declared = set()
        for a, b in token_spans(ghost):
            declared.update(DECL.findall(ghost[a:b]))
        self.assertIn("--danger", used - declared, "an undeclared token reference passed")


if __name__ == "__main__":
    unittest.main()

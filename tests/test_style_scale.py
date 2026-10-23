"""The type + spacing scale is a token layer, and a token layer is only real if a
theme cannot move it.

`static/style.css` used to carry 23 distinct raw `font-size` px values and 23
distinct spacing px values across ~500 rules; 11.8px, 12.8px, 15.5px, i.e. sizes
tuned by eye per rule rather than chosen from a scale. They are now `--fs-N` /
`--sp-N` tokens declared in the first `:root` block.

The second slice folded in the other two length properties: `margin` (18 distinct
values, mapped onto the SAME `--sp-N` steps rather than a parallel family; two
names for one 8px is two truths about it) and `border-radius` (`--rd-N` for the
values that had no token, reusing `--radius` 12px / `--radius-sm` 8px /
`--radius-pill` 999px where they already existed).

Two invariants, both cheap to break by accident:

  1. **No `[data-theme]` block may redefine a scale token.** The colour tokens are
     deliberately redefined per theme; the scale is not. If a theme ever sets
     `--fs-8`, every theme becomes a different layout and the Settings theme picker
     silently changes spacing.
  2. **Every `var(--fs-N)` / `var(--sp-N)` reference must resolve.** An undefined
     custom property makes the declaration invalid at computed-value time, so the
     property falls back to its inherited/initial value; the rule does not look
     broken, it just quietly loses its size.

The migration that created the tokens was 1:1 with the values already in use, so it
moved no pixels. Collapsing the long tail (9px, 11.8px, 12.8px, 22px, 26px …) onto a
shorter ladder DID move pixels and needed a browser pass; that collapse has now
happened (9 steps, ratio 1.125, floor 10px), and what is asserted here is the property
that made it worth doing -- see test_the_type_ladder_is_a_scale_not_a_continuum.
"""

import re
import unittest
from pathlib import Path

CSS_RAW = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text(
    encoding="utf-8"
)

# Strip comments BEFORE parsing. The banner at the top of the file says
# "5 themes via [data-theme] on <html>" in prose; without this, the first rule's
# "selector" is that comment plus `:root`, so a substring test for `data-theme`
# flags the scale's own home block as a theme. The failure looks exactly like a real
# violation and is not one; a CSS parser that reads comments as selectors is the bug.
CSS = re.sub(r"/\*.*?\*/", "", CSS_RAW, flags=re.S)

# The scale families. `--radius*` is included even though it predates this work:
# a theme that redefines `--radius` moves corners the same way one that redefines
# `--sp-9` moves gutters, and the invariant should not depend on which slice
# happened to add the token.
TOKEN = r"--(?:fs|sp|rd|tr)-\d+|--radius[\w-]*"

# Every flat `selector { declarations }` rule. Theme blocks are flat, so this
# reaches them; nested rules inside @media are reached too, which is what we want
# (a theme override could hide in a media query).
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)

# Length properties that went through the migration. Percentages and calc() stay
# literal: `border-radius: 50%` and `margin-left: calc(var(--sidebar-w) * -1)` are
# not lengths you would name on a scale. `border` widths are out too; a 1px
# hairline is not a spacing decision.
PX_DECL = re.compile(
    r"(?<![\w-])(font-size|gap|row-gap|column-gap|padding|padding-[\w-]+"
    r"|margin|margin-[\w-]+|border-radius|border-[\w-]+-radius)"
    r"\s*:\s*[^;{}]*?\d+(?:\.\d+)?px"
)


def rules(text=CSS):
    for sel, body in RULE.findall(text):
        yield " ".join(sel.split()), body


def scale_defs(body):
    return {n.strip() for n in re.findall(r"(" + TOKEN + r")\s*:", body)}


def ladder(prefix):
    """A scale family's declared values, in step order, as floats.

    Reads the numbers rather than counting the names, because the property that matters
    is how far apart the steps are, not how many there are.
    """
    out = []
    for sel, body in rules():
        if sel != ":root":
            continue
        for m in re.finditer(prefix + r"(\d+)\s*:\s*([\d.]+)px", body):
            out.append((int(m.group(1)), float(m.group(2))))
    return [v for _, v in sorted(out)]


class TestScaleIsDeclared(unittest.TestCase):
    def test_scale_tokens_exist(self):
        defined = set()
        for sel, body in rules():
            defined |= scale_defs(body)
        self.assertTrue(defined, "no --fs-*/--sp-* tokens defined at all")
        # A revert tripwire only. The property that matters is asserted by name below --
        # this used to be `>= 10`, which measured the artifact instead of the scale: it
        # would have failed a 9-step ladder at ratio 1.125 (a real scale) while passing
        # the 23-step ladder at 1.02 (not one).
        self.assertGreaterEqual(len([t for t in defined if t.startswith("--fs-")]), 8)
        self.assertGreaterEqual(len([t for t in defined if t.startswith("--sp-")]), 8)

    def test_the_type_ladder_is_a_scale_not_a_continuum(self):
        """Adjacent type steps must be far enough apart to read as different sizes.

        `operate.md` prescribes 1.125-1.2 for an Operate surface. The file used to carry
        23 steps whose neighbours differed by 1.016-1.06x -- half a pixel -- so 74% of all
        type sat in 11-13px across five steps nobody could tell apart. A step you cannot
        see is not a step; the count of them was never the point.
        """
        vals = ladder("--fs-")
        self.assertGreaterEqual(len(vals), 8, "type ladder looks collapsed or reverted")
        self.assertGreaterEqual(
            vals[0], 10, f"ladder floor is {vals[0]}px; nothing in this UI may be set in type "
            f"smaller than 10px -- it is not reading matter at any size below that",
        )
        ratios = [round(b / a, 4) for a, b in zip(vals, vals[1:])]
        thin = [(f"{v}px", r) for v, r in zip(vals[1:], ratios) if r < 1.10]
        self.assertFalse(thin, f"type steps too close to read as distinct: {thin}")
        self.assertLessEqual(max(ratios), 1.35, f"type ladder has a jump, not a step: {max(ratios)}")

    def test_scale_is_declared_only_in_the_bare_root_block(self):
        """`:root` and only `:root`, not a theme, not a component rule."""
        offenders = []
        for sel, body in rules():
            defs = scale_defs(body)
            if not defs:
                continue
            if sel == ":root":
                continue
            offenders.append(f"{sel} -> {sorted(defs)[:3]}")
        self.assertEqual(offenders, [],
                         "scale tokens declared outside bare :root: " + "; ".join(offenders))

    def test_themes_never_redefine_the_scale(self):
        """The test the task asks for: a theme block must not set any scale token.

        Colour tokens *are* allowed here, that is the point of a theme. Spacing and
        type are not: they are theme-independent by design.
        """
        for sel, body in rules():
            if "data-theme" not in sel:
                continue
            defs = scale_defs(body)
            self.assertFalse(
                defs,
                f'theme block "{sel}" redefines scale tokens {sorted(defs)}; '
                f"themes may recolour but never re-space",
            )

    def test_scale_block_is_before_the_theme_blocks(self):
        """A token used by a theme block must already be declared (source order)."""
        first_scale = first_theme = None
        for i, line in enumerate(CSS.splitlines()):
            if first_scale is None and re.match(r"\s*--(?:fs|sp)-\d+\s*:", line):
                first_scale = i
            if first_theme is None and "[data-theme=" in line:
                first_theme = i
        self.assertIsNotNone(first_scale)
        self.assertIsNotNone(first_theme)
        self.assertLess(first_scale, first_theme)


class TestScaleIsUsed(unittest.TestCase):
    def test_every_scale_reference_resolves(self):
        defined = set()
        for sel, body in rules():
            defined |= scale_defs(body)
        refs = {m.strip() for m in re.findall(r"var\(\s*(" + TOKEN + r")\s*\)", CSS)}
        self.assertEqual(sorted(refs - defined), [],
                         "var() references to undefined scale tokens (these silently "
                         "fall back to the initial value)")
        self.assertTrue(refs, "no scale references at all; migration reverted?")

    def test_no_raw_px_left_in_the_migrated_properties(self):
        """Every length property that went through the migration stays on the scale.

        Scope is font-size, gap, padding, margin and border-radius. What is NOT
        covered, and is not an oversight: `border` widths (a 1px hairline is not a
        spacing decision), percentages (`border-radius: 50%` on avatars), and
        `calc()` offsets like `margin-left: calc(var(--sidebar-w) * -1)`; none of
        those are lengths you would name on a scale.
        """
        # Skip the :root/theme region: the scale's own px values live there by
        # design. The last token declaration line ends that region; nothing after
        # it may hard-code a migrated property.
        last_decl = max(m.start() for m in re.finditer(r"^\s*--[\w-]+\s*:", CSS, re.M))
        rest = CSS[CSS.index("\n", last_decl) + 1:]

        bad = PX_DECL.findall(rest)
        self.assertEqual(bad, [], f"raw px still used for {bad[:5]}")


    def test_the_guards_can_fail(self):
        """Both guards are proven to react to a violation, on a mutated COPY.

        They were written against a file that already satisfies them, so "passes"
        alone is not evidence: a regex that silently matches nothing also passes.
        This plants one theme override and one raw length and requires both to be
        caught. The file on disk is never touched.
        """
        mutated = CSS + '\n[data-theme="midnight"] { --rd-1: 20px; }\n'
        caught = [
            sorted(scale_defs(body))
            for sel, body in rules(mutated)
            if "data-theme" in sel and scale_defs(body)
        ]
        self.assertTrue(caught, "a theme redefining --rd-1 went unnoticed")

        last = max(m.start() for m in re.finditer(r"^\s*--[\w-]+\s*:", mutated, re.M))
        rest = mutated[mutated.index("\n", last) + 1 :] + "\n.sidebar { margin: 7px; }\n"
        self.assertTrue(PX_DECL.findall(rest), "a raw `margin: 7px` went unnoticed")


if __name__ == "__main__":
    unittest.main()

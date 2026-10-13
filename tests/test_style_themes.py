"""Every theme must define the whole colour-token set, and mean what its name says.

`static/style.css` carries the themes as `[data-theme="…"]` blocks; `static/app.js`
carries the picker registry (`THEMES`); a `.sw-<id>` rule per theme paints the card
swatch. Three lists that have to agree, and nothing but this file made them agree —
a theme missing from `THEMES` is invisible, a theme missing from CSS falls back to
the default values one property at a time, and a half-overridden theme is the worst
outcome: dark text on a light background, or midnight's violet accent on black-on-
white chrome.

The contrast test is the one that earns its keep. "high-contrast" is a claim about
numbers, not a vibe, and `--muted` / `--fadetext` are exactly the tokens that quietly
drop below readability when someone picks a greyscale by eye.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

THEME = re.compile(r'\[data-theme="([\w-]+)"\]\s*\{([^}]*)\}')
DECL = re.compile(r"^\s*(--[\w-]+|color-scheme)\s*:\s*(.+?)[ \t]*;[ \t]*$", re.M)
REGISTRY = re.compile(r"""\{\s*id:\s*'([\w-]+)'""")
SWATCH = re.compile(r"^\.sw-([\w-]+)\s*\{", re.M)
SCALE_NAME = re.compile(r"--(?:fs|sp|rd)-\d+$|^--radius[\w-]*$")

# The pairs that must clear WCAG AA. `--text` on `--bg` is the body copy; `--muted`
# is secondary copy at a smaller size, held to 4.5 as well because it is used for
# real labels (hints, timestamps), not decoration.
MIN_TEXT = 4.5
MIN_MUTED = 4.5
# The themes whose entire name is a contrast promise get the AAA bar. Both halves
# of the accessibility pair — `contrast` (white on black) and `contrast-light`
# (black on white) — because a light high-contrast theme is the one that usually
# fails: dark-on-light accents picked from a dark palette land at 2:1 on white.
AAA_THEMES = ("contrast", "contrast-light")
MIN_AAA = 7.0


def themes():
    """{id: {token: value}} for every `[data-theme]` block."""
    out = {}
    for ident, body in THEME.findall(CSS):
        out[ident] = {n: v for n, v in DECL.findall(body)}
    return out


def _srgb(h):
    c = int(h, 16) / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(color):
    """Relative luminance of a #rgb/#rrggbb hex, or None for anything else."""
    s = (color or "").strip().lower()
    if not s.startswith("#"):
        return None
    h = s[1:]
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6 or any(c not in "0123456789abcdef" for c in h):
        return None
    return (0.2126 * _srgb(h[0:2]) + 0.7152 * _srgb(h[2:4]) + 0.0722 * _srgb(h[4:6]))


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    if la is None or lb is None:
        return None
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def rgb_triplet(value):
    return [int(x.strip()) for x in value.split(",")]


# ── item 5's open accessibility debt, measured instead of remembered ───────────
# `--on-accent` is the foreground of six controls that sit on `--accent`: Send,
# New chat, the picked option, the primary button, the assistant avatar, the
# active segment. It is white in bare `:root` and no theme overrides it, so on a
# light accent it is white on near-white. That is a palette decision (deviation 1)
# and this file does not make it — but the debt was carried with no number
# attached, and an unnumbered debt is how `contrast` ended up at 1.41:1 on *the
# accessibility theme* while every other contrast in the file was checked.
#
# So: the failing set is pinned. A 9th theme with a light accent cannot join it
# quietly, an offender cannot deepen quietly, and when the palette call is made
# the list has to be emptied — which is the decision being recorded, not lost.
MIN_ON_ACCENT = 4.5
ON_ACCENT_DEBT = {          # theme -> ratio measured 2026-10-13, white on --accent
    "contrast": 1.41,
    "ember": 2.56,
    "midnight": 3.99,
    "ocean": 2.59,
    "vapor": 2.94,
}
# `mono` was 1.12:1 — white text on a #f2f2f2 accent, i.e. white on white. Fixed
# 2026-07-29 by the palette call (PLAN item 5, deviation 1: "just Mono"), which is
# `--on-accent: #000` in the mono block. Removed from the dict above because a fixed
# theme left listed is exactly what test_the_failing_set_is_exactly_the_recorded_debt
# is built to catch; the remaining five were reviewed and judged acceptable as-is.
# A hue may be re-picked for any reason; drifting *below* the recorded ratio is
# the only direction that makes the app worse.
ON_ACCENT_SLACK = 0.02
ON_ACCENT_CONSUMERS = 6

RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
BG = re.compile(r"\bbackground(?:-color)?\s*:\s*([^;]+?)\s*;")


def strip_comments(text):
    """`/* … */` prose mentions tokens by name; a scan that reads it matches
    declarations that do not exist. Blank it first — see the note in memory."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def on_accent_rules(text):
    """[(selector, [background values])] for every rule painting --on-accent."""
    return [(" ".join(sel.split()), [v.strip() for v in BG.findall(body)])
            for sel, body in RULE.findall(strip_comments(text))
            if "var(--on-accent)" in body]


def on_accent_ratios(text):
    """{theme: (ratio, foreground, background)} — theme override, else bare :root."""
    stripped = strip_comments(text)
    root = {}
    for sel, body in RULE.findall(stripped):
        if " ".join(sel.split()) == ":root":
            root.update(DECL.findall(body))
    out = {}
    for ident, body in THEME.findall(stripped):
        tok = dict(DECL.findall(body))
        fg, bg = tok.get("--on-accent") or root.get("--on-accent"), tok.get("--accent")
        out[ident] = (contrast(fg, bg), fg, bg)
    return out


class TestThemeSets(unittest.TestCase):
    def test_every_theme_declares_the_full_token_set(self):
        """No theme may override some tokens and inherit the rest.

        Comparison is against `midnight`, which is the default — and it is compared
        as a SET, not a fixed list, so the test does not fossilise today's 26 tokens:
        adding a token means adding it to every theme, which is the point.
        """
        t = themes()
        self.assertIn("midnight", t, "the default theme block is gone")
        base = set(t["midnight"])
        self.assertTrue(base, "no tokens parsed — the guard is reading nothing")
        for ident in sorted(t):
            declared = set(t[ident])
            self.assertEqual(
                declared, base,
                f"[{ident}] differs from the default set — "
                f"missing {sorted(base - declared)}, "
                f"extra {sorted(declared - base)}")

    def test_no_theme_redefines_the_scale(self):
        """Theme blocks are colour only.

        `test_style_scale` already fences this off from its side; this is the same
        invariant stated from the theme's side, so a violation names the theme that
        did it rather than the token.
        """
        for ident, tokens in sorted(themes().items()):
            moved = sorted(k for k in tokens if SCALE_NAME.match(k))
            self.assertEqual(moved, [], f"[{ident}] redefines scale tokens: {moved}")

    def test_color_scheme_matches_the_background(self):
        """`color-scheme` decides native widget colours (scrollbars, form controls).

        A light theme declaring `dark` gets dark-native widgets on a light page —
        the bug is invisible in the app's own chrome and obvious the moment a
        `<select>` or a textarea appears.
        """
        t = themes()
        for ident, tokens in sorted(t.items()):
            self.assertIn(tokens.get("color-scheme"), ("dark", "light"),
                          f"[{ident}] color-scheme")
            bg = luminance(tokens["--bg"])
            want = "light" if bg > 0.5 else "dark"
            self.assertEqual(tokens["color-scheme"], want,
                             f"[{ident}] is {'light' if want == 'light' else 'dark'} "
                             f"by background luminance ({bg:.3f}) but declares "
                             f"{tokens['color-scheme']}")

    def test_body_and_secondary_text_clear_wcag_aa(self):
        """--text/--bg and --muted/--bg must reach 4.5:1 in every theme."""
        for ident, tokens in sorted(themes().items()):
            for tok, floor in (("--text", MIN_TEXT), ("--muted", MIN_MUTED)):
                got = contrast(tokens[tok], tokens["--bg"])
                self.assertIsNotNone(got, f"[{ident}] {tok} is not a hex")
                self.assertGreaterEqual(
                    got, floor,
                    f"[{ident}] {tok} on --bg is {got:.2f}:1, below {floor}:1")

    def test_the_high_contrast_theme_is_actually_high_contrast(self):
        """The AAA bar, on the theme that promises it."""
        all_themes = themes()
        for ident in AAA_THEMES:
            self.assertIn(ident, all_themes, f"theme {ident} no longer exists")
            tokens = all_themes[ident]
            for tok in ("--text", "--muted", "--fadetext", "--user-bubble-text"):
                got = contrast(tokens[tok], tokens["--bg"])
                self.assertGreaterEqual(
                    got, MIN_AAA,
                    f"[{ident}] {tok} on --bg is {got:.2f}:1 — this theme's whole "
                    f"purpose is {MIN_AAA}:1")

    def test_the_rgb_companions_match_their_hex(self):
        """`--accent-rgb` must be `--accent`, same for accent2, err and warn.

        The `-rgb` forms feed `rgba(var(--accent-rgb), .12)` washes. When they drift
        the soft background of a chip is a different hue from the chip's own text —
        invisible in a screenshot of one component, obvious across the app.

        `--warn-rgb` did not exist until the colour sweep. `--accent`, `--accent-2`
        and `--err` all had companions and `--warn` alone did not, so every warn wash
        had to be a hard-coded `rgba(232, 163, 61, .12)` — midnight's amber frozen
        into the other seven themes. This list of pairs was pinned to the three that
        existed, so nothing reported the missing fourth: a completeness check written
        against the tokens someone happened to use is not a completeness check.
        """
        pairs = (("--accent", "--accent-rgb"),
                 ("--accent-2", "--accent2-rgb"),
                 ("--err", "--err-rgb"),
                 ("--warn", "--warn-rgb"))
        for ident, tokens in sorted(themes().items()):
            for hexname, rgbname in pairs:
                hexv, rgbv = tokens[hexname], tokens[rgbname]
                h = hexv.strip().lstrip("#").lower()
                if len(h) == 3:
                    h = "".join(c * 2 for c in h)
                self.assertEqual(len(h), 6, f"[{ident}] {hexname} is not a hex colour")
                want = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
                self.assertEqual(rgb_triplet(rgbv), want,
                                 f"[{ident}] {rgbname} = {rgbv} but {hexname} = "
                                 f"{hexv} ({', '.join(map(str, want))})")

    def test_only_shadow_may_carry_a_px_literal(self):
        """Theme blocks are colour tokens; `--shadow` is the one sanctioned length.

        Everything else that looks like a length in a theme block means someone put
        a spacing or type decision in a palette — which is the exact thing that makes
        a theme move the layout.
        """
        for ident, body in THEME.findall(CSS):
            for line in body.splitlines():
                if "px" not in line:
                    continue
                name = line.split(":")[0].strip()
                self.assertEqual(name, "--shadow",
                                 f"[{ident}] has a px literal in {name!r}: {line.strip()}")

    def test_registry_css_and_swatch_names_agree(self):
        """app.js THEMES ids == CSS theme blocks == .sw-<id> swatch rules.

        A theme in CSS but not in THEMES cannot be picked; one in THEMES but not in
        CSS renders a card that does nothing; one without a swatch renders a card
        with an empty dot.
        """
        css_ids = set(themes())
        reg_ids = set(REGISTRY.findall(APP))
        sw_ids = set(SWATCH.findall(CSS))
        self.assertEqual(css_ids, reg_ids,
                         f"CSS-only {sorted(css_ids - reg_ids)}, "
                         f"registry-only {sorted(reg_ids - css_ids)}")
        self.assertEqual(css_ids, sw_ids,
                         f"CSS-only (no swatch) {sorted(css_ids - sw_ids)}, "
                         f"swatch-only {sorted(sw_ids - css_ids)}")
        self.assertGreaterEqual(len(css_ids), 8,
                                "the plan asks for 8 themes; the count went down")

    def test_the_guards_can_fail(self):
        """Prove the three interesting guards react, on mutated COPY only."""
        # a theme that forgets --text
        missing = CSS.replace('  --text: #f2ebe1;\n', '', 1)
        t = {i: {n for n, _ in DECL.findall(b)} for i, b in THEME.findall(missing)}
        self.assertIn("ember", t)
        self.assertNotEqual(t["ember"], t["midnight"],
                            "a theme missing --text looked complete")

        # a theme that re-spaces instead of re-colouring
        spaced = CSS + '\n[data-theme="mono"] {\n  --sp-9: 3px;\n}\n'
        moved = [i for i, b in THEME.findall(spaced)
                 if any(SCALE_NAME.match(n) for n, _ in DECL.findall(b))]
        self.assertIn("mono", moved, "a theme redefining --sp-9 went unnoticed")

        # a registry entry with no block behind it
        ids = set(themes())
        self.assertEqual(set(REGISTRY.findall(APP)), ids,
                         "registry and CSS already disagree")
        planted = APP + "\n  { id: 'paper', name: 'Paper', hint: 'x' },\n"
        self.assertNotEqual(set(REGISTRY.findall(planted)), ids,
                            "a new registry id would not show up as drift")


class TestOnAccentContrast(unittest.TestCase):
    """The one accessibility failure item 5 has not decided, pinned by number.

    These tests deliberately do not fix it: five themes' button text changing
    colour is a palette call. What they do is make the call against today's
    measurements and stop the hole widening meanwhile.
    """

    def ratios(self):
        return on_accent_ratios(CSS)

    def test_every_theme_resolves_a_foreground_and_an_accent(self):
        r = self.ratios()
        self.assertEqual(len(r), 8, "expected 8 theme blocks")
        for ident, (got, fg, bg) in sorted(r.items()):
            self.assertIsNotNone(got, f"[{ident}] --on-accent={fg!r} or --accent={bg!r} "
                                      "is not a hex, so the pairing cannot be measured")

    def test_the_failing_set_is_exactly_the_recorded_debt(self):
        r = self.ratios()
        failing = {i for i, (got, _, _) in r.items() if got < MIN_ON_ACCENT}
        self.assertEqual(
            failing, set(ON_ACCENT_DEBT),
            f"--on-accent on --accent below {MIN_ON_ACCENT}:1 drifted: "
            f"new offenders {sorted(failing - set(ON_ACCENT_DEBT))}, "
            f"fixed but still listed {sorted(set(ON_ACCENT_DEBT) - failing)} — "
            "a fixed theme must be removed from ON_ACCENT_DEBT, that is the record "
            "catching up with the palette decision")
        passing = sorted(set(r) - failing)
        self.assertEqual(passing, ["contrast-light", "daybreak", "mono"],
                         "the themes that clear AA changed; update the note in "
                         "PLAN item 5's deviation 1 (mono joined them on 2026-07-29)")

    def test_no_offender_may_deepen(self):
        for ident, floor in sorted(ON_ACCENT_DEBT.items()):
            got = self.ratios()[ident][0]
            self.assertGreaterEqual(
                got, floor - ON_ACCENT_SLACK,
                f"[{ident}] --on-accent on --accent fell to {got:.2f}:1, below the "
                f"recorded {floor:.2f}:1 — the debt got worse, not smaller")

    def test_every_consumer_sits_on_the_accent_it_was_measured_against(self):
        """The measurement is only meaningful while `--on-accent` means "on --accent".

        A 7th control painted white on `--err`, or on a literal, would be a new
        unreadable surface that no contrast test here would notice.
        """
        rules = on_accent_rules(CSS)
        self.assertEqual(len(rules), ON_ACCENT_CONSUMERS,
                         f"--on-accent is consumed by {len(rules)} rules, not "
                         f"{ON_ACCENT_CONSUMERS}: {[s for s, _ in rules]}")
        for sel, bgs in rules:
            self.assertEqual(bgs, ["var(--accent)"],
                             f"{sel} paints --on-accent on {bgs}, not on --accent")

    def test_the_guards_can_fail(self):
        """Teeth, on mutated COPY only."""
        # a 9th theme with a light accent: white on white, and no guard but this one
        pale = CSS + '\n[data-theme="paper"] {\n  --accent: #f2f2f2;\n}\n'
        r = on_accent_ratios(pale)
        self.assertIn("paper", r)
        self.assertLess(r["paper"][0], MIN_ON_ACCENT,
                        "a light-accent theme slipped under the failing-set check")

        # a fixed theme left in the list would also have to be caught. Planting on
        # `contrast`, not `mono`: mono is fixed now, so overriding it proves nothing —
        # this must show the mechanism works on a theme that is *still* in the debt.
        fixed = CSS.replace('--accent-rgb: 255, 214, 10;\n  --on-accent: #fff;',
                            '--accent-rgb: 255, 214, 10;\n  --on-accent: #000;', 1)
        self.assertGreaterEqual(on_accent_ratios(fixed)["contrast"][0], MIN_ON_ACCENT,
                                "a per-theme override did not take effect, so the "
                                "list could never be emptied")

        # an offender made worse — white on a *lighter* accent than the recorded one
        worse = CSS.replace("--accent: #ffd60a;", "--accent: #fff08c;", 1)
        self.assertLess(on_accent_ratios(worse)["contrast"][0], ON_ACCENT_DEBT["contrast"],
                        "contrast's accent got lighter and nothing noticed")

        # a consumer painted on the wrong ground
        mispainted = CSS.replace(".send-btn {", ".send-btn {\n  color: var(--on-accent);\n"
                                 "  background: var(--err);", 1)
        self.assertTrue(any("--err" in b for _, bs in on_accent_rules(mispainted)
                            for b in bs),
                        "a rule putting --on-accent on --err went unnoticed")


if __name__ == "__main__":
    unittest.main()

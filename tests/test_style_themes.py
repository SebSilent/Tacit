"""Every theme must define the whole colour-token set, and mean what its name says.

`static/style.css` carries the themes as `[data-theme="…"]` blocks; `static/app.js`
carries the picker registry (`THEMES`); a `.sw-<id>` rule per theme paints the card
swatch. Three lists that have to agree, and nothing but this file made them agree:
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
SCALE_NAME = re.compile(r"--(?:fs|sp|rd|tr)-\d+$|^--radius[\w-]*$")

# The pairs that must clear WCAG AA. `--text` on `--bg` is the body copy; `--muted`
# is secondary copy at a smaller size, held to 4.5 as well because it is used for
# real labels (hints, timestamps), not decoration.
MIN_TEXT = 4.5
MIN_MUTED = 4.5
# The themes whose entire name is a contrast promise get the AAA bar. Both halves
# of the accessibility pair, `contrast` (white on black) and `contrast-light`
# (black on white), because a light high-contrast theme is the one that usually
# fails: dark-on-light accents picked from a dark palette land at 2:1 on white.
AAA_THEMES = ("contrast", "contrast-light")
MIN_AAA = 7.0

# The theme the bare `:root` is fused with, and therefore the one every other theme's
# token SET is compared against. It was `midnight` until the 2026-10-13 appearance
# revamp replaced the eight palettes; the guards below are written against this name
# rather than a literal so the next world change moves one line, not six.
DEFAULT_THEME = "graphite"

# How many worlds ship. Pinned as a number rather than "at least eight" because
# the swatch-literal guard in test_color_tokens has to know it too: a swatch is two
# stops, so ten themes is twenty literals and not one more. The count moved 8 -> 10
# when amethyst and celadon were added -- the purple and the cyan the palette had
# been steering around, composed rather than avoided -- and 20 -> 22 when kinpaku and
# kinpaku-dark were added, the gold-leaf pair: paper ground with the metal dark enough
# to read as text, lacquer ground with the metal bright enough to need black on it.
THEME_COUNT = 22


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


# ── `--on-accent`, measured instead of remembered ──────────────────────────────
# `--on-accent` is the foreground of six controls that sit on `--accent`: Send,
# New chat, the picked option, the primary button, the assistant avatar, the
# active segment. It was white in bare `:root` with no theme overriding it, so on
# a light accent it was white on near-white; `contrast` sat at 1.41:1 on *the
# accessibility theme*, and the debt was carried with no number attached.
#
# Every palette re-measures this pair rather than inheriting an answer. Thirteen of
# the twenty-two declare `--on-accent: #000` in their own block, because their accents
# are light enough that white on them falls under AA; the other nine keep the white
# from bare `:root`, their accents being dark enough to carry it.
#
# Which side a theme falls on is a property of its accent's luminance and not of its
# name, so the split is deliberately NOT enumerated here. It was, at eight themes,
# and it went stale the moment the set grew; a list of names in a comment is a
# second source of truth about a number the test already measures.
# `test_every_theme_clears_aa_now_that_the_debt_is_paid` is what checks it.
#
# The debt list stays as a list rather than being deleted, because the guard below
# compares against it: a theme with a light accent and no override re-enters this
# dict and has to be argued for explicitly.
#
# Darkening `--accent` instead stays rejected, for the reason it always was:
# `--accent` is also text on `--bg`, so a darker accent trades a button failure
# for a link failure. Both pairs are measured in the palette lab, not one.
MIN_ON_ACCENT = 4.5
ON_ACCENT_DEBT: dict = {}   # paid in full; see the note above before adding a row
# A hue may be re-picked for any reason; drifting *below* the recorded ratio is
# the only direction that makes the app worse.
ON_ACCENT_SLACK = 0.02
# Six controls sit on `--accent`: Send, New chat, the picked option, the primary
# button, the assistant avatar, the active segment. The appearance revamp added a
# seventh -- `::selection`, which used to wash the accent at .35 over whatever
# foreground was already there, so on a light accent it selected near-white text
# onto a near-white ground. It now paints the accent solid with `--on-accent` on
# it, which is the same measured pair as the other six rather than a new one.
ON_ACCENT_CONSUMERS = 7

RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
BG = re.compile(r"\bbackground(?:-color)?\s*:\s*([^;]+?)\s*;")


def strip_comments(text):
    """`/* … */` prose mentions tokens by name; a scan that reads it matches
    declarations that do not exist. Blank it first; see the note in memory."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def on_accent_rules(text):
    """[(selector, [background values])] for every rule painting --on-accent."""
    return [(" ".join(sel.split()), [v.strip() for v in BG.findall(body)])
            for sel, body in RULE.findall(strip_comments(text))
            if "var(--on-accent)" in body]


def on_accent_ratios(text):
    """{theme: (ratio, foreground, background)}; theme override, else bare :root."""
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

        Comparison is against `midnight`, which is the default, and it is compared
        as a SET, not a fixed list, so the test does not fossilise today's 26 tokens:
        adding a token means adding it to every theme, which is the point.
        """
        t = themes()
        self.assertIn(DEFAULT_THEME, t, "the default theme block is gone")
        base = set(t[DEFAULT_THEME])
        self.assertTrue(base, "no tokens parsed; the guard is reading nothing")
        for ident in sorted(t):
            declared = set(t[ident])
            self.assertEqual(
                declared, base,
                f"[{ident}] differs from the default set: "
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

        A light theme declaring `dark` gets dark-native widgets on a light page:
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
                    f"[{ident}] {tok} on --bg is {got:.2f}:1, this theme's whole "
                    f"purpose is {MIN_AAA}:1")

    def test_the_rgb_companions_match_their_hex(self):
        """`--accent-rgb` must be `--accent`, same for accent2, err and warn.

        The `-rgb` forms feed `rgba(var(--accent-rgb), .12)` washes. When they drift
        the soft background of a chip is a different hue from the chip's own text:
        invisible in a screenshot of one component, obvious across the app.

        `--warn-rgb` did not exist until the colour sweep. `--accent`, `--accent-2`
        and `--err` all had companions and `--warn` alone did not, so every warn wash
        had to be a hard-coded `rgba(232, 163, 61, .12)`; midnight's amber frozen
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
        a spacing or type decision in a palette, which is the exact thing that makes
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
        self.assertGreaterEqual(len(css_ids), THEME_COUNT,
                                "the theme count went down from %d" % THEME_COUNT)

    def test_the_guards_can_fail(self):
        """Prove the three interesting guards react, on mutated COPY only."""
        # a theme that forgets --text
        missing = CSS.replace('  --text: #eee6e3;\n', '', 1)
        t = {i: {n for n, _ in DECL.findall(b)} for i, b in THEME.findall(missing)}
        self.assertIn("oxide", t)
        self.assertNotEqual(t["oxide"], t[DEFAULT_THEME],
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
        self.assertEqual(len(r), THEME_COUNT, "expected %d theme blocks" % THEME_COUNT)
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
            f"fixed but still listed {sorted(set(ON_ACCENT_DEBT) - failing)}; "
            "a fixed theme must be removed from ON_ACCENT_DEBT, that is the record "
            "catching up with the palette decision")
        passing = sorted(set(r) - failing)
        self.assertEqual(passing, sorted(r),
                         "a theme fell below AA; every one of the eight clears it "
                         "since the 2026-10-13 palette call, so a new offender means "
                         "a regression or a new theme that owes an --on-accent row")

    def test_every_theme_clears_aa_now_that_the_debt_is_paid(self):
        """The floor, applied to all eight instead of to a list of offenders.

        While the debt was open this test walked `ON_ACCENT_DEBT` and compared each
        theme against its own recorded ratio, which let five themes stay broken as
        long as they did not get worse. The debt is empty now, so that loop would
        iterate over nothing and report success; a guard that passes because it
        measures nothing. This is the same check with the exception list removed:
        every theme must clear AA against the accent it actually paints.
        """
        for ident, (got, fg, bg) in sorted(self.ratios().items()):
            self.assertIsNotNone(got, f"[{ident}] {fg} on {bg} is not measurable")
            self.assertGreaterEqual(
                got, MIN_ON_ACCENT,
                f"[{ident}] --on-accent ({fg}) on --accent ({bg}) is {got:.2f}:1, "
                f"under the {MIN_ON_ACCENT}:1 AA floor; either the accent moved or "
                "the override was deleted. If it is deliberate, add the theme to "
                "ON_ACCENT_DEBT with the measured ratio and say why.")

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

        # a paid theme re-broken. The old mutation planted `--on-accent: #fff` inside
        # the contrast block, which no longer exists, so it replaced nothing and
        # passed on a no-op; the quiet failure mode of every string-anchored
        # mutation in this file. Assert the anchor moved first, then the ratio.
        anchor = '--on-accent: #000;\n  --accent-2: #4ad9ff;'
        regressed = CSS.replace(anchor, '--on-accent: #fff;\n  --accent-2: #4ad9ff;', 1)
        self.assertNotEqual(regressed, CSS,
                            "the contrast anchor moved, so this mutation proves nothing")
        self.assertLess(on_accent_ratios(regressed)["contrast"][0], MIN_ON_ACCENT,
                        "white on contrast's yellow came back and nothing noticed")

        # a declaration the token-set parser cannot read. `themes()` scans the RAW
        # file and `DECL` needs the line to end with `;`, so a trailing comment drops
        # `--on-accent` from that theme's declared set; the exact trap the 2026-10-13
        # fix walked into, where three overrides were in the file, rendered correctly,
        # and were invisible to `test_every_theme_declares_the_full_token_set`.
        #
        # `on_accent_ratios` strips comments before scanning, so it still reads #000
        # here. The two parsers disagree, and the raw one is the stricter: pinning the
        # stricter one is what keeps a commented declaration from passing quietly.
        commented = CSS.replace(anchor,
                                '--on-accent: #000;  /* white was 1.41:1 */\n'
                                '  --accent-2: #4ad9ff;', 1)
        self.assertNotEqual(commented, CSS, "the contrast anchor moved")
        declared = {i: {n for n, _ in DECL.findall(b)} for i, b in THEME.findall(commented)}
        self.assertNotIn("--on-accent", declared["contrast"],
                         "a commented-out declaration still parsed, so the token-set "
                         "test is not the guard this note claims it is")
        self.assertIn("--on-accent", declared[DEFAULT_THEME],
                         "the anchor theme lost its own row, so the comparison below "
                         "has nothing to compare against")

        # a consumer painted on the wrong ground
        mispainted = CSS.replace(".send-btn {", ".send-btn {\n  color: var(--on-accent);\n"
                                 "  background: var(--err);", 1)
        self.assertTrue(any("--err" in b for _, bs in on_accent_rules(mispainted)
                            for b in bs),
                        "a rule putting --on-accent on --err went unnoticed")


class TestThemeGrouping(unittest.TestCase):
    """The Appearance panel groups its twenty-two cards by tone. That only works while
    `tone` is a fact about the theme rather than an opinion about it, so it is read
    back out of the theme block's own `color-scheme` -- the property the browser uses
    to pick form-control and scrollbar colours. A hand-written label that drifts from
    it would make the picker confidently wrong, which is worse than not labelling."""

    ENTRY = re.compile(r"\{ id: '([\w-]+)',.*?tone: '(\w+)'", re.S)

    def registry(self):
        block = APP[APP.index("const THEMES = ["):]
        return self.ENTRY.findall(block[:block.index("\n];")])

    def test_tone_is_the_blocks_own_color_scheme(self):
        th = themes()
        got = self.registry()
        self.assertEqual(len(got), THEME_COUNT, "the registry did not parse")
        for tid, tone in got:
            self.assertIn(tone, ("dark", "light"), f"{tid}: tone {tone!r}")
            self.assertEqual(th[tid].get("color-scheme"), tone,
                             f"{tid}: picker says {tone!r}, the stylesheet says "
                             f"{th[tid].get('color-scheme')!r}")

    def test_the_groups_are_contiguous_and_dark_first(self):
        tones = [t for _, t in self.registry()]
        self.assertEqual(tones, sorted(tones, key=["dark", "light"].index),
                         "the two groups interleave -- the panel renders one list "
                         "in the registry's order, so a scattered registry is a "
                         "scattered picker")
        self.assertEqual(tones.count("dark") + tones.count("light"), len(tones))

    def test_css_blocks_follow_the_registry(self):
        """Order is irrelevant to the cascade here (the selectors are mutually
        exclusive) and entirely relevant to a reader comparing the two files."""
        self.assertEqual(list(themes()), [i for i, _ in self.registry()],
                         "style.css and app.js list the themes in different orders")

    def test_the_panel_renders_a_group_per_tone(self):
        HARNESS = (ROOT / "static" / "harness.js").read_text(encoding="utf-8")
        for frag in ("theme-group", "theme-group-label", "theme-group-count",
                     "t.tone"):
            self.assertIn(frag, HARNESS, f"harness.js no longer references {frag}")
        for sel in (".theme-group", ".theme-group-label", ".theme-group-count"):
            self.assertIn(sel + " ", CSS, f"{sel} is referenced but never styled")

    def test_the_guards_can_fail(self):
        reg = self.registry()
        self.assertTrue(reg, "premise: the registry parses")
        # a light theme mislabelled dark is the exact drift this exists to catch, and
        # the comparison only works because the two answers are distinguishable
        th = themes()
        tid, tone = reg[0]
        flipped = "light" if tone == "dark" else "dark"
        self.assertNotEqual(th[tid].get("color-scheme"), flipped,
                            "premise: tone-vs-color-scheme can actually disagree")
        # and the contiguity guard has something to bite on
        mixed = ["dark", "light", "dark"]
        self.assertNotEqual(mixed, sorted(mixed, key=["dark", "light"].index))


if __name__ == "__main__":
    unittest.main()

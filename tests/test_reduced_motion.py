"""`prefers-reduced-motion` must switch everything off, in one place.

the deleted plan item 5: "Respect `prefers-reduced-motion`: every animation has an off state."
The way that fails is the per-selector `@media` block; someone adds a pulse,
forgets the off switch, and nothing notices. `static/style.css` had exactly that
bug: the session dot's pulse was covered and `tokPulse`, `dgPulse` and every
width/opacity/transform transition were not.

So the file carries ONE blanket rule, and these tests keep it that way:

* the blanket exists, is universal, and `!important`-locks the durations;
* it is the only `prefers-reduced-motion` block (the per-selector form must not
  come back alongside it);
* it sits after every animated declaration, so the cascade reads in one direction;
* every `animation` name resolves to a `@keyframes` that exists, and every
  `@keyframes` is used; a typo'd name animates nothing and looks like a working
  pulse that simply never pulses;
* no transition runs longer than 600ms (a tripwire against slow-motion chrome).
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

COMMENT = re.compile(r"/\*.*?\*/", re.S)
BLANKET = re.compile(r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)")
KEYFRAMES = re.compile(r"@keyframes\s+([\w-]+)")
ANIM = re.compile(r"(?:^|[;{\s])animation(?:-name)?\s*:\s*([^;{}]+)", re.M)
TRANS = re.compile(r"(?:^|[;{\s])transition\s*:\s*([^;{}]+)", re.M)
PROP = re.compile(r"(?:^|[;{\s])(animation-duration|animation-iteration-count|"
                  r"transition-duration)\s*:\s*([^;{}]+)", re.M)

# Words that can legally sit in an `animation` shorthand but are not a name.
ANIM_WORDS = {
    "ease", "ease-in", "ease-out", "ease-in-out", "linear", "step-start",
    "step-end", "infinite", "none", "forwards", "backwards", "both",
    "alternate", "alternate-reverse", "normal", "reverse", "running", "paused",
    "initial", "inherit", "unset",
}

MAX_TRANSITION = 0.6  # seconds


def stripped(css=CSS):
    """The stylesheet with comments removed; comments quote the very strings
    these tests count, so leaving them in would make every count wrong."""
    return COMMENT.sub("", css)


def block_body(css, open_idx):
    """Text between the braces at `open_idx`, matched by depth."""
    depth = 0
    for i in range(open_idx, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[open_idx + 1:i]
    raise AssertionError("unbalanced braces from " + repr(css[open_idx:open_idx + 40]))


def blanket(css):
    m = BLANKET.search(css)
    if not m:
        return None, None, None
    brace = css.index("{", m.end())
    return m.start(), block_body(css, brace), brace


def animation_names(css):
    out = []
    for value in ANIM.findall(css):
        for token in value.replace(",", " ").split():
            t = token.strip().rstrip(";").lower()
            if re.fullmatch(r"[a-z_][\w-]*", t) and t not in ANIM_WORDS:
                out.append(t)
    return out


DUR_TOKEN = re.compile(r"(--dur-[\w-]+)\s*:\s*([\d.]+)(ms|s)\s*;")
BARE_ROOT = re.compile(r"^:root\s*\{(.*?)^\}", re.M | re.S)
VAR_DUR = re.compile(r"^var\(\s*(--dur-[\w-]+)\s*\)$")


def duration_tokens(css):
    """{--dur-N: seconds} declared in the bare `:root`."""
    m = BARE_ROOT.search(css)
    if not m:
        return {}
    return {n: (float(v) / 1000 if u == "ms" else float(v))
            for n, v, u in DUR_TOKEN.findall(m.group(1))}


def durations(css):
    """Every duration declared in a `transition` shorthand, in seconds.

    Resolves `var(--dur-N)` against the bare `:root` rather than skipping it. The
    motion grammar moved onto a duration token ladder, and a guard that only reads
    literal `150ms` would have measured nothing at all after that move while still
    reporting success; the quiet failure this suite names elsewhere. An unresolvable
    token is raised as a duration far over the ceiling so the caller names it.
    """
    toks = duration_tokens(css)
    out = []
    for value in TRANS.findall(css):
        for token in value.split():
            m = re.fullmatch(r"(\d*\.?\d+)(ms|s)", token)
            if m:
                n = float(m.group(1))
                out.append(n / 1000 if m.group(2) == "ms" else n)
                continue
            v = VAR_DUR.match(token)
            if v:
                out.append(toks.get(v.group(1), MAX_TRANSITION * 100))
    return out


class TestReducedMotion(unittest.TestCase):
    def setUp(self):
        self.css = stripped()

    def test_one_blanket_rule_switches_motion_off(self):
        start, body, _ = blanket(self.css)
        self.assertIsNotNone(body, "no prefers-reduced-motion block in style.css")
        self.assertIn("*", body, "the off switch is not universal")
        self.assertIn("::before", body, "pseudo-element motion is not covered")
        self.assertIn("::after", body, "pseudo-element motion is not covered")
        props = {p for p, _ in PROP.findall(body)}
        self.assertEqual(
            props, {"animation-duration", "animation-iteration-count", "transition-duration"},
            "the blanket must pin these three; without iteration-count an "
            "`infinite` pulse keeps pulsing at .01ms per lap")
        for prop, value in PROP.findall(body):
            self.assertIn("!important", value, f"{prop} in the blanket is not !important")

    def test_the_blanket_is_the_only_motion_query(self):
        """One block, not one per animated selector.

        The per-selector form is what rotted in the first place: it can only cover
        the animations somebody remembered.
        """
        self.assertEqual(self.css.count("prefers-reduced-motion"), 1,
                         "more than one prefers-reduced-motion block; collapse "
                         "them into the blanket at the end of the file")

    def test_the_blanket_is_top_level_and_last(self):
        start, _, _ = blanket(self.css)
        self.assertEqual(self.css[:start].count("{") - self.css[:start].count("}"), 0,
                         "the blanket is nested inside another at-rule, so it does "
                         "not apply everywhere it looks like it does")
        last_anim = max([m.start() for m in ANIM.finditer(self.css)]
                        + [m.start() for m in TRANS.finditer(self.css)])
        self.assertGreater(start, last_anim,
                           "the blanket precedes an animated declaration")

    def test_every_animation_names_a_keyframes_that_exists(self):
        declared = set(n.lower() for n in KEYFRAMES.findall(self.css))
        used = animation_names(self.css)
        self.assertTrue(used, "no animations parsed, this test is reading nothing")
        ghost = sorted(set(used) - declared)
        self.assertEqual(ghost, [], f"animated but not defined: {ghost}")

    def test_no_keyframes_is_dead(self):
        declared = set(n.lower() for n in KEYFRAMES.findall(self.css))
        used = set(animation_names(self.css))
        dead = sorted(declared - used)
        self.assertEqual(dead, [], f"defined but never used: {dead}")

    def test_no_transition_outlives_a_status_change(self):
        long = [d for d in durations(self.css) if d > MAX_TRANSITION]
        self.assertEqual(long, [], f"transitions over {MAX_TRANSITION}s: {long}")

    def test_the_guards_can_fail(self):
        """Mutated COPY only; the shipped file must stay as the tests found it."""
        # the per-selector form creeping back next to the blanket
        two = self.css + "\n@media (prefers-reduced-motion: reduce) {\n  .x { animation: none; }\n}\n"
        self.assertEqual(two.count("prefers-reduced-motion"), 2,
                         "a second motion block went unnoticed")

        # a typo'd animation name: the rule parses, the pulse simply never runs
        ghost = self.css.replace(".tok-meter.compacting .tok-fill { animation: tokPulse 1s",
                                 ".tok-meter.compacting .tok-fill { animation: tokPuls 1s", 1)
        self.assertNotEqual(ghost, self.css, "the animation anchor moved")
        declared = {n.lower() for n in KEYFRAMES.findall(ghost)}
        self.assertIn("tokpuls", animation_names(ghost))
        self.assertNotEqual(set(animation_names(ghost)) - declared, set(),
                            "an animation naming a missing @keyframes passed")

        # the blanket without !important loses to every rule it is meant to beat
        weak = self.css.replace("animation-iteration-count: 1 !important;",
                                "animation-iteration-count: 1;", 1)
        self.assertTrue(any("!important" not in v
                            for p, v in PROP.findall(weak[weak.index("@media (prefers-reduced-motion"):])
                            if p == "animation-iteration-count"),
                        "a weakened blanket still looked locked")


if __name__ == "__main__":
    unittest.main()

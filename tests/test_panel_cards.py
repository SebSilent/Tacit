"""The panel-card requirement: "Panels (assistant, delegation, git, settings) share one
card language — same paddings, same headers, same controls." (The plan file this was
item 5 of has since been deleted; the requirement it recorded still stands.)

Before this slice there was no card language, only eight cards that each re-declared
their whole chrome: `background`, `border`, `border-radius` and `padding` written out
independently in five padding pairs and three radius dialects, four different head
class names, and **no test that mentioned a single one of them**. Nothing could stop a
ninth card from arriving with its own idea of a card.

What converged here is the part that moves no pixel:

* `.card` is the one declaration site for `background` / `border` / `border-radius`;
  a card that genuinely disagrees keeps an override, and the override set is pinned
  (`.dg-card` radius, `.mcp-card`/`.plug-card`/`.snap-card` border + radius).
* `.card-head` / `.card-actions` are the shared head and control-row structure; the
  four head dialects became one base plus three pinned overrides. `.dg-head` and
  `.prov-actions` were *identical* to the base and are gone.
* Controls: every button inside a card is `ho-btn …`, except the pinned `dg-toggle`.

What is **not** decided here — because it moves pixels and needs a browser — is which
padding pair and which radius the eight cards should share. That is still an open
deviation; the count test pins how many dialects exist so the gap cannot widen quietly
while it waits. It is pinned in pixels rather than token names precisely so that a
ladder collapse can be told apart from a card going its own way.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "static" / "style.css"
CSS = CSS_PATH.read_text(encoding="utf-8")

PANEL_JS = ["static/delegation.js", "static/harness.js", "static/platform.js"]
HTML = "static/index.html"

CARDS = ["ctl-card", "prov-card", "skill-card", "theme-card", "mcp-card", "plug-card",
         "dg-card", "snap-card"]
SHELL = ".card"
SHELL_PROPS = ("background", "border", "border-radius")

# The only card rules allowed to re-state a shell property, and what each restates.
CHROME_EXCEPTIONS = {
    ".dg-card": {"border-radius"},
    ".mcp-card, .plug-card": {"border", "border-radius"},
    ".snap-card": {"border", "border-radius"},
}
# The only head rules allowed to differ from `.card-head`.
HEAD_EXCEPTIONS = {
    ".prov-head": {"justify-content", "gap"},
    ".skill-head": {"gap"},
    ".mcp-head": {"flex-wrap"},
}
# Five padding pairs across seven cards. Pinned, not endorsed — see the deviation.
#
# Pinned in PIXELS, not token names, and re-pinned on 2026-10-19 when the spacing ladder
# collapsed 24 steps -> 12 on a 4-unit base. That renumbering moved the cards that sat on
# odd values (3px, 13px, 15px, 7px, 9px) onto the grid; the two cards already on it
# (10px 12px) did not move at all. Every drift below is +1px, and it is a rounding
# decision, not a redesign: ties in the collapse rounded UP, because crowding a dense UI
# costs more legibility than 1px of breathing room.
PADDINGS = {
    ".ctl-card": "4px",                    # was 3px
    ".prov-card": "14px 16px",             # was 13px 15px
    ".skill-card": "12px 14px",            # was 11px 13px
    ".theme-card": "12px 14px",            # was 11px 13px
    ".mcp-card, .plug-card": "10px 12px",  # unchanged — already on the grid
    ".snap-card": "10px 12px",             # unchanged
    ".dg-card": "8px 10px",                # was 7px 9px
}
# The shell's own corner. Was 10px; the radius ladder collapsed 10 steps -> 6 and the
# nearest surviving step is 12px. +2px on eight cards at once, which is the one move here
# that should be looked at in a browser before it is treated as settled.
SHELL_RADIUS = "12px"
# Buttons inside a card that are not the shared `ho-btn` idiom.
CONTROL_EXCEPTIONS = {"dg-toggle"}


def rules(text):
    """(selector, body, start) for every rule, comments stripped from the selector."""
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        sel = " ".join(re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S).split())
        out.append((sel, m.group(2), m.start()))
    return out


def body_of(text, sel):
    for s, b, _ in rules(text):
        if s == sel:
            return b
    raise AssertionError("no rule for %s" % sel)


def props(body):
    return {d.split(":")[0].strip() for d in body.split(";") if d.strip()}


def decl(body, prop):
    m = re.search(r"(?:^|;)\s*%s\s*:\s*([^;]+)" % re.escape(prop), body)
    return m.group(1).strip() if m else None


def ladder(text):
    """Every scale token -> its pixel value, read from the bare `:root` block."""
    out = {}
    for sel, body, _ in rules(text):
        if sel != ":root":
            continue
        for m in re.finditer(r"(--(?:sp|rd)-\d+|--radius[\w-]*)\s*:\s*([^;]+)", body):
            out[m.group(1)] = " ".join(m.group(2).split())
    return out


LADDER = ladder(CSS)


def px(value, scale=None):
    """Resolve a length declaration to pixels.

    These guards exist to catch a card drifting away from the shared language. They used
    to compare token NAMES, which cannot tell drift from a renumbering: when the spacing
    ladder collapsed from 24 steps to 12, `.msg-tag`'s `var(--sp-7)` became `var(--sp-4)`
    — 6px both before and after, nothing moved, and the guard screamed anyway. Comparing
    resolved pixels makes a pure rename invisible and a real move still fire, which is
    the thing the test was always trying to detect.
    """
    if value is None:
        return None
    scale = scale or LADDER
    def sub(m):
        tok = m.group(1)
        if tok not in scale:
            raise AssertionError("unknown or undeclared scale token %r" % tok)
        return scale[tok]
    return " ".join(re.sub(r"var\(\s*(--[\w-]+)\s*\)", sub, value).split())


def targets_card(sel, cls):
    """True when the rule styles the card box itself, not something inside it.
    `.dg-card.done` counts; `.dg-card.done .dg-dot` does not."""
    for part in sel.split(","):
        last = part.strip().split()[-1]
        if re.search(r"\.%s(?![\w-])" % cls, last):
            return True
    return False


def chrome_offenders(text):
    """Card rules that re-state shell chrome beyond their pinned exception."""
    out = {}
    for sel, body, _ in rules(text):
        if sel.startswith(SHELL):
            continue
        for cls in CARDS:
            if targets_card(sel, cls):
                bad = (props(body) & set(SHELL_PROPS)) - CHROME_EXCEPTIONS.get(sel, set())
                if bad:
                    out[sel] = sorted(bad)
    return out


def markup():
    """Every class attribute in the panel files + index.html, with its source."""
    hits = []
    for f in PANEL_JS + [HTML]:
        t = (ROOT / f).read_text(encoding="utf-8")
        for m in re.finditer(r'class="([^"]*)"', t):
            hits.append((f, m.group(1)))
    return hits


def card_template(cls):
    """The markup a panel renders for one card: from its opening tag to the end of
    that template literal — enough to see its head and its buttons."""
    for f in PANEL_JS:
        t = (ROOT / f).read_text(encoding="utf-8")
        i = t.find('class="card %s' % cls)
        if i < 0:
            continue
        end = t.find("`", i)
        return t[i:end if end > 0 else i + 900]
    raise AssertionError("no card markup builds .%s" % cls)


class PanelCardLanguage(unittest.TestCase):
    def test_one_shell_declares_the_card_chrome(self):
        b = body_of(CSS, SHELL)
        for p in SHELL_PROPS:
            self.assertIn(p, props(b), "the shell must own %s" % p)
        self.assertEqual(decl(b, "background"), "var(--bg-3)")
        self.assertEqual(decl(b, "border"), "1px solid var(--line)")
        self.assertEqual(px(decl(b, "border-radius")), SHELL_RADIUS,
                         "the shared card corner moved: %r" % decl(b, "border-radius"))

    def test_the_shell_precedes_every_card_that_inherits_it(self):
        """Equal specificity means source order decides: a shell placed after a panel
        rule would silently stop being the shell."""
        at = dict((s, i) for s, _, i in rules(CSS))
        self.assertIn(SHELL, at)
        for sel in list(CHROME_EXCEPTIONS) + list(PADDINGS):
            self.assertLess(at[SHELL], at[sel], "%s precedes %s" % (SHELL, sel))

    def test_no_card_redeclares_chrome_outside_the_pinned_exceptions(self):
        """Only rules that style the card *itself*. `.dg-card.done .dg-dot` paints a
        status dot, not the card box, and is legitimately free to set a background."""
        self.assertEqual(chrome_offenders(CSS), {}, "card chrome drifted back")
        for sel, allowed in CHROME_EXCEPTIONS.items():
            got = props(body_of(CSS, sel)) & set(SHELL_PROPS)
            self.assertEqual(got, allowed, "%s overrides exactly %s" % (sel, allowed))

    def test_every_panel_card_carries_the_shell_class(self):
        """The shell only applies if the markup asks for it. A card built without
        `card` looks right in the panel it lives in and wrong in the other five."""
        for cls in CARDS:
            sites = [(f, c) for f, c in markup()
                     if re.search(r"\b%s\b" % cls, c)]
            self.assertTrue(sites, ".%s is not rendered anywhere" % cls)
            for f, c in sites:
                self.assertIn("card", c.split(),
                              "%s in %s is missing the shell class: %r" % (cls, f, c))

    def test_every_card_head_carries_the_shared_head(self):
        heads = [c for f, c in markup() if re.search(r"\b(?:prov|skill|mcp)-head\b", c)]
        self.assertTrue(heads, "the head dialects vanished — that is not convergence")
        for c in heads:
            self.assertIn("card-head", c.split(), "head without the shared base: %r" % c)
        self.assertIn('class="card-head"',
                      (ROOT / "static" / "delegation.js").read_text(encoding="utf-8"))
        for sel in list(HEAD_EXCEPTIONS) + [".dg-head"]:
            if sel in [s for s, _, _ in rules(CSS)]:
                self.assertFalse({"display", "align-items"} & props(body_of(CSS, sel)),
                                 "%s re-declares what .card-head owns" % sel)

    def test_head_overrides_are_the_pinned_set(self):
        got = {}
        for sel, body, _ in rules(CSS):
            if sel == ".card-head":
                continue
            if any(targets_card(sel, h) for h in ("prov-head", "skill-head", "mcp-head",
                                                 "dg-head", "theme-head")):
                if props(body) & {"gap", "flex-wrap", "justify-content"}:
                    got[sel] = props(body)
        self.assertEqual(got, HEAD_EXCEPTIONS,
                         "head dialects changed shape: %s — update the deviation note, "
                         "not just this test" % got)

    def test_card_controls_use_one_button_idiom(self):
        for cls in ["prov-card", "skill-card", "mcp-card", "snap-card", "plug-card", "dg-card"]:
            tpl = card_template(cls)
            for c in re.findall(r'<button class="([^"$]*)', tpl):
                names = set(c.split())
                self.assertTrue(names & {"ho-btn"} or names & CONTROL_EXCEPTIONS,
                                "%s builds a private button idiom: %r" % (cls, c))
        # …and the shared control row exists where cards have one: the provider card's
        # actions row plus the three platform cards that already shared `mcp-actions`.
        rows = [(f, c) for f, c in markup() if "card-actions" in c.split()]
        self.assertEqual([f.rsplit("/", 1)[-1] for f, c in rows],
                         ["harness.js", "platform.js", "platform.js", "platform.js"],
                         "card-actions sites changed: %s" % rows)

    def test_padding_dialects_are_counted_not_drifting(self):
        """Five pairs today. This does not endorse them: it makes a sixth impossible to
        add quietly while the visual decision is open. Compared in resolved pixels, so
        renumbering the spacing ladder is not mistaken for the cards drifting."""
        got = {sel: px(decl(body_of(CSS, sel), "padding")) for sel in PADDINGS}
        self.assertEqual(got, PADDINGS, "padding changed without a decision: %s" % got)
        self.assertEqual(len(set(PADDINGS.values())), 5)

    def test_the_guards_can_fail(self):
        css = CSS.replace(".prov-card {\n  padding:", ".prov-card {\n  background: var(--bg-2);\n  padding:", 1)
        self.assertNotEqual(css, CSS, "plant 1 did not land")
        self.assertEqual(chrome_offenders(css), {".prov-card": ["background"]},
                         "a card re-declaring background must be caught")
        # a descendant rule must NOT be mistaken for card chrome
        self.assertEqual(chrome_offenders(CSS), {},
                         ".dg-card.done .dg-dot paints a dot, not the card box")

        js = (ROOT / "static" / "harness.js").read_text(encoding="utf-8")
        self.assertIn('class="card skill-card"', js)
        self.assertNotIn('class="skill-card"', js.replace('class="card skill-card"', ""),
                         "a card built without the shell class must be visible to the guard")

        # Located by pattern, not by literal text: the token names in this rule are exactly
        # what a ladder collapse rewrites, so a hard-coded anchor rots on the next one.
        plant = r"(\.dg-card\s*\{[^}]*?padding:\s*)[^;]+;"
        css3 = re.sub(plant, r"\g<1>var(--sp-6) var(--sp-7);", CSS, count=1)
        self.assertNotEqual(css3, CSS, "plant 3 did not land")
        self.assertNotEqual(px(decl(body_of(css3, ".dg-card"), "padding"), ladder(css3)),
                            PADDINGS[".dg-card"], "a new padding pair must fail the count")

        # The resolver must actually resolve. A px() that passed text through unchanged
        # would make every pixel pin above vacuously true.
        self.assertEqual(px("var(--sp-5)"), LADDER["--sp-5"])
        self.assertNotEqual(px("var(--sp-5)"), "var(--sp-5)")
        # …and a rename that preserves pixels must NOT fire, which is the whole point.
        scale2 = dict(LADDER, **{"--sp-90": LADDER["--sp-5"]})
        self.assertEqual(px("var(--sp-90)", scale2), px("var(--sp-5)"))


if __name__ == "__main__":
    unittest.main()

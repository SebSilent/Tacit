"""PLAN item 5: "Panels (assistant, delegation, git, settings) share one card
language — same paddings, same headers, same controls."

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
padding pair and which radius the eight cards should share. That is recorded as a
deviation in PLAN.md; test 7 pins the current dialect count so the gap cannot widen
while it waits.
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
PADDINGS = {
    ".ctl-card": "var(--sp-4)",
    ".prov-card": "var(--sp-14) var(--sp-16)",
    ".skill-card": "var(--sp-12) var(--sp-14)",
    ".theme-card": "var(--sp-12) var(--sp-14)",
    ".mcp-card, .plug-card": "var(--sp-11) var(--sp-13)",
    ".snap-card": "var(--sp-11) var(--sp-13)",
    ".dg-card": "var(--sp-8) var(--sp-10)",
}
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
        self.assertEqual(decl(b, "border-radius"), "var(--rd-8)")

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
        """Five pairs today. This does not endorse them: it makes a sixth impossible
        to add quietly while the visual decision (PLAN deviation) is open."""
        got = {}
        for sel in PADDINGS:
            got[sel] = decl(body_of(CSS, sel), "padding")
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

        css3 = CSS.replace(".dg-card {\n  border-radius: var(--radius-sm);\n  padding: var(--sp-8) var(--sp-10);",
                           ".dg-card {\n  border-radius: var(--radius-sm);\n  padding: var(--sp-6) var(--sp-19);", 1)
        self.assertNotEqual(css3, CSS, "plant 3 did not land")
        self.assertNotEqual(decl(body_of(css3, ".dg-card"), "padding"),
                            PADDINGS[".dg-card"], "a new padding pair must fail the count")


if __name__ == "__main__":
    unittest.main()

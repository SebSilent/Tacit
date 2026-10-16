"""The sidebar drawer has to be dismissible, and nothing but this file makes it so.

This was written for the `/mobile` page, which is gone; what it pins is the **desktop**
sidebar's off-canvas mode, so it now carries that name. Nothing here depends on a mobile
page existing - the file is a `static/app.js` and `static/style.css` source assertion.

Under 760px the sidebar stops being a column and becomes an off-canvas drawer:
`style.css` paints `.sidebar.open` over the transcript and adds a scrim at
`.app:has(.sidebar.open)::after`, z-index 45, under the drawer. That scrim is the
tap target, and it sat there for a whole release with nothing listening for the
tap - CSS can draw a dismissal and the JavaScript can still forget it. The toggle
alone left three ways a hand reaches for and only one of them worked.

So the specification is three dismissal paths, not one: tap away, Escape, and
picking a session. Each is asserted against `static/app.js` by function body, and
each has a plant in `test_the_guards_can_fail`, because a guard that cannot fail is
how this shipped green the first time.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

BREAKPOINT = 760


def fn_body(js, name):
    """The `{...}` of `function name(...)`, braces balanced."""
    i = js.index("function " + name + "(")
    j = js.index("{", i)
    depth = 0
    for k in range(j, len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[j:k + 1]
    raise AssertionError("unterminated function: " + name)


def doc_listener(js, event, needle):
    """A document-level listener whose body contains `needle`, plus the text that
    follows its closing brace (so the capture flag is readable)."""
    pat = "document.addEventListener('" + event + "'"
    pos, out = 0, []
    while True:
        i = js.find(pat, pos)
        if i < 0:
            break
        j = js.index("{", i)
        depth, end = 0, -1
        for k in range(j, len(js)):
            if js[k] == "{":
                depth += 1
            elif js[k] == "}":
                depth -= 1
                if depth == 0:
                    end = k
                    break
        if end < 0:
            break
        out.append((js[i:end + 1], js[end + 1:end + 12]))
        pos = end
    for chunk, tail in out:
        if needle in chunk:
            return chunk, tail
    return None, None


def opens_on_the_button(js):
    """The button still decides drawer vs desktop column by the same width the CSS uses."""
    i = js.find("$('#sidebarToggle').addEventListener")
    if i < 0:
        return False
    chunk = js[i:i + 300]
    return ("innerWidth <= " + str(BREAKPOINT) in chunk
            and "classList.toggle('open')" in chunk
            and "classList.toggle('hidden')" in chunk)


def state_is_scoped_to_the_drawer(js):
    """One owner for `.open`, and it only speaks when the drawer is really a drawer.
    Without the width test every Escape and every stray click churns a class the
    desktop layout never set."""
    o, c = fn_body(js, "drawerOpen"), fn_body(js, "closeDrawer")
    return ("innerWidth <= " + str(BREAKPOINT) in o
            and "classList.contains('open')" in o
            and "if (drawerOpen())" in c
            and "classList.remove('open')" in c)


def dismisses_on_tap_away(js):
    chunk, tail = doc_listener(js, "click", "closeDrawer();")
    if chunk is None:
        return False
    return ("drawerOpen()" in chunk
            and "#sidebar" in chunk and "#sidebarToggle" in chunk
            and tail.startswith(", true"))


def dismisses_on_escape(js):
    chunk, _ = doc_listener(js, "keydown", "closeDrawer();")
    return chunk is not None and "'Escape'" in chunk


def dismisses_on_pick(js):
    body = fn_body(js, "switchSession")
    if "closeDrawer();" not in body:
        return False
    return body.index("closeDrawer();") < body.index("if (id === activeId) return;")


class TestDrawerDismissal(unittest.TestCase):

    def test_the_toggle_still_opens_it(self):
        self.assertTrue(opens_on_the_button(APP), "the button no longer opens a drawer")

    def test_one_function_owns_the_open_state(self):
        self.assertTrue(state_is_scoped_to_the_drawer(APP),
                        "drawerOpen/closeDrawer moved or lost their width test")

    def test_a_tap_away_from_the_drawer_closes_it(self):
        self.assertTrue(dismisses_on_tap_away(APP),
                        "the scrim is painted but nothing listens for a tap on it")

    def test_escape_closes_it(self):
        self.assertTrue(dismisses_on_escape(APP), "Escape does not dismiss the drawer")

    def test_picking_a_session_closes_it(self):
        self.assertTrue(dismisses_on_pick(APP),
                        "switching sessions leaves the drawer over the transcript")

    def test_the_guards_can_fail(self):
        # matched by shape, not by wording: the line carries a comment, and a plant
        # that breaks when someone rephrases it is a plant that tests the prose
        line = re.search(r"[^\n]*closeDrawer\(\);[^\n]*\n", fn_body(APP, "switchSession"))
        self.assertIsNotNone(line, "plant 1 found no dismissal to remove")
        p1 = APP.replace(line.group(0), "", 1)
        self.assertNotEqual(p1, APP, "plant 1 did not land")
        self.assertFalse(dismisses_on_pick(p1), "plant 1 still passed")

        p2 = APP.replace("  closeDrawer();\n}, true);", "  closeDrawer();\n});", 1)
        self.assertNotEqual(p2, APP, "plant 2 did not land")
        self.assertFalse(dismisses_on_tap_away(p2),
                         "a bubble-phase tap-away loses to a row that stops propagation")

        p3 = APP.replace("if (e.key === 'Escape') closeDrawer();",
                         "if (e.key === 'Escape') void 0;", 1)
        self.assertNotEqual(p3, APP, "plant 3 did not land")
        self.assertFalse(dismisses_on_escape(p3), "plant 3 still passed")

        p4 = APP.replace("return innerWidth <= 760 && $('#sidebar').classList.contains('open');",
                         "return $('#sidebar').classList.contains('open');", 1)
        self.assertNotEqual(p4, APP, "plant 4 did not land")
        self.assertFalse(state_is_scoped_to_the_drawer(p4), "plant 4 still passed")


class TestDrawerIsStillADrawer(unittest.TestCase):
    """The JS only means something while the CSS still makes it a drawer."""

    def test_the_scrim_the_js_listens_for_is_still_painted(self):
        self.assertIn(".app:has(.sidebar.open)::after", CSS,
                      "the scrim went: a tap-away now has nothing to tap")

    def test_the_drawer_is_positioned_off_canvas(self):
        rule = re.search(r"\.sidebar\.open\s*\{([^}]*)\}", CSS)
        self.assertIsNotNone(rule, "`.sidebar.open` is gone, so `.open` sets nothing")

    def test_the_breakpoint_matches_the_ones_the_css_declares(self):
        widths = set(re.findall(r"@media \(max-width:\s*(\d+)px\)", CSS))
        self.assertIn(str(BREAKPOINT), widths,
                      f"app.js dismisses at {BREAKPOINT}px but the layout changes at {sorted(widths)}")


if __name__ == "__main__":
    unittest.main()

"""The three fixes that closed out the appearance work, pinned.

Each of these was a thing the interface did wrong that no test could see:

* The composer drew a square focus outline around the writing area while its own
  rounded box lit at the same time. Two indicators for one state, the uglier one
  on top. Deleting an outline is an accessibility failure unless something visible
  stays, so this file pins the replacement, not just the removal.
* A tool result had a 280px cap and a scrollbar, and still read as a card that
  grew without limit, because its only separation from the card was a `border-top`
  — containment you cannot see is not containment.
* The assistant's `assistant_tool_start` fired a toast and `assistant_tool_end`
  was an empty `break`. The backend was already emitting both events; nothing drew
  them. A feature that is half-wired is worse than one that is absent, because the
  half that is wired looks like it works.

Everything here is decidable from source text, which is why it is in a test rather
than on the browser list. What stays a browser call is whether the well *reads* as
contained and whether one ring *reads* as a focus indicator.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
JS = (ROOT / "static" / "assistant.js").read_text(encoding="utf-8")


def strip(text):
    return re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), text, flags=re.S)


def rule(css, selector):
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{(.*?)^\}", css, re.S)
    return m.group(1) if m else None


def decl(body, prop):
    if body is None:
        return None
    m = re.search(r"(?m)^\s*" + re.escape(prop) + r"\s*:\s*([^;]+);", body)
    return m.group(1).strip() if m else None


def count_rule(css, selector):
    return len(re.findall(r"(?m)^" + re.escape(selector) + r"\s*\{", css))


CSS_CODE = strip(CSS)


class ToolResultIsAWell(unittest.TestCase):
    """A scrollable box with its own ground, not the lower half of the card."""

    def test_it_caps_and_scrolls(self):
        b = rule(CSS_CODE, ".tool-body")
        self.assertIsNotNone(b, "the tool body moved")
        self.assertRegex(decl(b, "max-height") or "", r"\d+px",
                         "the result cap is gone, so a long output grows the card")
        self.assertIn(decl(b, "overflow"), ("auto", "scroll"),
                      "capped but not scrollable: the tail is unreachable")

    def test_it_has_its_own_box(self):
        b = rule(CSS_CODE, ".tool-body")
        for prop in ("background", "border", "border-radius", "box-shadow"):
            v = decl(b, prop)
            self.assertIsNotNone(v, "a contained well needs its own %s" % prop)
        self.assertEqual(decl(b, "background"), "var(--code-bg)",
                         "the well should sit on the code ground, like every other "
                         "block of output on the page")
        self.assertIn("inset", decl(b, "box-shadow"),
                      "an outset shadow on a box inside a box is two elevations for "
                      "one surface; this one recedes")

    def test_it_is_inset_from_the_card(self):
        """The margin is what makes it read as inside rather than as more card."""
        m = decl(rule(CSS_CODE, ".tool-body"), "margin")
        self.assertIsNotNone(m, "no inset — the well touches the card's edges and "
                                "reads as the card's lower half")
        self.assertNotEqual(m, "0")

    def test_the_well_is_not_also_a_border_top(self):
        self.assertIsNone(decl(rule(CSS_CODE, ".tool-body"), "border-top"),
                          "a border-top on a box that already has a border all "
                          "round is the old separation left behind")

    def test_the_guards_can_fail(self):
        css = CSS_CODE.replace("  max-height: 280px;\n", "\n", 1)
        self.assertNotEqual(css, CSS_CODE, "plant 1 did not land")
        self.assertNotRegex(decl(rule(css, ".tool-body"), "max-height") or "", r"\d+px")

        css2 = CSS_CODE.replace("  overflow: auto;", "  overflow: visible;", 1)
        self.assertNotEqual(css2, CSS_CODE, "plant 2 did not land")
        self.assertNotIn(decl(rule(css2, ".tool-body"), "overflow"), ("auto", "scroll"),
                         "a capped well whose overflow is not scrollable hides the tail")


class OneFocusIndicatorPerControl(unittest.TestCase):
    """A borderless textarea inside a rounded box must not draw its own square."""

    def test_the_composer_textarea_is_borderless(self):
        """The premise. If this stops being true the suppression below is wrong."""
        self.assertEqual(decl(rule(CSS_CODE, ".composer textarea"), "border"), "0",
                         "the composer textarea now has its own border, so the "
                         "outline suppression below removes a real indicator")

    def test_the_composer_textarea_draws_no_outline(self):
        b = rule(CSS_CODE, ".composer textarea:focus-visible")
        self.assertIsNotNone(b, "no override, so the global square outline is back")
        self.assertEqual(decl(b, "outline"), "none")

    def test_the_container_still_shows_focus(self):
        """The removal is only legal because something visible stays."""
        b = rule(CSS_CODE, ".composer:focus-within")
        self.assertIsNotNone(b, "the composer has no focus state at all")
        self.assertEqual(decl(b, "border-color"), "var(--accent)")
        self.assertIsNotNone(decl(b, "box-shadow"),
                             "a 1px border colour change is a thin focus indicator; "
                             "the ring is what makes it findable")

    def test_the_assistant_input_is_the_same_case(self):
        b = rule(CSS_CODE, ".as-input textarea:focus-visible")
        self.assertIsNotNone(b, "the assistant textarea is drawing a second ring")
        self.assertEqual(decl(b, "outline"), "none")
        self.assertEqual(decl(rule(CSS_CODE, ".as-input textarea:focus"), "border-color"),
                         "var(--accent)", "outline removed with nothing left to show")

    def test_no_selector_is_declared_twice(self):
        """The fix first shipped as a second `.composer:focus-within` block rather
        than an edit of the incumbent one. It rendered fine and defeated any guard
        that reads the first match — the exact trap test_panel_cards documents."""
        for sel in (".composer:focus-within", ".composer textarea:focus-visible",
                    ".as-input textarea:focus-visible", ".tool-body"):
            self.assertEqual(count_rule(CSS_CODE, sel), 1,
                             "%s is declared %d times" % (sel, count_rule(CSS_CODE, sel)))

    def test_the_guards_can_fail(self):
        css = CSS_CODE.replace(".composer textarea:focus-visible {\n  outline: none;",
                               ".composer textarea:focus-visible {\n  outline: revert;", 1)
        self.assertNotEqual(css, CSS_CODE, "plant 1 did not land")
        self.assertNotEqual(decl(rule(css, ".composer textarea:focus-visible"), "outline"),
                            "none")

        css2 = CSS_CODE.replace("  box-shadow: 0 0 0 var(--sp-1) color-mix(in srgb, "
                                "var(--accent) 32%, transparent);\n", "", 1)
        self.assertNotEqual(css2, CSS_CODE, "plant 2 did not land")
        self.assertIsNone(decl(rule(css2, ".composer:focus-within"), "box-shadow"),
                          "the ring was the surviving indicator; without it the "
                          "outline removal is an accessibility regression")


class AssistantDrawsItsToolCalls(unittest.TestCase):
    """The events were already arriving. Pinning that they are now rendered."""

    def case(self, name):
        m = re.search(r"case '%s': \{(.{0,2200}?)\n      \}\n" % name, JS, re.S)
        self.assertIsNotNone(m, "case '%s' is missing or no longer a block" % name)
        return m.group(1)

    def test_start_builds_a_tool_card(self):
        body = self.case("assistant_tool_start")
        self.assertIn("tool-card", body, "the event is swallowed again")
        self.assertIn("tool-badge", body, "a card with no name on it")
        self.assertIn("tool-body", body, "a card with nowhere for the result to go")
        self.assertIn("turnTools.push", body,
                      "the card exists only in the DOM, so the next re-render erases it")

    def test_end_writes_the_result(self):
        body = self.case("assistant_tool_end")
        self.assertIn(".tool-body pre", body)
        self.assertIn("textContent", body, "writing markup, not text")
        self.assertNotIn("innerHTML", body, "result text is untrusted output")
        self.assertIn("turnTools", body, "the record must be closed or a later end "
                                         "event fills the wrong card")
        self.assertIn("rec.err", body, "error state set on the node only would not "
                                       "survive the re-render from turnTools")

    def test_a_tool_before_the_first_delta_still_has_a_home(self):
        """The answer's element used to be created by the first text delta. A tool
        call that arrives before it had nowhere to go."""
        self.assertIn("as-msg them", self.case("assistant_tool_start"),
                      "the streaming message is assumed to exist")

    def test_the_render_from_messages_draws_them_too(self):
        """The thread re-renders from `messages` on every save. If the cards were
        only appended by the live path they would vanish the moment the answer
        was stored — which is the bug this whole case exists for."""
        self.assertIn("asToolsHtml()", JS)
        i = JS.index("function renderMessages()")
        self.assertIn("asToolsHtml()", JS[i:i + 3000],
                      "renderMessages does not draw the turn's tools")

    def test_the_toggle_is_delegated_not_per_card(self):
        m = re.search(r"function bindToolToggles\(box\) \{(.*?)\n  \}", JS, re.S)
        self.assertIsNotNone(m, "no toggle handler")
        body = m.group(1)
        self.assertIn("box._toolsBound", body,
                      "re-binding on every render leaks a listener per re-render")
        self.assertEqual(body.count("addEventListener"), 1,
                          "more than one listener in a delegated handler")
        self.assertIn(".as-tools .tool-head", body)

    def test_tools_clear_on_a_new_turn_without_anyone_being_told(self):
        """Nothing announces the start of a turn. Tools belong to the answer that
        follows the last prompt, so a thread ending on a user message means the
        previous turn is over."""
        i = JS.index("function renderMessages()")
        window = JS[i:i + 1400]
        self.assertIn("turnTools = []", window,
                      "the previous turn's tool calls would be drawn under the "
                      "next answer")
        self.assertIn("role === 'user'", window)

    def test_the_guards_can_fail(self):
        js = JS.replace("        turnTools.push(rec);", "", 1)
        self.assertNotEqual(js, JS, "plant 1 did not land")
        self.assertNotIn("turnTools.push",
                         re.search(r"case 'assistant_tool_start': \{(.{0,2200}?)\n      \}\n",
                                   js, re.S).group(1))
        js2 = JS.replace("    bindToolToggles(box);\n    box.scrollTop",
                         "    box.scrollTop", 1)
        self.assertNotEqual(js2, JS, "plant 2 did not land")
        i = js2.index("function renderMessages()")
        self.assertNotIn("bindToolToggles(box)", js2[i:i + 3000],
                         "a re-render would leave the cards unclickable")


if __name__ == "__main__":
    unittest.main()

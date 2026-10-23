"""The three fixes that closed out the appearance work, pinned.

Each of these was a thing the interface did wrong that no test could see:

* The composer drew a square focus outline around the writing area while its own
  rounded box lit at the same time. Two indicators for one state, the uglier one
  on top. Deleting an outline is an accessibility failure unless something visible
  stays, so this file pins the replacement, not just the removal.
* A tool result had a 280px cap and a scrollbar, and still read as a card that
  grew without limit, because its only separation from the card was a `border-top`.
  Containment you cannot see is not containment.
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

    def cap(self, css):
        """The result cap, resolving a var() through the token layer.

        Three surfaces were moved onto one shared --peek-h, and a guard that
        insisted on a literal would then pass only if the cap were hard-coded
        again -- the opposite of what the token exists for. What must stay true
        is that the cap is a real length, not that it is spelled in place.
        """
        v = decl(rule(css, ".tool-body"), "max-height") or ""
        m = re.fullmatch(r"var\((--[\w-]+)\)", v)
        if m:
            d = re.search(r"(?m)^\s*" + re.escape(m.group(1)) + r"\s*:\s*([^;]+);", css)
            v = d.group(1).strip() if d else ""
        return v

    def mutate(self, css, prop, value):
        """Replace one declaration inside the .tool-body rule only.

        Scoped to the rule because several rules now share the identical
        `max-height: var(--peek-h)` line, and a file-wide replace edits whichever
        one happens to come first -- a plant that lands on the reasoning body
        proves nothing about the well. The rule body is the unique anchor, so the
        swap goes through it rather than through the line.
        """
        body = rule(css, ".tool-body")
        self.assertIsNotNone(body, "premise: .tool-body exists")
        old = re.search(r"(?m)^\s*" + re.escape(prop) + r"\s*:\s*[^;]+;\n", body)
        self.assertIsNotNone(old, f"premise: .tool-body declares {prop}")
        return css.replace(body, body.replace(old.group(0), value, 1), 1)

    def test_it_caps_and_scrolls(self):
        b = rule(CSS_CODE, ".tool-body")
        self.assertIsNotNone(b, "the tool body moved")
        self.assertRegex(self.cap(CSS_CODE), r"^\d+px$",
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
        self.assertIsNotNone(m, "no inset; the well touches the card's edges and "
                                "reads as the card's lower half")
        self.assertNotEqual(m, "0")

    def test_the_well_is_not_also_a_border_top(self):
        self.assertIsNone(decl(rule(CSS_CODE, ".tool-body"), "border-top"),
                          "a border-top on a box that already has a border all "
                          "round is the old separation left behind")

    def test_the_guards_can_fail(self):
        css = self.mutate(CSS_CODE, "max-height", "\n")
        self.assertNotEqual(css, CSS_CODE, "plant 1 did not land")
        self.assertNotRegex(self.cap(css), r"^\d+px$",
                            "the cap was deleted and the guard still passed")

        css2 = self.mutate(CSS_CODE, "overflow", "  overflow: visible;\n")
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
        that reads the first match; the exact trap test_panel_cards documents."""
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
        was stored, which is the bug this whole case exists for."""
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


class ToolBlockIsBounded(unittest.TestCase):
    """The call list gets a box of its own once it is long enough to need one.

    A turn with twenty calls stacked an unbounded column of cards, so the newest
    call (the one you are actually waiting on) landed below the fold and the
    answer arrived a screenful later. The fix is a capped, scrolling `.tools` box
    that appears only at three or more cards, plus a follow that keeps the newest
    card in view. Both halves matter: a box nobody scrolls inside is worse than no
    box, and a box that yanks a reader who scrolled up is worse than either.
    """

    APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    SEL = ".tools:has(.tool-card:nth-of-type(3))"

    @staticmethod
    def resolved(css, selector, prop):
        v = decl(rule(css, selector), prop) or ""
        m = re.fullmatch(r"var\((--[\w-]+)\)", v)
        if m:
            d = re.search(r"(?m)^\s*" + re.escape(m.group(1)) + r"\s*:\s*([^;]+);", css)
            v = d.group(1).strip() if d else ""
        return v

    def test_it_is_a_box_of_its_own_at_three_cards(self):
        b = rule(CSS_CODE, self.SEL)
        self.assertIsNotNone(b, "the bounded call block moved")
        self.assertRegex(self.resolved(CSS_CODE, self.SEL, "max-height"), r"^\d+px$",
                         "no definite cap, so the column is unbounded again")
        self.assertIn(decl(b, "overflow-y"), ("auto", "scroll"),
                      "capped but not scrollable: the newest call is unreachable")
        self.assertEqual(decl(b, "overscroll-behavior"), "contain",
                         "scrolling off the end hands the wheel to the transcript "
                         "behind it and the page jumps")

    def test_the_cap_is_the_same_number_as_the_wells(self):
        """One token, three surfaces. If they drift, the guard has to say so; the
        token exists precisely so they cannot."""
        well = self.resolved(CSS_CODE, ".tool-body", "max-height")
        block = self.resolved(CSS_CODE, self.SEL, "max-height")
        self.assertEqual(well, block,
                         "the result well and the call block no longer share a cap")

    def test_one_or_two_calls_are_still_not_a_frame_within_a_frame(self):
        self.assertIsNone(
            re.search(r"(?m)^\.tools\s*\{[^}]*max-height", CSS_CODE),
            "an unconditional cap on .tools boxes a single call inside a message "
            "that is already a box")

    def test_the_newest_card_is_not_below_the_fold(self):
        i = self.APP.index("const atEnd = tools.scrollHeight")
        window = self.APP[i:i + 200]
        self.assertIn("if (atEnd) tools.scrollTop = tools.scrollHeight;", window,
                      "the block stopped following its own end: a new call arrives "
                      "below the fold and looks like the turn stalled")
        self.assertLess(window.index("atEnd ="), window.index("appendChild(card)"),
                        "measured after the append, the block has already grown, so "
                        "atEnd is false for every long result and nothing follows")

    def test_the_guards_can_fail(self):
        js = self.APP.replace("if (atEnd) tools.scrollTop = tools.scrollHeight;",
                              "tools.scrollTop = tools.scrollHeight;", 1)
        self.assertNotEqual(js, self.APP, "plant 1 did not land")
        self.assertNotIn("if (atEnd) tools.scrollTop", js,
                         "the follow became unconditional and yanks a reader who "
                         "scrolled up inside the block")

        js2 = self.APP.replace(
            "  const atEnd = tools.scrollHeight - tools.scrollTop"
            " - tools.clientHeight < 40;\n  tools.appendChild(card);\n",
            "  tools.appendChild(card);\n  const atEnd = tools.scrollHeight"
            " - tools.scrollTop - tools.clientHeight < 40;\n", 1)
        self.assertNotEqual(js2, self.APP, "plant 2 did not land")
        i = js2.index("const atEnd = tools.scrollHeight")
        self.assertLess(js2.index("tools.appendChild(card)"), i,
                        "plant 2 did not move the measurement after the append")

        css = CSS_CODE.replace(self.SEL + " {\n", ".tools {\n", 1)
        self.assertNotEqual(css, CSS_CODE, "plant 3 did not land")
        self.assertIsNotNone(re.search(r"(?m)^\.tools\s*\{[^}]*max-height", css),
                            "the cap became unconditional and one call is boxed again")


if __name__ == "__main__":
    unittest.main()

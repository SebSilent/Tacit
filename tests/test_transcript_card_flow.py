"""One card per turn, live. Pinned in source, because the browser is not.

The symptom was: chat to the agent and the answer continues in the *previous*
card. It only looked right after a reload, which is the tell; `renderTranscript()`
draws the stored rows and the stored rows are correct, so the transcript was
never wrong; the live view was.

`resumeLiveStream()` used to answer "which bubble is this turn's" with "the last
assistant bubble on screen". `session_runtime.start_turn` publishes
`state_delta busy:true` (backend/session_runtime.py:877) *before* the prompt is
appended and before `user_message` is published (:920), so a view whose writer was
not yet armed adopted the bubble the **previous** turn finished in. Every delta of
the new turn landed there. `case 'user_message'` then called
`ensureStreamingBubble()`, which no-oped because `streaming` was set.

The fix is a second record, `openCard`: the card the turn in flight owns, set by
`send()` and by `renderTranscript()` for a session that is mid-turn, released by
`endStream()`, and re-armed only through `heldCard()`. `streaming` alone cannot
carry this; `endStreamQuiet()` clears it on any idle report, on a session switch,
and on a reconnect, none of which end a turn.

These assertions are on source text, in the style of `test_transcript_structure.py`
and `test_md_escapes.py`: the repo's suite stays offline and installs nothing, so a
headless browser is not available here. What is pinned is the structure that makes
the behaviour what it is. The visual result (one card per answer, no stray empty
bubble) is still a browser call, recorded at the bottom of
`docs/interface.md`'s sibling work and repeated in the report.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def code(src):
    """JS with comments blanked, so a comment that *names* a bug is not a match."""
    out = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//[^\n]*", "", out)


def body(src, name):
    """The text of `function name(...) { ... }`, braces balanced, strings skipped."""
    m = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"{name}() not found"
    i, depth, n = src.index("{", m.start()), 0, len(src)
    start = i
    while i < n:
        c = src[i]
        if c in "\"'`":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces in {name}()")


def code_body(name):
    """`body()` over app.js with the comments already stripped."""
    return body(code(APP), name)


class OneCardPerTurn(unittest.TestCase):
    """The adoption that caused it must not come back."""

    def test_resume_arms_only_a_card_it_holds(self):
        b = code_body("resumeLiveStream")
        self.assertIn("heldCard(s)", b,
                      "resumeLiveStream() no longer asks which card this view holds")
        self.assertNotIn("rendered[rendered.length - 1].querySelector", b,
                         "resumeLiveStream() is adopting 'the last assistant bubble on "
                         "screen' again, that is the previous turn's card")

    def test_held_card_checks_all_three_conditions(self):
        """Session, position, and still in the document. Drop any one and a stale
        bubble from another session, another turn, or a wiped transcript is
        reachable again."""
        b = code_body("heldCard")
        for needle in ("openCard.sid !== s.id",
                       "s.messages[s.messages.length - 1] !== openCard.msg",
                       "!openCard.el.isConnected"):
            self.assertIn(needle, b, f"heldCard() no longer checks {needle!r}")

    def test_end_of_turn_releases_the_card(self):
        b = code_body("endStream")
        self.assertIn("openCard = null;", b,
                      "endStream() no longer releases the card, so the next turn "
                      "can be handed the previous answer's bubble")

    def test_a_quiet_end_releases_only_a_card_that_said_something(self):
        """`endStreamQuiet()` runs on a session switch and on a reconnect, where
        the turn is still running: releasing the card there would split one answer
        across two bubbles. Releasing it never is the missed-`done` bug."""
        b = code_body("endStreamQuiet")
        self.assertIn("openCard = null;", b)
        self.assertIn("m.content || m.reason", b,
                      "endStreamQuiet() no longer distinguishes a finished card "
                      "from a send that has not produced output yet")

    def test_send_records_its_card(self):
        b = code_body("send")
        self.assertIn("setOpenCard(s, s.messages[at], el);", b,
                      "send() opens a card nobody records; the busy signal that "
                      "follows will find nothing held")

    def test_a_user_row_rebases_the_view(self):
        """The server publishes the prompt, and a card armed before that row
        belongs to the turn the prompt follows."""
        case = re.search(r"case 'user_message': \{.*?\n    \}", code(APP), re.S)
        self.assertTrue(case, "case 'user_message' not found")
        text = case.group(0)
        self.assertLess(text.index("rebaseOnUserRow(s, streaming)"),
                        text.index("s.messages.push("),
                        "rebase happens after the row is pushed, so the card it "
                        "meant to drop is already the wrong one")
        self.assertIn("if (echoed) break;", text,
                      "the local echo check moved; every typed prompt would now "
                      "rebase its own card")

    def test_deltas_self_heal_instead_of_being_dropped(self):
        """`reason` always had this guard. `text` and `tool_start` did not, so a
        delta with no card open vanished and the turn looked like it said nothing."""
        c = code(APP)
        # Whitespace-tolerant: the comment stripped out of `case 'text'` leaves its
        # own line behind, and an assertion that breaks on a blank line is a guard
        # that fails for the wrong reason.
        for kind in ("text", "tool_start"):
            self.assertRegex(
                c, r"case '" + kind + r"':\s*if \(!streaming\) ensureStreamingBubble\(\);",
                f"case '{kind}' no longer opens a card when a delta arrives with none")

    def test_ensure_bubble_is_not_a_second_implementation(self):
        """Two functions answering "which card is this turn's" is how they drifted
        apart once already."""
        b = code_body("ensureStreamingBubble")
        self.assertEqual(b.count("appendMsg("), 0,
                         "ensureStreamingBubble() builds a bubble again; there are "
                         "now two answers to which card the turn owns")
        self.assertIn("resumeLiveStream();", b)

    def test_the_replay_passes_its_own_knowledge_of_the_turn(self):
        """A non-empty replay is proof the turn is running. `sessionStates` is fed
        by a 3s poll and `endStreamQuiet()` has just cleared `busy`, so the render
        cannot find this out by itself."""
        self.assertIn("renderTranscript(s, m.events.length > 0)", code(APP))


class RenderRecordsTheOpenCard(unittest.TestCase):
    def test_only_a_busy_session_has_an_open_card(self):
        c = code(APP)
        self.assertIn("const live = !!midTurn", c,
                      "renderTranscript() no longer decides liveness per session")
        self.assertIn("openCard = null;", c[c.index("const live = !!midTurn"):
                                          c.index("const live = !!midTurn") + 700],
                      "a session that is not mid-turn leaves a stale card behind")

    def test_the_card_is_the_trailing_group_not_any_assistant_row(self):
        c = code(APP)
        self.assertIn("groups[groups.length - 1].role === 'assistant'", c,
                      "renderTranscript() no longer requires the last group to be "
                      "the assistant's; any older bubble becomes reachable")


class GuardHasTeeth(unittest.TestCase):
    """Each guard must bite on the exact regression it describes."""

    def test_the_old_adoption_is_caught(self):
        planted = body(code(APP), "resumeLiveStream").replace(
            "const held = heldCard(s);",
            "const held = heldCard(s);\n  const rendered = messagesEl.querySelectorAll('.msg.assistant');\n"
            "  if (rendered.length) { streaming = { msg: held && held.msg, "
            "el: rendered[rendered.length - 1].querySelector('.msg-body-inner') }; return; }", 1)
        self.assertNotEqual(planted, body(code(APP), "resumeLiveStream"), "anchor moved")
        self.assertIn("rendered[rendered.length - 1].querySelector", planted,
                      "the plant did not reproduce the old shape")

    def test_dropping_the_release_is_caught(self):
        planted = code(APP).replace("  openCard = null;\n  setBusy(false);\n  markActiveSessionState('idle');",
                                    "  setBusy(false);\n  markActiveSessionState('idle');", 1)
        self.assertNotEqual(planted, code(APP), "anchor moved")
        self.assertNotIn("openCard = null;", body(planted, "endStream"))

    def test_a_second_bubble_builder_is_caught(self):
        planted = code(APP).replace("function ensureStreamingBubble() {\n  resumeLiveStream();",
                                    "function ensureStreamingBubble() {\n  if (streaming) return;\n"
                                    "  appendMsg('assistant', '', null, 0, 0);", 1)
        self.assertNotEqual(planted, code(APP), "anchor moved")
        self.assertNotEqual(body(planted, "ensureStreamingBubble").count("appendMsg("), 0)


if __name__ == "__main__":
    unittest.main()

"""Two transcript bullets that were met, unclaimed, and unguarded — pinned.

`PLAN.md:544-546`: "Tighten the transcript: clearer role separation, calmer tool
cards (collapsed by default, expandable, result monospace), reasoning visually
distinct from the answer."

`PLAN.md:691-692`: "A 'thinking' surface that does not bury the answer: reasoning
collapsed by default, one click to expand, never interleaved into the answer text."

Neither was ticked, neither was written up as done, and no test named any part of
either — while all of it is decidable from source text, which is why it belongs here
rather than in the browser list. (An earlier draft of this docstring merged the two
bullets into one and added a "one-line summary" clause that exists in neither, nor
anywhere else in the repo. Quoting the plan from memory is how a wrong fact outlives
the session that wrote it.)

Two things this guard is *not*:
  * It does not judge pixels. Every assertion is on a declaration that exists in
    `static/style.css` or an assignment that exists in `static/app.js`. The
    collapsed/expanded *experience* stays a browser call; what is pinned is the
    structure that makes the behaviour what it is — `display: none` by default, and
    the only thing that flips it is the click.
  * It does not trust prose. Comments in both files *describe* these properties
    (`app.js:1136-1139` explains why no listener sits on `.reason-toggle`;
    `style.css:994-996` explains the `.msg-tag` rationale), so comments are stripped
    before matching — the same failure the toast guard recorded, and the one this
    file's own sibling (`test_md_escapes.py`) re-sprang on `reason` vs `reason-block`.

Every predicate takes its source as an argument rather than reading the module
global. That is not tidiness: the first draft had `collapsed_by_default` reading
the global, and its planted violation passed — a guard that cannot be mutated
cannot be trusted.
"""
import re
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def css_code(src: str = None) -> str:
    """CSS with block comments blanked, line numbers preserved."""
    src = CSS if src is None else src
    return re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), src, flags=re.S)


def js_lines(src: str = None) -> list:
    """app.js without whole-line comments.

    Only full-line `//` (and `*`) comments are dropped: a mid-line strip would eat
    the `//` in `https://` inside a URL string, and the prose that can fool a
    matcher here (`:1136-1139`, `:1356-1357`) is all full-line.
    """
    src = APP if src is None else src
    return [ln for ln in src.split("\n")
            if not (ln.lstrip().startswith("//") or ln.lstrip().startswith("*"))]


def rule(css: str, selector: str):
    """The declaration block of the rule whose selector is exactly `selector`."""
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{(.*?)^\}", css, flags=re.S)
    return m.group(1) if m else None


def decl(body, prop: str):
    """Value of `prop` inside a rule body, or None."""
    if body is None:
        return None
    m = re.search(r"(?m)^\s*" + re.escape(prop) + r"\s*:\s*([^;]+);", body)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------- predicates
# Shared by the real assertions and by the planted-violation cases, so a check
# that silently matches nothing cannot pass either way.

def collapsed_by_default(css, hidden_sel, open_sel):
    """True when `hidden_sel` hides its body and only `open_sel` reveals it."""
    return (decl(rule(css, hidden_sel), "display") == "none"
            and decl(rule(css, open_sel), "display") == "block")


def tool_cards_start_closed(js):
    """Every tool-card is created without the `open` class."""
    made = [ln for ln in js if re.search(r"className\s*=\s*'tool-card", ln)]
    return bool(made) and all("open" not in ln for ln in made)


def result_is_mono(css):
    fam = decl(rule(css, ".tool-body pre"), "font-family")
    mono = re.search(r"(?m)^\s*--mono:\s*([^;]+);", css)
    return (fam == "var(--mono)"
            and mono is not None and "monospace" in mono.group(1))


def result_written_as_text(js):
    return any(".tool-body pre'" in ln and ".textContent" in ln and "innerHTML" not in ln
               for ln in js)


def reasoning_is_distinct(css):
    """`.reason-body` carries marks the answer's `.msg-content` does not."""
    r, m = rule(css, ".reason-body"), rule(css, ".msg-content")
    if r is None or m is None:
        return False
    return (decl(r, "font-style") == "italic"
            and decl(r, "color") == "var(--muted)"
            and (decl(r, "border-left") or "").startswith("2px")
            and decl(m, "font-style") is None
            and decl(m, "border-left") is None)


def div_depths(fragment):
    """{class: nesting depth} and the open order, for divs in an HTML fragment."""
    depths, order, depth = {}, [], 0
    for token in re.finditer(r"<div\b[^>]*>|</div>", fragment):
        t = token.group(0)
        if t == "</div>":
            depth -= 1
            continue
        cls = re.search(r'class="([^"]+)"', t)
        name = cls.group(1) if cls else "?"
        depths.setdefault(name, depth)
        order.append((name, depth))
        depth += 1
    return depths, order


def reason_not_interleaved(js):
    """In the assistant skeleton, `.reason-block` and `.msg-content` sit at the
    same depth — siblings, not one nested inside the other."""
    skel = next((ln for ln in js if 'class="reason-block"' in ln), None)
    if skel is None:
        return False
    depths, order = div_depths(skel[skel.index("`"):])
    if "reason-block" not in depths or "msg-content" not in depths:
        return False
    names = [n for n, _ in order]
    return (depths["reason-block"] == depths["msg-content"]
            and names.index("msg-content") > names.index("reason-block"))


def reason_never_written_into_answer(js):
    """Nothing that targets `.msg-content` writes reasoning, and nothing that
    targets `.reason-body` writes markup."""
    for ln in js:
        if ".msg-content'" in ln and "reason" in ln:
            return False
        if ".reason-body'" in ln and "innerHTML" in ln:
            return False
    return any(".reason-body'" in ln and "textContent" in ln for ln in js)


def roles_are_separate(css):
    """User and assistant rows differ in avatar paint, role colour, and bubble."""
    ua = decl(rule(css, ".msg.user .avatar"), "background")
    aa = decl(rule(css, ".msg.assistant .avatar"), "background")
    if ua is None or aa is None or ua == aa:
        return False
    base_role = decl(rule(css, ".msg-role"), "color")
    asst_role = decl(rule(css, ".msg.assistant .msg-role"), "color")
    if base_role is None or asst_role is None or base_role == asst_role:
        return False
    return (decl(rule(css, ".msg.user .msg-content"), "background") is not None
            and decl(rule(css, ".msg-content"), "background") is None)


# --------------------------------------------------------------------- tests

class TestToolCardsCollapsed(TestCase):
    def test_collapsed_in_css_and_expandable(self):
        css = css_code()
        self.assertTrue(collapsed_by_default(css, ".tool-body",
                                             ".tool-card.open .tool-body"),
                        "tool cards are not collapsed-by-default in CSS")

    def test_created_without_the_open_class(self):
        self.assertTrue(tool_cards_start_closed(js_lines()))

    def test_open_is_toggled_never_forced(self):
        self.assertNotIn(".tool-card').classList.add('open')", APP.replace("\n", ""))
        self.assertIn("closest('.tool-card').classList.toggle('open')", APP)


class TestResultsMonospace(TestCase):
    def test_result_pre_uses_the_mono_token(self):
        self.assertTrue(result_is_mono(css_code()))

    def test_result_is_written_as_text_not_markup(self):
        self.assertTrue(result_written_as_text(js_lines()))


class TestReasoningCollapsedAndDistinct(TestCase):
    def test_collapsed_in_css_and_expandable(self):
        self.assertTrue(collapsed_by_default(css_code(), ".reason-body",
                                             ".reason-block.open .reason-body"))

    def test_visually_distinct_from_the_answer(self):
        self.assertTrue(reasoning_is_distinct(css_code()))

    def test_toggle_is_delegated_not_per_row(self):
        js = js_lines()
        self.assertTrue(any("closest('.reason-block').classList.toggle('open')" in ln
                            for ln in js))
        self.assertFalse(any(".reason-toggle" in ln and "addEventListener" in ln
                             for ln in js))


class TestReasoningNotInterleaved(TestCase):
    def test_reason_and_answer_are_siblings(self):
        self.assertTrue(reason_not_interleaved(js_lines()))

    def test_no_writer_puts_reasoning_into_the_answer(self):
        self.assertTrue(reason_never_written_into_answer(js_lines()))


class TestRoleSeparation(TestCase):
    def test_user_and_assistant_rows_are_painted_apart(self):
        self.assertTrue(roles_are_separate(css_code()))


class TestGuardHasTeeth(TestCase):
    """Each check fails when its own property is broken — and the untouched
    source still passes it in the same breath, so a predicate that matches
    nothing cannot report a pass either."""

    def test_expanded_tool_card_is_caught(self):
        css = css_code()
        self.assertTrue(collapsed_by_default(css, ".tool-body",
                                             ".tool-card.open .tool-body"))
        broken = css.replace(".tool-body {\n  display: none;",
                             ".tool-body {\n  display: block;", 1)
        self.assertFalse(collapsed_by_default(broken, ".tool-body",
                                              ".tool-card.open .tool-body"))
        self.assertTrue(reasoning_is_distinct(broken),
                        "the tool-card plant leaked into the reasoning check")

    def test_reasoning_without_its_mark_is_caught(self):
        css = css_code()
        self.assertTrue(reasoning_is_distinct(css))
        broken = css.replace("  font-style: italic;\n", "\n", 1)
        self.assertFalse(reasoning_is_distinct(broken))

    def test_nested_reasoning_is_caught(self):
        self.assertTrue(reason_not_interleaved(js_lines()))
        nested = ['  body.innerHTML = `<div class="reason-block">'
                  '<button class="reason-toggle">thinking</button>'
                  '<div class="msg-content"></div></div>`;']
        self.assertFalse(reason_not_interleaved(nested))

    def test_reasoning_written_into_the_answer_is_caught(self):
        self.assertTrue(reason_never_written_into_answer(js_lines()))
        self.assertFalse(reason_never_written_into_answer(
            ["  el.querySelector('.msg-content').innerHTML = md(t.reason);"]))

    def test_card_created_open_is_caught(self):
        self.assertTrue(tool_cards_start_closed(js_lines()))
        self.assertFalse(tool_cards_start_closed(["  card.className = 'tool-card open';"]))

    def test_identical_roles_are_caught(self):
        css = css_code()
        self.assertTrue(roles_are_separate(css))
        broken = css.replace(".msg.assistant .avatar {\n  background: var(--accent);",
                             ".msg.assistant .avatar {\n  background: var(--user-bubble);",
                             1)
        self.assertFalse(roles_are_separate(broken))

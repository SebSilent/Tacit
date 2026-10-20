"""The documentation is a specification, so it gets checked like one.

Three bugs this catches. The first two happened while these docs were being written; the
third was found by a rewrite that shrank a file every doc cites.

**Names that do not exist.** The README's configuration table once listed fourteen
``TACIT_*`` variables the code never reads: ``TACIT_ALLOW_VCS``,
``TACIT_TURN_SOUND``, ``TACIT_AUDIT_FILE``, ``TACIT_MODEL``, the five
``TACIT_LEARN_*``, the three ``TACIT_MCP_*``, and four more. They are gone now, and
the table is generated against the code instead of against intent. A reader types one
into a shell, nothing happens, and the reasonable conclusion is that the knob is
broken, so they give up on a setting that was never there to begin with. The table
was written from intent rather than from ``backend/config.py``, and nothing
compared the two. A code scan is the only way to know, so this does the scan.

The scan found one more species of the same bug: a name written as shorthand,
``TACIT_COMPACT_TAIL_MIN`` / ``_MAX``, where ``_MAX`` is not a variable anybody can
set. So a name counts only when it is spelled in full and read from the environment.

**A check narrower than the promise it makes.** Fourteen of the twenty citations that
named the README by line number pointed past the end of the file after one rewrite took
it from 1,254 lines to 423, and the six that were still in bounds pointed at the wrong
paragraph in all but one case. None of them failed anything, because the pattern that
found citations matched only paths with a directory in front of them, so a bare
``README.md:1113-1116`` was invisible to it, and ``CONTRIBUTING.md``, which is where half
of them live, was not in the list of files to read either. A checker is only as wide as
the text it matches. The two root-level pages are named in the pattern now, a range is
captured instead of its first number alone, and both ends are checked.

**Citations that point nowhere.** Prose that says ``(``backend/memory.py:59``)`` is
a promise the reader can spend five seconds checking. When the file is not there
``backend/memory.py`` does not exist; memory is ``memory_modes.py`` and
``memory_store.py``) the promise is void, and worse, it teaches the reader that
citations in this project's docs are decorative. Same for a line number past the
end of the file: the first one of those I wrote cited ``backend/main.py:1253`` in a
file that is 213 lines long. So every ``path:line`` in a doc is opened here and its
length measured.

The rule these tests enforce is the one the project guide states: if you cannot
point at the line, you do not know the answer, and you do not write the sentence.
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DOCS = ROOT / "docs"

# Where a variable name can be read. Anything outside this set is a name the docs
# invented: installers and run scripts count, because they do set real variables.
CODE_GLOBS = ("backend/**/*.py", "backend/**/*.mjs", "static/**/*.js",
              "scripts/**/*.py", "tests/**/*.py")
CODE_FILES = ("install.sh", "install.ps1", "run.sh", "run.bat")

# CONTRIBUTING.md is in the set because it cites the README by line number, and the
# last README rewrite moved every one of those numbers and nothing noticed.
DOC_GLOBS = ("README.md", "CONTRIBUTING.md", "docs/*.md", "docs/**/*.md")

ENV_RE = re.compile(r"TACIT_[A-Z0-9_]+")
# `backend/config.py:477`, `static/app.js:1181`, `install.ps1:16`, `README.md:13-15` -
# a path in this repo followed by a line number, or by a range of them. Quoted or bare,
# both count. The two root-level pages are named rather than wildcarded: a `*.md`
# catch-all would make every page of prose a citation target and then demand a line
# check against files nobody means to cite by line.
CITE_RE = re.compile(
    r"(?<![\w./-])((?:backend|static|scripts|tests|docs)/[\w./-]+?\.(?:py|js|mjs|css|html|md)"
    r"|README\.md|CONTRIBUTING\.md|[\w-]+\.(?:sh|ps1|bat))"
    r"(?::(\d+)(?:\s*[-\u2013]\s*(\d+))?)?")
# `:93`, the shorthand that repeats the path it follows. The lookbehind is what keeps
# this off prose: a digit or a word character before the colon means it is a ratio, a
# time or a URL scheme, not a line number.
BARE_RE = re.compile(r"(?<![:\w/]):(\d+)(?:\s*[-\u2013]\s*(\d+))?")
LINK_RE = re.compile(r"\]\((?!https?:)([^)#]+)(?:#[\w-]*)?\)")


def _cites(text: str) -> list[tuple[str, str | None, str | None]]:
    """Every (path, first line, last line) a doc cites, with shorthand resolved.

    Three shapes are in use here: `path:477`, a range `path:881-887`, and the shorthand
    `:93`, which names no file at all and inherits the path before it. A checker that
    only matches paths reads the first two and walks straight past the third, so half a
    citation goes unverified while looking verified. Resolution runs per block of prose,
    not per physical line, because these paragraphs are wrapped at a hundred characters
    and the shorthand usually lands on the continuation line. A bare range counts only
    inside a code span, since a sentence colon followed by a number is not a citation.
    """
    out: list[tuple[str, str | None, str | None]] = []
    carry = None
    for raw in text.splitlines():
        line, stripped = raw, raw.strip()
        # A markdown table row is its own unit: `:751` in one row belongs to the path in
        # that row, never to the path in the row above it. Prose is the opposite, since
        # these paragraphs wrap and the shorthand lands on the continuation line.
        table = stripped.startswith("|")
        last_path = None if table else carry
        carry = None
        consumed = []
        for m in CITE_RE.finditer(line):
            rel, a, b = m.group(1), m.group(2), m.group(3)
            last_path = rel
            carry = rel
            out.append((rel, a, b or a))
            if a:
                consumed.append((m.start(2), m.end(2)))
        for m in BARE_RE.finditer(line):
            if any(s <= m.start(1) < e for s, e in consumed):
                continue
            if last_path and line[:m.start()].count("`") % 2:
                out.append((last_path, m.group(1), m.group(2) or m.group(1)))
    return out


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _doc_files() -> list[pathlib.Path]:
    seen: list[pathlib.Path] = []
    for pat in DOC_GLOBS:
        for p in sorted(ROOT.glob(pat)):
            if p.is_file() and p not in seen:
                seen.append(p)
    return seen


def _code_files() -> list[pathlib.Path]:
    seen: list[pathlib.Path] = []
    for pat in CODE_GLOBS:
        for p in sorted(ROOT.glob(pat)):
            if p.is_file() and p not in seen:
                seen.append(p)
    for name in CODE_FILES:
        p = ROOT / name
        if p.is_file() and p not in seen:
            seen.append(p)
    return seen


# Names that appear in prose about their own absence ("there is no TACIT_FOO for
# this"). Each exception is a file plus the name plus why it is allowed to stand;
# an unexplained exception is worse than the bug the test is chasing.
ENV_EXCEPTIONS: dict[tuple[str, str], str] = {}

# Citations to a file that lives in somebody else's repository. The rule is that a
# citation is a promise the reader can check *here*, so an external file has to be
# named, with the reason it cannot be opened, rather than matched by a looser regex.
CITE_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("README.md", "checker.sh"):
        "OpenBench's own checker scripts, quoted as evidence that the agent never "
        "edits them (README's benchmark section). Not a file in this repository.",
}


class TestEnvNamesExist(unittest.TestCase):
    def test_every_named_variable_is_read_by_something(self):
        real = set()
        for p in _code_files():
            real.update(ENV_RE.findall(_read(p)))
        self.assertGreater(len(real), 50,
                           "the code scan found almost nothing; the walk is broken, "
                           "not the docs")
        bad: list[str] = []
        for doc in _doc_files():
            for name in sorted(set(ENV_RE.findall(_read(doc)))):
                if name not in real and (doc.name, name) not in ENV_EXCEPTIONS:
                    bad.append(f"{doc.name}: {name}")
        self.assertEqual(bad, [],
                         "docs name environment variables no code reads:\n  "
                         + "\n  ".join(bad))

    def test_no_variable_is_named_only_in_a_comment(self):
        """A name that appears in code but only in a comment is the same bug.

        ``# TACIT_FOO controls this`` reads like a knob and does nothing, so the
        name has to be *read* by something, not merely written down near something.
        """
        # A name counts as read only on a line that actually reads it out of the
        # environment or the shell: os.environ.get / getenv / environ[] in Python,
        # $env:NAME in PowerShell, ${TACIT_NAME:} or export in sh.
        READS = re.compile(
            r"environ\.get\(|getenv\(|environ\[|os\.environ\b"
            r"|\$env:TACIT_|\$\{TACIT_[A-Z0-9_]+[:-]|\bTACIT_[A-Z0-9_]+="
            # ...and a name handed to a child process through a dict, which is a
            # real use: the turn worker is told which session it belongs to this way.
            r"|[\[\"']TACIT_[A-Z0-9_]+[\"']\]")
        read = set()
        for p in _code_files():
            for line in _read(p).splitlines():
                if "TACIT_" not in line or not READS.search(line):
                    continue
                read.update(ENV_RE.findall(line))
        named = set()
        for doc in _doc_files():
            named.update(ENV_RE.findall(_read(doc)))
        comment_only = sorted(n for n in named if n not in read)
        self.assertEqual(comment_only, [],
                         "docs name variables that only ever appear in a comment "
                         "of code, never read:\n  " + "\n  ".join(comment_only))


class TestCitationsResolve(unittest.TestCase):
    def test_a_shorthand_range_inherits_the_path_before_it(self):
        """The resolution itself, pinned, because binding it wrongly is the bug.

        A shorthand `:93` names no file, so it has to borrow one, and this project
        writes citations in two shapes that borrow differently. Prose wraps at a hundred
        characters and lands the shorthand on the continuation line, so it inherits from
        above. A markdown table row is a sentence of its own, and `:751` in one row
        belongs to the path in that row and to nothing else. Bind a table's shorthand to
        the row above and the checker opens a real file at a line nobody meant, which is
        a worse failure than not checking at all: it reports a broken citation in prose
        that is correct.
        """
        prose = ("Tuning lives in `scripts/check_prose.py:29-60`, `:70-74`. Patterns\n"
                 "that need judgment are warnings (`:16-18`, the tuple is `:76-78`).")
        self.assertEqual(_cites(prose), [
            ("scripts/check_prose.py", "29", "60"),
            ("scripts/check_prose.py", "70", "74"),
            ("scripts/check_prose.py", "16", "18"),
            ("scripts/check_prose.py", "76", "78"),
        ])
        table = ("| `model` | `backend/routers/api.py:734` | the default |\n"
                 "| `thinking` | `backend/routers/api.py:68`, `:751` | reasoning |\n")
        self.assertEqual(_cites(table), [
            ("backend/routers/api.py", "734", "734"),
            ("backend/routers/api.py", "68", "68"),
            ("backend/routers/api.py", "751", "751"),
        ])
        # A ratio, a time and a port are not line numbers.
        self.assertEqual(_cites("the 2:1 split at 10:45 on https://host:8443/api"), [])

    def test_cited_files_exist(self):
        missing = []
        for doc in _doc_files():
            for path, _a, _b in _cites(_read(doc)):
                if (doc.name, path) in CITE_EXCEPTIONS:
                    continue
                if not (ROOT / path).is_file():
                    missing.append(f"{doc.name}: {path}")
        self.assertEqual(missing, [],
                         "docs cite files that are not in the repository:\n  "
                         + "\n  ".join(sorted(set(missing))))

    def test_every_exception_is_still_needed(self):
        """An exception that no longer matches is dead weight, so it fails too.

        A allowlist nobody needs is how a real bug gets waved through six months
        later under the same excuse.
        """
        stale = []
        for doc in _doc_files():
            cited = {p for p, _a, _b in _cites(_read(doc))}
            for (name, path) in CITE_EXCEPTIONS:
                if name == doc.name and path not in cited:
                    stale.append(f"{name}: {path}")
        for (name, path) in CITE_EXCEPTIONS:
            if not any(d.name == name for d in _doc_files()):
                stale.append(f"{name}: the file that carries the exception is gone")
        self.assertEqual(stale, [],
                         "citation exceptions that nothing cites any more, delete them:\n  "
                         + "\n  ".join(sorted(set(stale))))

    def test_cited_lines_are_inside_the_file(self):
        """Both ends of a range, not just the number the old regex captured.

        ``README.md:1113-1116`` survived a rewrite that took the file to 423 lines
        because nothing matched a bare ``README.md`` at all. The fix has to be as wide
        as the mistake: ranges are captured, and either end landing past the end of the
        file fails.
        """
        over = []
        for doc in _doc_files():
            for path, a, b in _cites(_read(doc)):
                if not a:
                    continue
                p = ROOT / path
                if not p.is_file():
                    continue  # the test above reports it
                n = len(_read(p).splitlines())
                if int(b) > n or int(a) > n:
                    span = f"{path}:{a}" if a == b else f"{path}:{a}-{b}"
                    over.append(f"{doc.name}: {span} (file has {n} lines)")
        self.assertEqual(over, [],
                         "docs cite a line past the end of the file:\n  "
                         + "\n  ".join(sorted(set(over))))

    def test_cited_line_is_not_blank(self):
        """A citation to an empty line means the number was never looked at.

        Off-by-a-few is tolerated: a citation may point one line off the code it
        describes if the reader lands in the same function. A blank line two
        streets away means the number was guessed. Both ends of a range are
        checked, since the end is the line a reader stops on.
        """
        blank = []
        for doc in _doc_files():
            for path, a, b in _cites(_read(doc)):
                if not a:
                    continue
                p = ROOT / path
                if not p.is_file():
                    continue
                lines = _read(p).splitlines()
                for n in sorted({int(a), int(b)}):
                    i = n - 1
                    if 0 <= i < len(lines) and not lines[i].strip():
                        blank.append(f"{doc.name}: {path}:{n} is a blank line")
        self.assertEqual(blank, [],
                         "docs cite blank lines, which means nobody opened the "
                         "file:\n  " + "\n  ".join(sorted(set(blank))))


class TestLinksResolve(unittest.TestCase):
    def test_relative_links_point_at_files(self):
        broken = []
        for doc in _doc_files():
            text = _read(doc)
            for target in LINK_RE.findall(text):
                if target.startswith("/") or target == "":
                    continue
                base = ROOT if doc == README else doc.parent
                if not (base / target).is_file():
                    broken.append(f"{doc.name}: ({target})")
        self.assertEqual(broken, [],
                         "docs link to files that do not exist:\n  "
                         + "\n  ".join(sorted(set(broken))))


# The pages the documentation set promises. A missing page is a failure, not a skip:
# three of these were linked from `docs/settings.md` and `docs/install.md` long before
# they existed, and a skip is how that stayed invisible.
REQUIRED_PAGES = (
    "index.md", "install.md", "interface.md", "settings.md", "configuration.md",
    "tools.md", "architecture.md", "cost-and-profiles.md", "isolation-and-audit.md",
    "memory-and-learning.md", "mcp-and-plugins.md", "data-and-privacy.md",
    "testing-and-benchmarks.md",
)


class TestDocSetIsComplete(unittest.TestCase):
    def test_required_pages_exist(self):
        absent = [p for p in REQUIRED_PAGES if not (DOCS / p).is_file()]
        self.assertEqual(absent, [],
                         "the documentation set is missing pages: " + ", ".join(absent))

    def test_every_page_is_reachable_from_the_index(self):
        index = DOCS / "index.md"
        self.assertTrue(index.is_file(),
                        "docs/index.md is the entry point and it is not there")
        text = _read(index)
        orphans = [p.name for p in sorted(DOCS.rglob("*.md"))
                   if p != index and f"]({p.relative_to(DOCS)}".replace("\\", "/")
                                   not in text]
        self.assertEqual(orphans, [],
                         f"pages the index does not mention: {orphans}")

    def test_readme_points_at_the_docs(self):
        text = _read(README)
        self.assertIn("docs/", text,
                      "the README never mentions docs/, so a reader arriving from "
                      "the repository root cannot find them")


class TestPublishedCostFiguresAreMeasured(unittest.TestCase):
    """The README's cost table is a measurement claim, so it gets measured again.

    ``1,889`` tokens for the default profile outlived the tool schemas that grew past
    it, and nothing noticed, because every other number in these docs is a line the
    reader can open while this one was a figure somebody had run once. It is now
    re-derived from `profiles.cost_of` on every run, and a row that disagrees fails
    the suite rather than the reader's wallet.
    """

    def setUp(self):
        """Measure a fresh install, because that is the install the table describes.

        `cost_of` reads the real `~/.tacit` for plugins, MCP servers and memory, so
        measured against this machine it reports whatever this person has installed
        and the README's row is wrong for everybody else. The published figures are
        the built-in profiles on an empty home, so that is the condition measured.
        """
        import tempfile   # noqa: PLC0415
        from backend import config, profiles   # noqa: PLC0415
        from backend.ai import prompts   # noqa: PLC0415
        from tests.helpers import state_paths   # noqa: PLC0415
        self.profiles = profiles
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        keys = state_paths(config)
        orig = {key: getattr(config, key) for key in keys}
        prior_home = config.HOME

        def restore():
            config.HOME = prior_home
            for key, value in orig.items():
                setattr(config, key, value)
        self.addCleanup(restore)
        config.HOME = home
        for key in keys:
            target = home / pathlib.Path(orig[key]).name
            setattr(config, key, target)
            if key.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        self.measured = {name: profiles.cost_of(cfg)
                         for name, cfg in profiles.BUILTIN.items()}
        self.prompt_chars = len(prompts.system_prompt(None, False, chat=False))

    def _readme(self):
        return _read(README)

    # | **minimal** | 7 | none | off | off | ~768 tokens |
    PROFILE_ROW = re.compile(
        r"^\| \*\*(minimal|default)\*\* \| *(\d+) \|.*?~([\d,]+) tokens \|", re.M)
    # | **Tacit, default profile** | **974 chars** | **20** | **6.5 KB** | **~1,914** |
    COMPARISON_ROW = re.compile(
        r"^\| \*\*Tacit, (Minimal|default profile)\*\* \| \*\*(\d+) chars\*\* \| \*\*(\d+)\*\* "
        r"\| \*\*([\d.]+) KB\*\* \| \*\*~([\d,]+)(?: tokens)?\*\* \|", re.M)

    def test_the_profile_table_publishes_what_the_code_measures(self):
        found = {m.group(1): (int(m.group(2)), int(m.group(3).replace(",", "")))
                 for m in self.PROFILE_ROW.finditer(self._readme())}
        self.assertEqual(sorted(found), ["default", "minimal"],
                         "the README's profile table no longer holds the two rows this "
                         "test reads, so the figures it publishes are unchecked")
        bad = []
        for name, (tools, shown) in sorted(found.items()):
            cost = self.measured[name]
            if tools != cost["tool_count"]:
                bad.append(f"{name}: README says {tools} tools, cost_of says "
                           f"{cost['tool_count']}")
            if shown != cost["total"]:
                bad.append(f"{name}: README says ~{shown:,} tokens, cost_of says "
                           f"{cost['total']:,}")
        self.assertEqual(bad, [], "\n  ".join(bad))

    def test_the_comparison_rows_are_the_measured_ones(self):
        """The row the README argues with other agents on, checked cell by cell."""
        import json     # noqa: PLC0415
        from backend import tokens   # noqa: PLC0415
        chars = self.prompt_chars
        rows = {m.group(1): m.groups() for m in self.COMPARISON_ROW.finditer(self._readme())}
        self.assertEqual(sorted(rows), ["Minimal", "default profile"],
                         "the comparison table lost the Tacit rows")
        want = {"Minimal": "minimal", "default profile": "default"}
        bad = []
        for label, (_shown_label, shown_chars, shown_tools, shown_kb, shown_tokens) in rows.items():
            name = want[label]
            cost = self.measured[name]
            size = sum(len(json.dumps(t)) for t in self.profiles._tool_set(
                self.profiles.BUILTIN[name]))
            kb = round(size / 1024, 1)
            checks = ((int(shown_chars), chars, "system prompt chars"),
                      (int(shown_tools), cost["tool_count"], "tool count"),
                      (float(shown_kb), kb, "tool schema KB"),
                      (int(shown_tokens.replace(",", "")), cost["total"], "fixed cost"))
            for got, expected, what in checks:
                if got != expected:
                    bad.append(f"{label}: README {what} is {got}, measured {expected}")
        self.assertEqual(bad, [], "\n  ".join(bad))

    def test_the_prose_figures_are_the_measured_ones(self):
        text = self._readme()
        minimal = self.measured["minimal"]["total"]
        default = self.measured["default"]["total"]
        # The figure is stated twice, once in Key properties and once in the
        # accounting section, and both have to move together or one of them lies.
        hits = text.count(f"{minimal:,} tokens on Minimal and {default:,} on Default")
        self.assertGreaterEqual(hits, 2,
                                f"expected the measured pair in both prose places, saw {hits}")

    def test_the_standing_prompt_length_is_real(self):
        from backend.ai import prompts   # noqa: PLC0415
        chars = len(prompts.system_prompt(None, False, chat=False))
        self.assertIn(f"{chars} characters of standing", self._readme(),
                      f"README publishes a different standing-prompt length than {chars}")

    def test_the_spread_across_profiles_is_the_measured_extremes(self):
        totals = sorted(c["total"] for c in self.measured.values())
        self.assertIn(f"~{totals[0]:,} to ~{totals[-1]:,}", self._readme(),
                      f"the published range is not {totals[0]:,} to {totals[-1]:,}")

    def test_the_docs_page_agrees_with_the_readme(self):
        text = _read(DOCS / "cost-and-profiles.md")
        default = self.measured["default"]["total"]
        self.assertIn(f"{default:,} on Default", text,
                      "docs/cost-and-profiles.md publishes a different default figure")


if __name__ == "__main__":
    unittest.main()

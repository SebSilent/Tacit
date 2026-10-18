"""The documentation is a specification, so it gets checked like one.

Two bugs this catches, both of which actually happened while these docs were being
written.

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

DOC_GLOBS = ("README.md", "docs/*.md", "docs/**/*.md")

ENV_RE = re.compile(r"TACIT_[A-Z0-9_]+")
# `backend/config.py:477`, `static/app.js:1181`, `install.ps1:16` - a path that
# exists in this repo followed by a line number. Quoted or bare, both count.
CITE_RE = re.compile(r"(?<![\w./-])((?:backend|static|scripts|tests|docs)/[\w./-]+?\.(?:py|js|mjs|css|html|md)|[\w-]+\.(?:sh|ps1|bat))(?::(\d+))?")
LINK_RE = re.compile(r"\]\((?!https?:)([^)#]+)(?:#[\w-]*)?\)")


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
    def test_cited_files_exist(self):
        missing = []
        for doc in _doc_files():
            for path, line in CITE_RE.findall(_read(doc)):
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
            cited = {p for p, _ in CITE_RE.findall(_read(doc))}
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
        over = []
        for doc in _doc_files():
            for path, line in CITE_RE.findall(_read(doc)):
                if not line:
                    continue
                p = ROOT / path
                if not p.is_file():
                    continue  # the test above reports it
                n = len(_read(p).splitlines())
                if int(line) > n:
                    over.append(f"{doc.name}: {path}:{line} (file has {n} lines)")
        self.assertEqual(over, [],
                         "docs cite a line past the end of the file:\n  "
                         + "\n  ".join(sorted(set(over))))

    def test_cited_line_is_not_blank(self):
        """A citation to an empty line means the number was never looked at.

        Off-by-a-few is tolerated: a citation may point one line off the code it
        describes if the reader lands in the same function. A blank line two
        streets away means the number was guessed.
        """
        blank = []
        for doc in _doc_files():
            for path, line in CITE_RE.findall(_read(doc)):
                if not line:
                    continue
                p = ROOT / path
                if not p.is_file():
                    continue
                lines = _read(p).splitlines()
                i = int(line) - 1
                if 0 <= i < len(lines) and not lines[i].strip():
                    blank.append(f"{doc.name}: {path}:{line} is a blank line")
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


if __name__ == "__main__":
    unittest.main()

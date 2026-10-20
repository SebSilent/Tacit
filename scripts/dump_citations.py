"""Print every cited source line, so a human can check it against the sentence above it.

``tests/test_docs.py`` proves a citation points at a real file, a line inside it, and a
line that is not blank. It cannot prove the line says what the doc claims it says: a
citation at ``backend/sandbox.py:2-3`` passes all three checks and still points at a
blank line and a docstring title instead of the sentence being quoted. That gap is
closed by reading, not by asserting, and this makes the reading possible at scale: it
prints the doc line, the citation, and the real source lines side by side.

    python scripts/dump_citations.py             # README, CONTRIBUTING and docs/
    python scripts/dump_citations.py docs/tools.md

It handles the three forms a citation takes in these docs: ``path:N``, ``path:N-M``,
and a bare ``:N-M`` that repeats the path it follows on the same line. Exit is 1 when
a citation names a missing file or a line outside it, so this doubles as a check.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The two root-level pages are named rather than wildcarded. `README.md:13-15` is the
# commonest citation in this set and it carries no directory, so a pattern that matched
# only prefixed paths skipped every one of them silently, which is how a README that
# lost 800 lines went on being cited by line numbers that no longer existed.
CITE = re.compile(
    r"(?<![\w./-])"
    r"((?:backend|static|scripts|tests|docs)/[\w./-]+?\.(?:py|js|mjs|css|html|md)"
    r"|README\.md|CONTRIBUTING\.md|[\w-]+\.(?:sh|ps1|bat))"
    r"(?::(\d+)(?:\s*[-\u2013]\s*(\d+))?)?"
)
BARE = re.compile(r"(?<![:\w/]):(\d+)(?:\s*[-\u2013]\s*(\d+))?")

# How many source lines to print before collapsing the rest. A citation spanning a
# whole function is fine; printing sixty lines of it buries the point of the row.
SHOW = 6

_cache: dict[str, list[str] | None] = {}


def _lines(rel: str) -> list[str] | None:
    if rel not in _cache:
        p = ROOT / rel
        _cache[rel] = (p.read_text(encoding="utf-8", errors="replace").splitlines()
                       if p.exists() else None)
    return _cache[rel]


def _doc_files(argv: list[str]) -> list[pathlib.Path]:
    if argv:
        return [ROOT / a for a in argv]
    return [ROOT / "README.md", ROOT / "CONTRIBUTING.md"] + sorted((ROOT / "docs").glob("*.md"))


def _spans(line: str) -> list[tuple[str, int, int]]:
    """Every (path, first, last) a single doc line cites."""
    spans: list[tuple[str, int, int]] = []
    last_path, consumed = None, []
    for m in CITE.finditer(line):
        rel, a, b = m.group(1), m.group(2), m.group(3)
        last_path = rel
        if a:
            spans.append((rel, int(a), int(b or a)))
            consumed.append((m.start(2), m.end(2)))
    # A bare `:N-M` repeats the path before it, but only inside a code span: the
    # same shape appears in prose as a sentence colon followed by a number.
    for m in BARE.finditer(line):
        if any(s <= m.start(1) < e for s, e in consumed):
            continue
        if last_path and line[:m.start()].count("`") % 2:
            spans.append((last_path, int(m.group(1)), int(m.group(2) or m.group(1))))
    return spans


def main(argv: list[str]) -> int:
    problems = 0
    for doc in _doc_files(argv):
        print("=" * 78)
        print(doc.relative_to(ROOT))
        print("=" * 78)
        text = doc.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if line.count("`") < 2:
                continue
            for rel, a, b in _spans(line):
                src = _lines(rel)
                if src is None:
                    print(f"  MISSING FILE  {rel} cited at {doc.name}:{i}")
                    problems += 1
                    continue
                if b > len(src) or a > len(src):
                    print(f"  OUT OF BOUNDS {rel}:{a}-{b} (file is {len(src)} lines) "
                          f"cited at {doc.name}:{i}")
                    problems += 1
                    continue
                print(f"  {doc.name}:{i} cites {rel}:{a}-{b}")
                for n in range(a, min(b, a + SHOW - 1) + 1):
                    print(f"    {rel}:{n}| {src[n - 1]}")
                if b - a >= SHOW:
                    print(f"    ... {b - a - SHOW + 1} more lines")
    print("#" * 78)
    print("PROBLEMS:", problems)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

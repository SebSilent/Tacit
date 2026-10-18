"""The editorial brief, enforced instead of admired.

Documentation here is written to a brief: no em dashes, no hollow adjectives, no
throat-clearing openers. A brief nobody checks is a wish, so this script holds the
denylist and the test suite runs it on every commit, the way the site build at
impeccable.design fails on the same words.

    python scripts/check_prose.py                 # the documentation set
    python scripts/check_prose.py --all           # plus installer and UI strings
    python scripts/check_prose.py docs/install.md # one file

Exit is 0 when nothing is banned, 1 when something is. Patterns the brief says
need human judgment are reported as warnings and never fail the run: a rule that
cries wolf gets ignored inside a week.

Add a rule here when you ban a new pattern. Do not silently allowlist by working
around the regex: if a match is genuinely wrong, say why in `ALLOW` and name the
file, so the exception is auditable.
"""
import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# ---- the denylist -----------------------------------------------------------
# (pattern, why it is banned, what to write instead)
BANNED = [
    (r"\u2014", "em dash", "comma, colon, semicolon, period or parentheses: pick the relationship"),
    (r"&mdash;|&#8212;|&#x2014;", "em dash as an HTML entity", "real punctuation"),
    (r"\s--\s", "double hyphen standing in for an em dash", "real punctuation"),
    (r"\bload-bearing\b", "vague in almost every sentence it appears in", "name the thing it does"),
    (r"\bhighest-leverage\b", "vague claim of impact", "say what specifically pays off"),
    (r"\bbiggest unlock\b", "marketing-speak", "describe the actual change"),
    (r"\breflex defaults\b", "research-notebook jargon", "instincts, first guesses"),
    (r"\bcollapses into monoculture\b", "eval-paper voice", "describe what specifically went wrong"),
    (r"\bdata-driven\b", "empty marketing adjective", "cite the data"),
    (r"\bseamless(ly)?\b", "hollow positive", "say what works without friction"),
    (r"\brobust(ness)?\b", "hollow positive", "cite the failure mode it handles"),
    (r"\belevat(e|es|ed|ing)\b", "marketing verb", "improve, raise, sharpen"),
    (r"\bempower(s|ed|ing)?\b", "marketing verb", "let you"),
    # The tell is the verb, and it arrives with an object: "underscores the point",
    # "underscored that shift". Bare "underscore" is also the name of a character,
    # which prose about sanitising an identifier has to be allowed to say.
    (r"\bunderscor(?:e|es|ed|ing)\s+(?:the|that|this|these|those|how|what|it|a|an)\b",
     "AI tell", "show, make clear"),
    (r"\bpivotal\b", "hollow positive", "central, key, or describe the role"),
    (r"\btapestry\b", "AI scenery noun", "cut it"),
    (r"\bdelve[sd]?|\bdelving\b", "the most-flagged AI tell", "look at, explore, or delete the verb"),
    (r"\bin today.{0,3}s\b", "generic opener", "start at the point"),
    (r"\bgone are the days\b", "cliche opener", "make the point directly"),
    (r"\bwhether you.{0,3}re\b", "audience-pandering, addresses no one", "pick one reader"),
    (r"\b(let.{0,3}s|let us) dive in\b", "throat-clearing", "just start"),
    (r"\bin (this )?(guide|section|document|page)\b", "throat-clearing", "just start"),
    (r"\bin summary\b|\bin conclusion\b", "restates what was just said", "end on the strongest sentence"),
    (r"\bmoreover\b|\bfurthermore\b", "metronome transition", "drop it, or use also"),
    (r"\bdevelopers? should consider\b", "talks down the reader", "say what you would do"),
    (r"\bit.{0,3}s worth noting\b", "delays the point", "note it directly"),
]

# Patterns the brief says need judgment. Reported, never failed.
WARNINGS = [
    (r"\b(?:it|that|this) is not (?:just|merely) [^.\n]{2,60}, (?:it|that) is\b",
     "negation pivot: make the positive claim directly"),
    (r"\b\w+, \w+, and \w+\.\s*\n",
     "possible triadic list: vary the count"),
]

# Files the documentation set covers.
DOC_SET = ["README.md", "CONTRIBUTING.md", "docs"]

# Extra places checked by --all: strings a person actually reads.
STRINGS = ["install.sh", "install.ps1", "run.sh", "run.bat", "static"]

ALLOW = {
    # ("docs/install.md", 12, "double hyphen standing in for an em dash"),
}


def files_for(paths, all_strings=False):
    out = []
    targets = paths or list(DOC_SET) + (STRINGS if all_strings else [])
    for t in targets:
        p = (ROOT / t) if not pathlib.Path(t).is_absolute() else pathlib.Path(t)
        if p.is_dir():
            out += sorted(x for x in p.rglob("*.md") if ".venv" not in x.parts)
            if all_strings:
                out += sorted(x for x in p.rglob("*.js") if ".venv" not in x.parts)
        elif p.is_file():
            out.append(p)
    return [f for f in out if f.suffix in (".md", ".sh", ".ps1", ".bat", ".js")]


def scan(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    hits, warns = [], []
    rel = path.relative_to(ROOT).as_posix()
    for i, line in enumerate(text.splitlines(), 1):
        for pattern, why, instead in BANNED:
            if re.search(pattern.replace("\\u2014", "\u2014"), line, re.I):
                if (rel, i, why) in ALLOW:
                    continue
                hits.append((rel, i, why, instead, line.strip()[:120]))
        for pattern, advice in WARNINGS:
            if re.search(pattern, line, re.I):
                warns.append((rel, i, advice, line.strip()[:120]))
    return hits, warns


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", help="files or folders to check")
    ap.add_argument("--all", action="store_true", help="also check installer and UI strings")
    ap.add_argument("--quiet", action="store_true", help="only report the counts")
    args = ap.parse_args()

    hits, warns = [], []
    checked = 0
    for f in files_for(args.paths, args.all):
        checked += 1
        h, w = scan(f)
        hits += h
        warns += w

    if not args.quiet:
        for rel, i, why, instead, snippet in hits:
            print(f"{rel}:{i}: banned: {why}\n    use instead: {instead}\n    | {snippet}")
        for rel, i, advice, snippet in warns:
            print(f"{rel}:{i}: warning: {advice}\n    | {snippet}")

    print(f"\n{checked} files checked: {len(hits)} banned, {len(warns)} warning")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())

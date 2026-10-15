#!/usr/bin/env python3
"""Bump Tacit's version: the VERSION file at the repo root, and package.json to match.

Tacit's version is `VERSION` — one extensionless file, read by exactly one function
(`backend/autoupdater.py::local_version`) and, since the update transport started
ordering versions, compared by `autoupdater.compare_versions`. `package.json` used to
carry its own number: that file is the optional Playwright driver's manifest, not
Tacit's release, and the drift meant the driver claimed 1.0.0 while Tacit claimed a
date. They are now one identity, written together or not at all.

SemVer only, and the script refuses anything else on BOTH sides of the write:

  * the new value must parse as MAJOR.MINOR.PATCH with an optional -prerelease and
    +build, because a value the transport cannot parse is silently ordered as
    "older than every release", which is a wrong answer rather than an error;
  * the value being replaced must also be valid SemVer — with one exception, the
    legacy date (YYYY-MM-DD) that this transport wrote before SemVer. That is the
    only migration this script performs, so the first
    `python scripts/bump_version.py 1.0.0` on a date-versioned checkout works and a
    later run on top of a hand-edited VERSION does not.

It writes files and prints what it did. It runs no version control: a commit is the
operator's decision, and the update transport's whole contract is that Tacit never
shells out to git on your behalf.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
PACKAGE_FILE = ROOT / "package.json"

# The same grammar autoupdater.parse_version accepts. Duplicated on purpose and
# noted in both files: this script must refuse at write time what the transport
# cannot order at read time, and the two answers have to be the same answer.
SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?$")
# What VERSION held before SemVer. Accepted only as the value being replaced.
LEGACY_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def die(msg: str) -> "None":
    print(f"refused: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bump Tacit's VERSION (and package.json) to a SemVer value.")
    ap.add_argument("version", help="the new version, e.g. 1.2.0 or 1.2.0-rc.1")
    ap.add_argument("--dry-run", action="store_true", help="validate and report, write nothing")
    args = ap.parse_args(argv)

    new = args.version.strip()
    if not SEMVER.match(new):
        die(f"{new!r} is not SemVer — expected MAJOR.MINOR.PATCH with optional "
            f"-prerelease and +build (e.g. 1.2.0, 1.2.0-rc.1)")

    if not VERSION_FILE.is_file():
        die(f"{VERSION_FILE} is not there to bump — create it with the new value by hand "
            f"only if this install predates version tracking")
    old = VERSION_FILE.read_text(encoding="utf-8").strip()
    if old != new and not (SEMVER.match(old) or LEGACY_DATE.match(old)):
        die(f"the version being replaced, {old!r}, is neither SemVer nor the legacy date — "
            f"VERSION has been edited by hand and this script will not guess what it meant")

    pkg_line = None
    if PACKAGE_FILE.is_file():
        text = PACKAGE_FILE.read_text(encoding="utf-8")
        m = re.search(r'^([ \t]*)"version":\s*"([^"]*)"', text, re.M)
        if not m:
            die(f"{PACKAGE_FILE.name} has no \"version\" line to sync")
        pkg_line = (text, m)

    if args.dry_run:
        print(f"would write VERSION: {old or '(empty)'} -> {new}")
        if pkg_line:
            print(f"would write package.json: \"version\": \"{pkg_line[1].group(2)}\" -> \"{new}\"")
        return 0

    VERSION_FILE.write_text(new + "\n", encoding="utf-8")
    print(f"VERSION: {old or '(empty)'} -> {new}")
    if pkg_line:
        text, m = pkg_line
        PACKAGE_FILE.write_text(text[:m.start(2)] + new + text[m.end(2):], encoding="utf-8")
        print(f"package.json: \"{m.group(2)}\" -> \"{new}\"")
    print("Nothing was committed, and the running server still holds its loaded code — "
          "Settings > General > Restart server applies a file the update transport wrote.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

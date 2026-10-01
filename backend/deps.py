"""Optional dependencies, offered and never imposed.

Tacit core runs on the standard library plus FastAPI, uvicorn and httpx. Nothing
else is required, and nothing else is installed on your behalf.

Some features can do more when a system tool is present. This module is how you
find out which ones, what they would add, and the exact command to get them. It
detects, it explains, and it stops there. Running an installer is your decision
and your action; `install_command()` returns a string for you to read.

A feature whose dependency is missing is blocked with a reason. It is never
silently downgraded and it never half-works.
"""

from __future__ import annotations

import shutil
import sys

# Each group declares what it needs, what it unlocks, and how to get it.
GROUPS = {
    "isolation-advanced": {
        "label": "Advanced isolation",
        "why": "Read-only project mounts, a private temporary directory, a network "
               "namespace, and a process tree that dies with the command.",
        "unlocks": "sandbox backends beyond timeout-and-report",
        "needs": {
            "linux": [("bwrap", "sudo apt install bubblewrap   # or: sudo dnf install bubblewrap")],
            "darwin": [("sandbox-exec", "already present on macOS; nothing to install")],
            "windows": [],
        },
        "note": "Windows has no equivalent primitive in the base system. Tacit says so "
                "rather than pretending.",
    },
    "container-isolation": {
        "label": "Container isolation",
        "why": "Run commands inside a container you already trust.",
        "unlocks": "the container sandbox backend",
        "needs": {
            "linux": [("docker", "install Docker, or Podman, and put it on PATH")],
            "darwin": [("docker", "install Docker Desktop, or Podman, and put it on PATH")],
            "windows": [("docker", "install Docker Desktop, or Podman, and put it on PATH")],
        },
        "note": "Never required. Selected explicitly or not at all.",
    },
    "browser-automation": {
        "label": "Browser automation",
        "why": "Drive a real Chromium for pages that need JavaScript.",
        "unlocks": "the browser tool",
        "needs": {
            "linux": [("node", "install Node 22 or newer, then: npm install")],
            "darwin": [("node", "install Node 22 or newer, then: npm install")],
            "windows": [("node", "install Node 22 or newer, then: npm install")],
        },
        "note": "Around 130 MB. Plain pages are faster with fetch, which needs nothing.",
    },
    "memory-rich": {
        "label": "Rich memory",
        "why": "Full-text search over your saved notes.",
        "unlocks": "everything Tacit's memory does today",
        "needs": {"linux": [], "darwin": [], "windows": []},
        "note": "Built in. SQLite ships with Python, and FTS5 is used when the build "
                "has it, falling back to plain matching when it does not.",
    },
    "learning-autonomous": {
        "label": "Autonomous learning",
        "why": "Apply low-risk proposals without asking.",
        "unlocks": "the auto learning modes",
        "needs": {"linux": [], "darwin": [], "windows": []},
        "note": "Built in, and still off until you choose it.",
    },
    "migration-importers": {
        "label": "Migration importers",
        "why": "Read a file or folder you exported from another tool, once.",
        "unlocks": "manual import",
        "needs": {"linux": [], "darwin": [], "windows": []},
        "note": "Built in. Nothing is scanned or detected, and no external tool is "
                "needed to read your own export.",
    },
}


def platform_key() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def _missing(group: dict) -> list[dict]:
    key = platform_key()
    out = []
    for binary, how in group["needs"].get(key, []):
        if how.startswith("already present") or shutil.which(binary):
            continue
        out.append({"binary": binary, "install": how})
    return out


def check(name: str) -> dict:
    """What one group needs, and whether it is satisfied right now."""
    group = GROUPS.get(name)
    if group is None:
        return {"ok": False, "error": f"no dependency group '{name}'"}
    missing = _missing(group)
    return {"ok": True, "id": name, "label": group["label"], "why": group["why"],
            "unlocks": group["unlocks"], "note": group["note"],
            "missing": missing, "satisfied": not missing,
            "install_command": "\n".join(m["install"] for m in missing)}


def status() -> dict:
    """Every group, the current platform, and what is missing."""
    rows = [check(name) for name in GROUPS]
    return {"platform": platform_key(),
            "groups": [r for r in rows if r.get("ok")],
            "unsatisfied": [r["id"] for r in rows if r.get("ok") and not r["satisfied"]],
            "note": "Tacit installs nothing on its own. Every command here is yours to run."}


def install_command(name: str) -> str:
    """The exact command for a group, for you to read and run yourself."""
    got = check(name)
    if not got.get("ok"):
        return ""
    return got["install_command"]


def run_install(name: str) -> dict:
    """Deliberately not implemented as an automatic action.

    Returning the command rather than executing it is the whole design. An
    installer that runs itself from a coding agent is a bad default, so it is not
    a default at all.
    """
    got = check(name)
    if not got.get("ok"):
        return {"ok": False, "error": got["error"]}
    if got["satisfied"]:
        return {"ok": True, "already_satisfied": True, "command": ""}
    return {"ok": False, "command": got["install_command"],
            "error": "run this yourself in a terminal. Tacit does not install packages "
                     "on your behalf."}

"""Autoupdater — checks Git for changes and pulls them.

Tacit is an open-source project with no releases; every push to the default
branch is an update. The autoupdater uses git commands (fetch/diff/pull)
to verify and apply updates, similar to how Tacit is installed.
"""
import asyncio
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from . import config
from . import vcs

REPO_OWNER = "SebSilent"
REPO_NAME = "Tacit"
DEFAULT_BRANCH = "master"
REMOTE_NAME = "origin"

# Files that should never be auto-updated (user config, local data)
PROTECTED_PATHS = {
    ".env",
    "models.json",
    "prefs.json",
    "github.json",
    "plugins.json",
    "mcp.json",
    "profiles.json",
    "folders.json",
    "capabilities.json",
    "audit.jsonl",
    "learning.json",
    "memory.db",
    "evidence.jsonl",
    "benchmarks.json",
    "tacit.pid",
    "restart.json",
    "restart.log",
    "sessions/",
    "skills/",
    "knowledge/",
    "plans/",
    "checkpoints/",
    "adapters/",
    "plugins/",
    "bridges/",
    "node_modules/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    "dist/",
    "build/",
    ".venv/",
    "venv/",
}

# Files we track for updates (source code, static assets, install scripts)
TRACKED_EXTENSIONS = {".py", ".js", ".css", ".html", ".md", ".txt", ".sh", ".ps1", ".bat", ".json", ".toml", ".yaml", ".yml"}


def _is_protected(path: Path) -> bool:
    """Check if a path is protected from auto-updates."""
    rel = str(path).replace("\\", "/")
    for prot in PROTECTED_PATHS:
        if rel == prot or rel.startswith(prot.rstrip("/") + "/"):
            return True
    return False


def _is_tracked(path: Path) -> bool:
    """Check if a file extension is tracked for updates."""
    return path.suffix.lower() in TRACKED_EXTENSIONS


def _file_hash(path: Path) -> str:
    """Compute SHA256 hash of a file."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _git_repo_root() -> Path:
    """Get the git repository root."""
    return config.ROOT


def _run_git(args: list[str], cwd: Path | None = None) -> dict:
    """Run a git command and return result dict."""
    workdir = cwd or _git_repo_root()
    return vcs.run_argv([vcs.BIN, *args], str(workdir))


async def _run_git_async(args: list[str], cwd: Path | None = None) -> dict:
    """Run a git command asynchronously."""
    workdir = cwd or _git_repo_root()
    return await asyncio.to_thread(vcs.run_argv, [vcs.BIN, *args], str(workdir))


def load_state() -> dict:
    """Load autoupdater state from prefs."""
    prefs = config.prefs()
    return prefs.get("autoupdater", {
        "enabled": True,
        "last_check": 0,
        "last_commit": "",
        "last_update": 0,
        "auto_check_interval": 3600,  # 1 hour
    })


def save_state(state: dict) -> None:
    """Save autoupdater state to prefs."""
    config.save_prefs({"autoupdater": state})


async def check_for_updates() -> dict:
    """Check git remote for updates. Returns info about available updates."""
    state = load_state()
    repo_root = _git_repo_root()

    # Ensure we have a git repo
    if not (repo_root / ".git").exists():
        return {"ok": False, "error": "Not a git repository"}

    # Fetch latest from remote
    fetch_result = await _run_git_async(["fetch", REMOTE_NAME])
    if not fetch_result.get("ok"):
        return {"ok": False, "error": f"git fetch failed: {fetch_result.get('stderr', 'unknown error')}"}

    # Get current local commit
    local_commit_result = await _run_git_async(["rev-parse", "HEAD"])
    if not local_commit_result.get("ok"):
        return {"ok": False, "error": f"Failed to get local commit: {local_commit_result.get('stderr')}"}
    local_sha = local_commit_result.get("stdout", "").strip()

    # Get remote commit
    remote_ref = f"{REMOTE_NAME}/{DEFAULT_BRANCH}"
    remote_commit_result = await _run_git_async(["rev-parse", remote_ref])
    if not remote_commit_result.get("ok"):
        return {"ok": False, "error": f"Failed to get remote commit: {remote_commit_result.get('stderr')}"}
    remote_sha = remote_commit_result.get("stdout", "").strip()

    if not remote_sha:
        return {"ok": False, "error": "No remote commit SHA returned"}

    # If we're already on this commit, no update needed
    if state.get("last_commit") == remote_sha or local_sha == remote_sha:
        return {
            "ok": True,
            "update_available": False,
            "current_commit": local_sha,
            "latest_commit": remote_sha,
            "message": "Already up to date",
        }

    # Get commit message for the remote commit
    commit_msg_result = await _run_git_async(["log", "-1", "--pretty=format:%s", remote_sha])
    commit_msg = commit_msg_result.get("stdout", "").strip() if commit_msg_result.get("ok") else ""

    commit_date_result = await _run_git_async(["log", "-1", "--pretty=format:%cI", remote_sha])
    commit_date = commit_date_result.get("stdout", "").strip() if commit_date_result.get("ok") else ""

    # Get list of changed files between local and remote
    diff_result = await _run_git_async(["diff", "--name-only", f"{local_sha}..{remote_sha}"])
    if not diff_result.get("ok"):
        return {"ok": False, "error": f"git diff failed: {diff_result.get('stderr')}"}

    changed_paths = [p.strip() for p in diff_result.get("stdout", "").split("\n") if p.strip()]

    # Also check for new files (in remote but not in local)
    # This is covered by diff --name-only for new files too

    changed_files = []
    new_files = []

    for path in changed_paths:
        # Skip protected paths
        if _is_protected(Path(path)):
            continue

        # Only track relevant extensions
        if not _is_tracked(Path(path)):
            continue

        local_path = repo_root / path

        # Get the blob SHA from remote for this file
        blob_result = await _run_git_async(["ls-tree", remote_sha, path])
        remote_blob_sha = ""
        if blob_result.get("ok"):
            parts = blob_result.get("stdout", "").split()
            if len(parts) >= 3:
                remote_blob_sha = parts[2]

        if local_path.exists():
            local_hash = _file_hash(local_path)
            if local_hash != remote_blob_sha:
                changed_files.append({
                    "path": path,
                    "local_hash": local_hash[:16] if local_hash else "missing",
                    "remote_hash": remote_blob_sha[:16] if remote_blob_sha else "unknown",
                    "size": local_path.stat().st_size if local_path.exists() else 0,
                })
        else:
            new_files.append({
                "path": path,
                "remote_hash": remote_blob_sha[:16] if remote_blob_sha else "unknown",
                "size": 0,
            })

    has_changes = bool(changed_files or new_files)

    return {
        "ok": True,
        "update_available": has_changes,
        "current_commit": local_sha,
        "latest_commit": remote_sha,
        "commit_message": commit_msg,
        "commit_date": commit_date,
        "changed_files": changed_files,
        "new_files": new_files,
        "changed_count": len(changed_files),
        "new_count": len(new_files),
    }


async def pull_updates(dry_run: bool = False) -> dict:
    """Pull updates from git remote. Returns result of the update operation."""
    state = load_state()
    repo_root = _git_repo_root()

    # First check what's available
    check = await check_for_updates()
    if not check.get("ok"):
        return check
    if not check.get("update_available"):
        return {"ok": True, "updated": False, "message": "Already up to date"}

    latest_sha = check["latest_commit"]
    changed_files = check.get("changed_files", [])
    new_files = check.get("new_files", [])

    all_files = changed_files + new_files
    if not all_files:
        return {"ok": True, "updated": False, "message": "No files to update"}

    if dry_run:
        return {
            "ok": True,
            "updated": False,
            "dry_run": True,
            "would_update": len(all_files),
            "files": all_files,
        }

    # Pull the changes
    pull_result = await _run_git_async(["pull", REMOTE_NAME, DEFAULT_BRANCH])
    if not pull_result.get("ok"):
        return {"ok": False, "error": f"git pull failed: {pull_result.get('stderr')}"}

    # Update state
    state["last_commit"] = latest_sha
    state["last_update"] = time.time()
    state["last_check"] = time.time()
    save_state(state)

    return {
        "ok": True,
        "updated": True,
        "updated_files": [f["path"] for f in all_files],
        "failed_files": [],
        "commit": latest_sha,
        "message": f"Updated {len(all_files)} file(s) via git pull",
    }


async def auto_check_if_needed() -> dict | None:
    """Run an automatic check if enough time has passed since last check."""
    state = load_state()
    if not state.get("enabled", True):
        return None

    interval = state.get("auto_check_interval", 3600)
    last_check = state.get("last_check", 0)
    now = time.time()

    if now - last_check < interval:
        return None

    # Update last_check time to avoid repeated checks
    state["last_check"] = now
    save_state(state)

    # Run check in background
    return await check_for_updates()


def get_status() -> dict:
    """Get current autoupdater status."""
    state = load_state()
    return {
        "enabled": state.get("enabled", True),
        "last_check": state.get("last_check", 0),
        "last_commit": state.get("last_commit", ""),
        "last_update": state.get("last_update", 0),
        "auto_check_interval": state.get("auto_check_interval", 3600),
    }


def set_enabled(enabled: bool) -> dict:
    """Enable or disable the autoupdater."""
    state = load_state()
    state["enabled"] = bool(enabled)
    save_state(state)
    return {"ok": True, "enabled": state["enabled"]}


def set_check_interval(seconds: int) -> dict:
    """Set the auto-check interval in seconds."""
    state = load_state()
    state["auto_check_interval"] = max(60, int(seconds))  # minimum 1 minute
    save_state(state)
    return {"ok": True, "auto_check_interval": state["auto_check_interval"]}
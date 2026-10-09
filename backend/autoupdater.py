"""Autoupdater — plain HTTPS downloads from GitHub. No git, no API keys.

Tacit is an open-source project with no releases; every push to the default
branch is an update. The transport is two plain GETs: the remote VERSION file
(a few bytes — the cheap check), and, when it differs from the local one, the
branch tarball, which is unpacked over the checkout honouring the protected
paths and the tracked-extension filter. An archive install updates exactly
the way it was installed. If the checkout happens to be a git repository it
is left entirely alone — no fetch, no pull, no dirty-tree complaints; the
archive copy is the only mechanism, and "version control is a human action"
is why this module never shells out to anything.

What an update may touch is narrower than what git would: user state is
never touched (PROTECTED_PATHS), only tracked source extensions are
considered, and a file the upstream tree no longer contains is reported —
never deleted — because the archive cannot distinguish "upstream removed
this" from "the user added this".
"""
import asyncio
import hashlib
import io
import os
import tarfile
import time
from pathlib import Path, PurePosixPath

import httpx

from . import config

REPO_OWNER = "SebSilent"
REPO_NAME = "Tacit"
DEFAULT_BRANCH = "master"
VERSION_URL = (f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/"
               f"{DEFAULT_BRANCH}/VERSION")
TARBALL_URL = (f"https://codeload.github.com/{REPO_OWNER}/{REPO_NAME}/"
               f"tar.gz/refs/heads/{DEFAULT_BRANCH}")

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

# Directories that are machinery wherever they appear; pruned during the
# vanished-file walk so a big checkout (.venv alone is thousands of files)
# is not traversed at all.
_TECHNICAL_DIRS = {".venv", "venv", "node_modules", "__pycache__",
                   ".pytest_cache", ".mypy_cache", "dist", "build", ".git"}
# Directory-form protected paths are top-level state dirs; they are pruned
# at the root of the walk only (backend/plugins is code and must survive).
_STATE_DIRS = {p.rstrip("/") for p in PROTECTED_PATHS if p.endswith("/")}


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


def _local_version() -> str:
    """The checkout's VERSION file, stripped. Empty when the install predates
    version tracking — the check then says so instead of guessing."""
    try:
        return (config.ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    """The one seam every fetched byte comes through; the tests stub this."""
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


async def _http_get_async(url: str, timeout: float = 30.0) -> bytes:
    return await asyncio.to_thread(_http_get, url, timeout)


def load_state() -> dict:
    """Load autoupdater state from prefs."""
    prefs = config.prefs()
    state = prefs.get("autoupdater", {})
    # Installs from the git era kept the last commit sha under last_commit;
    # the version transport renamed the field — carry the old value across.
    if "last_version" not in state and state.get("last_commit"):
        state["last_version"] = state["last_commit"]
    state.setdefault("enabled", True)
    state.setdefault("last_check", 0)
    state.setdefault("last_version", "")
    state.setdefault("last_update", 0)
    state.setdefault("auto_check_interval", 3600)  # 1 hour
    return state


def save_state(state: dict) -> None:
    """Save autoupdater state to prefs."""
    config.save_prefs({"autoupdater": state})


def _walk_tracked() -> list[Path]:
    """Tracked, unprotected files in the checkout, machinery dirs pruned."""
    out = []
    root = config.ROOT
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if d not in _TECHNICAL_DIRS]
        if rel_dir == Path("."):
            dirnames[:] = [d for d in dirnames if d not in _STATE_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = p.relative_to(root)
            if _is_protected(rel) or not _is_tracked(rel):
                continue
            out.append(p)
    return out


def _apply_archive(data: bytes, dry_run: bool) -> dict:
    """Unpack the branch tarball over the checkout.

    One pass enumerates and (unless dry_run) writes: changed files are
    overwritten, new files created, protected paths and untracked extensions
    skipped, and local files the archive no longer contains are reported —
    never deleted, because the archive cannot tell "upstream removed this"
    from "the user added this".
    """
    changed: list[dict] = []
    new: list[dict] = []
    failed: list[str] = []
    skipped_protected: list[str] = []
    upstream: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = PurePosixPath(member.name).parts
            # Zip-slip guard: an archive entry must never escape the checkout.
            if not parts or ".." in parts or member.name.startswith(("/", "\\")) \
                    or (len(parts[0]) >= 2 and parts[0][1] == ":"):
                continue
            if len(parts) < 2:
                continue          # the archive's own root directory
            rel = "/".join(parts[1:])
            if _is_protected(Path(rel)):
                skipped_protected.append(rel)
                continue
            # VERSION is the one extensionless file the transport itself
            # depends on: the archive's copy is how the checkout learns its
            # new version, so it is always tracked.
            if rel != "VERSION" and not _is_tracked(Path(rel)):
                continue
            upstream.add(rel)
            local = config.ROOT.joinpath(*parts[1:])
            payload = tar.extractfile(member).read()
            digest = hashlib.sha256(payload).hexdigest()
            if local.exists():
                if _file_hash(local) == digest:
                    continue      # identical already — not an update
                entry = {"path": rel, "local_hash": (_file_hash(local) or "unreadable")[:16],
                         "remote_hash": digest[:16], "size": len(payload)}
                if not dry_run:
                    try:
                        local.parent.mkdir(parents=True, exist_ok=True)
                        local.write_bytes(payload)
                    except Exception:
                        failed.append(rel)
                        continue
                changed.append(entry)
            else:
                entry = {"path": rel, "remote_hash": digest[:16], "size": len(payload)}
                if not dry_run:
                    try:
                        local.parent.mkdir(parents=True, exist_ok=True)
                        local.write_bytes(payload)
                    except Exception:
                        failed.append(rel)
                        continue
                new.append(entry)
    removed_upstream = [p.relative_to(config.ROOT).as_posix()
                        for p in _walk_tracked() if p.relative_to(config.ROOT).as_posix() not in upstream]
    return {"changed": changed, "new": new, "failed": failed,
            "skipped_protected": skipped_protected,
            "removed_upstream": removed_upstream}


async def check_for_updates() -> dict:
    """Check the remote VERSION against the local one.

    Same-version checks cost one small GET. A differing version costs one
    more GET (the tarball) so the answer can say what would change — the
    same shape the git transport reported, sourced from the archive.
    """
    local = _local_version()
    if not local:
        return {"ok": False,
                "error": "no local VERSION file — this install predates version tracking"}
    try:
        remote = (await _http_get_async(VERSION_URL)).decode("utf-8").strip()
    except Exception as e:
        return {"ok": False, "error": f"VERSION fetch failed: {e}"}
    if not remote:
        return {"ok": False, "error": "the remote VERSION file is empty"}

    if remote == local:
        return {
            "ok": True,
            "update_available": False,
            "current_version": local,
            "latest_version": remote,
            "note": "Already up to date",
        }

    try:
        data = await _http_get_async(TARBALL_URL)
    except Exception as e:
        return {"ok": False, "error": f"archive download failed: {e}"}
    plan = _apply_archive(data, dry_run=True)
    return {
        "ok": True,
        "update_available": True,
        "current_version": local,
        "latest_version": remote,
        "note": f"version {local} → {remote}",
        "changed_files": plan["changed"],
        "new_files": plan["new"],
        "removed_upstream": plan["removed_upstream"],
        "changed_count": len(plan["changed"]),
        "new_count": len(plan["new"]),
    }


async def pull_updates(dry_run: bool = False) -> dict:
    """Apply the update: download the tarball, unpack over the checkout.

    One download per pull — the check's archive was enumerated, not kept.
    """
    local = _local_version()
    if not local:
        return {"ok": False,
                "error": "no local VERSION file — this install predates version tracking"}
    try:
        remote = (await _http_get_async(VERSION_URL)).decode("utf-8").strip()
    except Exception as e:
        return {"ok": False, "error": f"VERSION fetch failed: {e}"}
    if not remote or remote == local:
        return {"ok": True, "updated": False, "message": "Already up to date"}

    try:
        data = await _http_get_async(TARBALL_URL)
    except Exception as e:
        return {"ok": False, "error": f"archive download failed: {e}"}
    plan = _apply_archive(data, dry_run=True)

    if dry_run:
        return {
            "ok": True,
            "updated": False,
            "dry_run": True,
            "would_update": len(plan["changed"]) + len(plan["new"]),
            "files": plan["changed"] + plan["new"],
            "removed_upstream": plan["removed_upstream"],
        }

    plan = _apply_archive(data, dry_run=False)
    state = load_state()
    state["last_version"] = remote
    state["last_update"] = time.time()
    state["last_check"] = time.time()
    save_state(state)

    message = f"Updated {len(plan['changed']) + len(plan['new'])} file(s) to version {remote}"
    if plan["removed_upstream"]:
        message += (f"; {len(plan['removed_upstream'])} file(s) no longer upstream, "
                    "left on disk")
    if plan["failed"]:
        message += f"; {len(plan['failed'])} file(s) failed to write"
    return {
        "ok": True,
        "updated": True,
        "updated_files": [e["path"] for e in plan["changed"] + plan["new"]],
        "failed_files": plan["failed"],
        "removed_upstream": plan["removed_upstream"],
        "version": remote,
        "message": message,
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
        "last_version": state.get("last_version", ""),
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
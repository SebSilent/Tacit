"""Folders the user has actually used, so a picker does not start from scratch.

Choosing a directory every single time is the kind of small friction that makes
a tool tiring. Any folder that gets used is remembered here, most recent first,
and the list is the user's own: anything on it can be removed from the
interface, and nothing is ever added that they did not point at.

Stored under ``~/.tacit/folders.json``.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import config

MAX = 12


def _key(path: str) -> str:
    """Identity for a folder, insensitive to case and to separator style.

    Windows accepts both ``C:/x`` and ``C:\\x`` for the same place, and users
    type both, so the key normalises separators before comparing.
    """
    raw = _clean(path)
    if not raw:
        return ""
    return str(Path(raw)).replace(chr(92), "/").rstrip("/").lower()


def _clean(path: str) -> str:
    """A tidy absolute-looking path, without forcing it to exist yet."""
    raw = str(path or "").strip().strip('"')
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser())
    except Exception:  # noqa: BLE001
        return raw


def load() -> list[dict]:
    data = config.read_json(config.FOLDERS_FILE, [])
    if not isinstance(data, list):
        return []
    rows = []
    for item in data:
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            continue
        path = _clean(item.get("path") or "")
        if not path:
            continue
        rows.append({"path": path, "name": Path(path).name or path,
                     "used": float(item.get("used") or 0)})
    rows.sort(key=lambda r: r["used"], reverse=True)
    return rows[:MAX]


def _save(rows: list[dict]) -> None:
    config.write_json(config.FOLDERS_FILE, rows[:MAX])


def list_folders() -> list[dict]:
    """Saved folders, most recently used first."""
    return load()


def add(path: str) -> dict:
    """Remember a folder. Existing entries move to the top rather than duplicating."""
    clean = _clean(path)
    if not clean:
        return {"ok": False, "error": "a folder path is required"}
    rows = [r for r in load() if _key(r["path"]) != _key(clean)]
    rows.insert(0, {"path": clean, "name": Path(clean).name or clean,
                    "used": round(time.time(), 3)})
    _save(rows)
    return {"ok": True, "folders": load()}


def remove(path: str) -> dict:
    target = _key(path)
    rows = load()
    kept = [r for r in rows if _key(r["path"]) != target]
    if len(kept) == len(rows):
        return {"ok": False, "error": "that folder is not in the list"}
    _save(kept)
    return {"ok": True, "folders": load()}


def clear() -> dict:
    _save([])
    return {"ok": True, "folders": []}

"""The frozen archive: every row a compaction folds away, kept verbatim.

A compaction is destructive by design: it replaces a region of the transcript
with a summary and drops the rows it described. The summary is written for the
model's next request and is therefore short, lossy and unaddressable — the
model cannot ask it a question, and the person cannot read the work back from
it. This module is the other half of that trade: before a fold destroys rows,
they are appended here, to a sidecar that is never summarised, never pruned
and never loaded during a turn.

The file is append-only JSONL, one line per stored row, beside the session it
belongs to. Nothing reads it during a turn except the `session_archive` tool,
and that tool is off by default: an agent that can page through its own past
is a capability the person opts into, not one that arrives with the update.

Deletion is the last word, as everywhere else in the store: removing a session
removes its archive with it. An archive that outlived its session would be a
resurrection path with a transcript attached.
"""
import json
import threading
import time
from pathlib import Path

from . import config

# One writer at a time. Two folds can land in the same instant — a mid-turn
# compaction's epilogue and the next turn's start check are minutes apart in
# practice, but the interface's hand compaction is not, and an interleaved
# append would tear a line in half.
_lock = threading.Lock()


def _path(sid: str) -> Path:
    # Resolved per call, like every other path in this codebase: tests redirect
    # TACIT_HOME between calls, and a cached Path would keep writing to the
    # old home.
    return config.SESSIONS_DIR / f"{sid}.archive.jsonl"


def append_rows(sid: str, msgs: list, why: str = "fold") -> int:
    """Append rows to the archive before a fold destroys them. Returns the count.

    Called from the two fold paths in :mod:`agent` — `compact_history` and
    `apply_stored_summary` — and always *before* the rows are mutated or
    dropped, because the point of the archive is the verbatim row, tool
    results and all. A fold that archived its rows after pruning them would
    preserve exactly the part the prune removed and lose the part it kept.

    Never raises: an archive that cannot be written must not stop the
    compaction that needs to happen, and the fold is the more urgent of the
    two jobs. The count is returned so the fold's report can say what it
    preserved even when the write failed.
    """
    if not sid or not msgs:
        return 0
    ts = round(time.time(), 3)
    lines = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        lines.append(json.dumps({"ts": ts, "why": why, "msg": m}, ensure_ascii=False))
    if not lines:
        return 0
    try:
        with _lock:
            path = _path(sid)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        return 0
    return len(lines)


def _render(i: int, row: dict) -> str:
    """One archived row as the tool should show it.

    Fuller than the summariser's digest, because this text is the answer, not
    an input to one: a search that shows 200 characters of a result the model
    needs is a search that makes the model guess. Bounded anyway — an
    unbounded render of a 40,000-character result is the very bulk the fold
    was relieving.
    """
    m = row.get("msg") if isinstance(row, dict) else None
    if not isinstance(m, dict):
        return f"[{i}] (unreadable row)"
    role = m.get("role", "?")
    body = str(m.get("content") or "").strip()
    lines = [f"[{i}] {role}: {body[:1200]}"] if body else [f"[{i}] {role}:"]
    for t in m.get("tools") or []:
        if not isinstance(t, dict):
            continue
        try:
            argtext = json.dumps(t.get("args") or {}, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            argtext = str(t.get("args") or "")
        flag = " FAILED" if t.get("is_error") else ""
        lines.append(f"      tool {t.get('name')}{flag} {argtext[:300]}")
        result = str(t.get("result") or "")
        if result:
            lines.append(f"      result: {result[:600]}")
    return "\n".join(lines)


def _load(sid: str) -> list[dict]:
    try:
        raw = _path(sid).read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def info(sid: str) -> str:
    rows = _load(sid)
    if not rows:
        return ("archive: empty. Nothing has been folded away in this session yet, "
                "or the archive tool was off when it happened.")
    first, last = rows[0], rows[-1]
    size = _path(sid).stat().st_size
    return (f"archive: {len(rows)} rows, {size} bytes, frozen verbatim by compaction. "
            f"First folded {first.get('ts')}, last {last.get('ts')}. "
            "action=search with a query finds rows; action=read pages through them.")


def read(sid: str, action: str = "info", query: str = "", offset: int = 0,
         limit: int = 20) -> str:
    """The tool's whole surface: info, search, read. Returns text for the model."""
    action = (action or "info").strip().lower()
    if action == "info":
        return info(sid)
    rows = _load(sid)
    if not rows:
        return info(sid)
    rendered = [(i, _render(i, r)) for i, r in enumerate(rows)]
    if action == "search":
        q = (query or "").strip()
        if not q:
            return ("ERROR: action=search needs a query. action=info reports the "
                    "archive's size; action=read pages through it in order.")
        q = q.lower()
        rendered = [(i, text) for i, text in rendered if q in text.lower()]
        if not rendered:
            return f"archive: no row matches {query!r}."
    else:                                   # read
        if action != "read":
            return (f"ERROR: unknown action {action!r}; use info, search or read.")
    try:
        offset = max(0, int(offset or 0))
        limit = min(50, max(1, int(limit or 20)))
    except Exception:  # noqa: BLE001
        offset, limit = 0, 20
    page = rendered[offset:offset + limit]
    if not page:
        return f"archive: offset {offset} is past the end ({len(rendered)} rows)."
    head = f"archive: rows {page[0][0]}..{page[-1][0]} of {len(rendered)}"
    if offset + limit < len(rendered):
        head += f"; action=read with offset={offset + limit} continues"
    return head + "\n\n" + "\n\n".join(text for _, text in page)


def remove(sid: str) -> None:
    """Drop the archive with its session. A delete is the last word."""
    if not sid:
        return
    try:
        _path(sid).unlink(missing_ok=True)
    except OSError:
        pass
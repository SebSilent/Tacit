"""Live per-session turn state, keyed by session id.

The store (``backend/store.py``) holds the transcript; this holds what is true
*right now*: whether a turn is running in a session, since when, and how many
turns that session has started. It is deliberately not part of the session
record — a busy flag written into the record would be persisted as truth and
then lie after a crash. This state is process-local by design: a session is
busy because a worker in *this* process is running its turn, and for no other
reason.

Two readers:

- the interface, via ``GET /api/sessions/state``, which draws the per-session
  indicators from it (stage 2 of the plan); and
- the socket handler, which consults it so that a second window attaching to
  the same session cannot start a second concurrent turn in it.

One session, one running turn, no matter how many windows hold it open. That
is the invariant the leak report is about, expressed as data.
"""

from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_ROWS: dict[str, dict] = {}

KINDS = ("turn", "plan", "assistant")


def _blank(sid: str) -> dict:
    return {"sid": sid, "busy": False, "starting": False, "kind": "",
            "since": 0.0, "turns": 0}


def begin(sid: str, kind: str = "turn") -> dict:
    """Mark a session as running something. Called before the worker starts,
    so the flag is already up when the next prompt for the same session can
    possibly arrive."""
    row = _blank(str(sid or ""))
    row.update({"busy": True, "starting": True,
                "kind": kind if kind in KINDS else "turn",
                "since": round(time.time(), 3)})
    with _LOCK:
        prev = _ROWS.get(row["sid"])
        row["turns"] = int((prev or {}).get("turns") or 0) + 1
        _ROWS[row["sid"]] = row
    return dict(row)


def running(sid: str) -> None:
    """The turn's first real event arrived: no longer merely starting."""
    with _LOCK:
        row = _ROWS.get(str(sid or ""))
        if row:
            row["starting"] = False


def end(sid: str) -> None:
    """The worker finished (or was killed). The session is idle again.

    Called from the worker's own ``finally``, not from the socket path: a
    browser that disconnects mid-turn must not mark the session idle while its
    turn is still running in here.
    """
    with _LOCK:
        row = _ROWS.get(str(sid or ""))
        if row:
            row["busy"] = False
            row["starting"] = False
            row["kind"] = ""


def get(sid: str) -> dict:
    sid = str(sid or "")
    with _LOCK:
        row = _ROWS.get(sid)
    return dict(row) if row else _blank(sid)


def busy(sid: str) -> bool:
    return bool(get(sid).get("busy"))


def all_states() -> dict[str, dict]:
    with _LOCK:
        return {sid: dict(row) for sid, row in _ROWS.items()}


def reset() -> None:
    """Test hook. Production state dies with the process; tests die sooner."""
    with _LOCK:
        _ROWS.clear()
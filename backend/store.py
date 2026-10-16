import json
import threading
import time
import uuid
from pathlib import Path

from . import config

def _index_path() -> Path:
    """Resolved on every call, never cached at import.

    This was a module-level constant, which meant redirecting the sessions
    directory (as the tests do, and as any future relocation would) moved the
    session files but left the index behind. The two halves of the store then
    disagreed about where they lived.
    """
    return config.SESSIONS_DIR / "index.json"


def _path(sid: str) -> Path:
    return config.SESSIONS_DIR / f"{sid}.json"


def _now() -> float:
    return round(time.time(), 3)


def index() -> list[dict]:
    rows = config.read_json(_index_path(), [])
    return rows if isinstance(rows, list) else []


def _write_index(rows: list[dict]) -> None:
    config.write_json(_index_path(), rows)


def list_sessions() -> list[dict]:
    return sorted(index(), key=lambda s: s.get("updated") or 0, reverse=True)


def create(title: str = "New session", model: str = "", mode: str = "agent",
           thinking: str = "default", project: str = "", sid: str = "") -> dict:
    """Make a session. Only ever called for an explicit request.

    A session exists because the user started one. Nothing on a page load, a
    reconnect, or a sync is allowed to conjure one, so `sid` may be supplied by
    the caller to keep the client and server agreed on the id.
    """
    if sid and get(sid) is not None:
        return get(sid)
    sid = sid or uuid.uuid4().hex
    rec = {
        "id": sid, "title": title or "New session", "model": model, "mode": mode,
        "thinking": thinking, "project": project, "created": _now(), "updated": _now(),
        "messages": [],
    }
    config.write_json(_path(sid), rec)
    rows = index()
    rows.append({k: rec[k] for k in ("id", "title", "model", "mode", "thinking",
                                     "project", "created", "updated")})
    _write_index(rows)
    return rec


def get(sid: str) -> dict | None:
    return config.read_json(_path(sid), None)


# Fields a long-lived in-memory copy must never overwrite. A turn's record is
# held in memory for minutes while the person keeps using the interface, and
# the settings panel writes the same file from a fresh read: without this, the
# turn's next save silently reverted whatever they had just changed, which is
# exactly how the assistant's "last 5 turns" came back as 20.
# The Goal and the autonomous run's counters join them for the same reason, and
# a sharper one: the harness writes them from a driver thread while the panel
# writes them from REST, and the in-memory record a turn holds is minutes old.
# Without preservation, the turn's next save would rewind the cycle count and
# silently re-enable a run the person had just stopped.
# planState joins them because plan mode writes it the same way, from a driver
# thread, while the websocket handler holds a record loaded before the plan
# existed. A stale save there would not just rewind progress; it would delete the
# snapshot a reload reads back, which is the whole reason the plan survives a
# dropped socket.
# planFile joins them because Implement writes it onto the session it creates, from
# the planning session's socket, before that session has ever been opened. A record
# saved without it would silently un-point the working agent at its own plan.
PRESERVED = ("assistantSettings", "assistantGoal", "assistantAutonomy", "planState",
             "planFile")


# One writer at a time through the two halves of the store. `save()` reads the
# index, edits one row and writes the whole list back; `remove()` unlinks the
# record and then rebuilds the index from a fresh read. Interleaved, they leave a
# row whose file is gone, and that is exactly what a ghost session is: a row in
# the list that opens with no transcript. In-process is the whole blast radius,
# because a turn worker never writes a session (tests/test_claims.py pins
# `store.save` absent from turn_worker.py), so no cross-process lock is needed.
_write_lock = threading.Lock()


def save(rec: dict, *, preserve=PRESERVED) -> dict:
    sid = rec["id"]
    with _write_lock:
        disk = config.read_json(_path(sid), None)
        # A record whose file AND index row are both gone was deleted while
        # something still held a copy of it: the turn's driver, a socket's `rec`
        # (chat.py keeps one for the life of the connection and saves it from
        # set_model / set_session_name / truncate / compact / assistant_*), a plan
        # runtime. Every one of those writes minutes after the delete, all of them
        # through this function, and each would recreate the file AND re-append the
        # index row below, which is the resurrection, and why it read as
        # "sometimes": it only happened when something was still live.
        # Refuse only when BOTH halves are missing. File gone / row present is a
        # half-written record this call should heal, and row gone / file present is
        # a lost index row it should restore. `create()` never comes through here
        # (it writes both halves itself), so refusing cannot block a real session.
        if disk is None and not any(r.get("id") == sid for r in index()):
            return rec
        rec["updated"] = _now()
        # `and disk` because the heal path above reaches here with nothing on
        # disk: `key in None` is a TypeError, and it would fire on exactly the
        # half-written record this call is supposed to repair.
        if preserve and disk:
            for key in preserve:
                if key in disk:
                    rec[key] = disk[key]
        config.write_json(_path(sid), rec)
        # The index read-edit-write stays inside the lock: it is the second half
        # of the pair, and a `remove()` that lands between this read and this write
        # is the interleaving that puts a deleted row back.
        rows = index()
        for i, r in enumerate(rows):
            if r.get("id") == sid:
                rows[i] = {k: rec.get(k, r.get(k)) for k in
                           ("id", "title", "model", "mode", "thinking", "project", "created", "updated")}
                break
        else:
            rows.append({k: rec.get(k) for k in
                         ("id", "title", "model", "mode", "thinking", "project", "created", "updated")})
        _write_index(rows)
    return rec


def patch(sid: str, **fields) -> dict | None:
    """Write exactly these fields onto the record as it is on disk.

    ``preserve=()`` because the caller is the owner of the fields it names:
    re-reading them from disk here would discard the very change being made.
    """
    rec = get(sid)
    if not rec:
        return None
    rec.update({k: v for k, v in fields.items() if v is not None})
    return save(rec, preserve=())


def append(rec: dict, role: str, content: str, extra: dict | None = None) -> dict:
    msg = {"role": role, "content": content, "ts": _now()}
    if extra:
        msg.update(extra)
    rec.setdefault("messages", []).append(msg)
    return msg


def duplicate(sid: str, suffix: str = " (copy)", up_to=None) -> dict | None:
    src = get(sid)
    if not src:
        return None
    msgs = list(src.get("messages") or [])
    if isinstance(up_to, int) and 0 <= up_to <= len(msgs):
        msgs = msgs[:up_to]
    rec = create(title=f"{(src.get('title') or 'Session')}{suffix}",
                 model=src.get("model") or "", mode=src.get("mode") or "agent",
                 thinking=src.get("thinking") or "default",
                 project=src.get("project") or "")
    rec["messages"] = [dict(m) for m in msgs]
    return save(rec)


def remove(sid: str) -> bool:
    # Both halves under one lock. The unlink and the index rebuild were two
    # separate writes, so a `save()` landing between them could read an index that
    # still held the row and write it back, a row with no file, which opens as an
    # empty session. Same reason `save()` holds the lock across its own pair.
    with _write_lock:
        try:
            _path(sid).unlink(missing_ok=True)
        except Exception:
            return False
        rows = [r for r in index() if r.get("id") != sid]
        _write_index(rows)
    # Outside the lock, and after the row is gone: the session's scratch folder holds
    # whatever its tools wrote during a project-less turn, and nothing else collects it.
    config.remove_workspace(sid)
    return True


# The registry is the browser's boot payload: every session's transcript in one
# response. It used to ship the last 200 messages of up to 200 sessions, which on
# a real home measured 1.2 MB — and that grows with the *longest* conversation,
# not the number of them, so it never stops getting worse. A drawer row needs the
# tail (title, count, which copy is richer); the transcript itself is fetched from
# /api/sessions/<id> when a session is actually opened.
#
# `messageCount` and `trimmed` are the part that makes this safe rather than a
# silent history loss: they tell the client it is holding a tail, so a tail can
# never be rendered, edited, or pushed back to the server as if it were the whole
# conversation. Undo and rollback index into this array by position — cut at the
# wrong row and you delete turns the user never asked to delete.
REGISTRY_PREVIEW = 24

# Counting messages was not enough, and measuring it proved the point: the real
# home's boot payload is 95.9 KB, and 94.3 KB of that is ONE session's 24-message
# tail, because a single stored message measured 7,856 bytes. Twenty-four rows of
# an unbounded field is not a bound. The unbounded field is `tools[].args` - chat
# clips `result` (MAX_TOOL_RESULT) and `reason` (MAX_REASON), but `args` is a dict
# passed through as-is, so a `write_file` call stores the entire file body in it.
#
# So this bounds bytes, at two levels: per field, and per payload. A tail that
# would push the whole response past REGISTRY_TAIL_BUDGET ships with no messages
# at all - which is safe because nothing draws a non-active tail: the drawer row
# is built from the index fields, and the active session is replaced by the full
# /api/sessions/<id> record before it is ever painted (pinned in
# tests/test_registry_preview.py). `trimmed` then tells the client it is holding
# less than it thinks, which is the same flag that keeps a partial transcript from
# being pushed back as a whole one.
PREVIEW_MARKER = " […]"
PREVIEW_CONTENT_CAP = 240
PREVIEW_TOOL_CAP = 120
PREVIEW_TOOL_MAX = 6
REGISTRY_TAIL_BUDGET = 48_000


def _cap_str(value, limit: int):
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + PREVIEW_MARKER


def _preview_tool(t: dict) -> dict:
    """One tool call, same keys, bounded values. `args` is why this exists."""
    row = {}
    for k, v in t.items():
        if k == "args" and isinstance(v, dict):
            row[k] = {ak: _cap_str(av, PREVIEW_TOOL_CAP) for ak, av in v.items()}
        elif k == "result":
            row[k] = _cap_str(v, PREVIEW_TOOL_CAP)
        elif k == "name":
            row[k] = _cap_str(v, 64)          # a name is never the payload
        else:
            row[k] = _cap_str(v, PREVIEW_TOOL_CAP) if isinstance(v, str) else v
    return row


def _preview_message(m: dict) -> dict:
    """One message as the boot payload ships it.

    The key set is unchanged on purpose - the client's undo logic and the
    `trimmed` contract both read `messages[i]`, so a row that lost a key would be
    a shape change, not a size change. `reason` keeps its key and loses its value:
    it is capped at MAX_REASON (8,000) on the way in, it is the second-largest
    field, and a tail nobody renders does not need the model's thinking.
    """
    tools = m.get("tools")
    return {
        "role": m.get("role"),
        "content": _cap_str(m.get("content"), PREVIEW_CONTENT_CAP),
        "reason": None,
        "tools": [_preview_tool(t) for t in tools[:PREVIEW_TOOL_MAX] if isinstance(t, dict)]
        if isinstance(tools, list) and tools else None,
        "ts": m.get("ts"),
    }


def registry(rows: list[dict] | None = None, preview: int = REGISTRY_PREVIEW,
             budget: int = REGISTRY_TAIL_BUDGET) -> dict:
    rows = rows if rows is not None else index()
    out = []
    spent = 0
    for r in sorted(rows, key=lambda s: s.get("updated") or 0, reverse=True)[:200]:
        rec = get(r.get("id", ""))
        msgs = (rec or {}).get("messages") or []
        # `ts` is in the projection because the transcript's clock is a display
        # row, not a model row: it is written on every append (:121) and the
        # browser cannot draw a time it never receives. It is display metadata
        # only, agent.transcript_messages rebuilds every row as {role, content}
        # on the way to a provider, so nothing here reaches the model.
        messages = [_preview_message(m) for m in msgs[-preview:]]
        # Spend the budget newest-first: if the payload cannot carry every tail,
        # the rows it drops are the ones furthest from what the person is reading.
        # Message order is never disturbed, so an index still means the same row.
        keep: list[dict] = []
        for m in reversed(messages):
            # +2 because that is what the row really costs: json.dumps joins an
            # array with ", ", so a serialized array is exactly sum(msg + 2).
            # Charging len(msg) alone under-counted the payload by hundreds of
            # bytes on a six-session home, which is the same mistake in miniature.
            cost = len(json.dumps(m, ensure_ascii=False)) + 2
            if spent + cost > budget:
                break
            spent += cost
            keep.append(m)
        keep.reverse()
        if not keep:
            spent += 2   # an empty tail still ships "[]"
        out.append({**r, "messageCount": len(msgs),
                    "trimmed": len(msgs) > len(keep), "messages": keep})
    return {"sessions": out, "active": (out[0]["id"] if out else "")}


def merge(payload: dict) -> dict:
    """Apply updates arriving from another browser. Never creates anything.

    This used to invent a session for every unrecognised id, which meant one
    page load could multiply the list: the client pushed its whole local set
    and the server dutifully materialised each one. An unknown id is now
    ignored, because a session is created by the user, not by a mention of an
    id.
    """
    rows = index()
    by_id = {r["id"]: r for r in rows}
    for incoming in (payload or {}).get("sessions") or []:
        sid = incoming.get("id")
        if not sid:
            continue
        local = get(sid)
        if local is None:
            continue
        incoming_msgs = incoming.get("messages") or []
        if len(incoming_msgs) > len(local.get("messages") or []):
            local["messages"] = incoming_msgs
        for k in ("title", "model", "mode", "thinking", "project"):
            if incoming.get(k):
                local[k] = incoming[k]
        save(local)
        by_id[sid] = local
    return registry(list(by_id.values()))

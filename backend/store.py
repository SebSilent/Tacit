import json
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
           thinking: str = "medium", project: str = "", sid: str = "") -> dict:
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


def save(rec: dict) -> dict:
    rec["updated"] = _now()
    config.write_json(_path(rec["id"]), rec)
    rows = index()
    for i, r in enumerate(rows):
        if r.get("id") == rec["id"]:
            rows[i] = {k: rec.get(k, r.get(k)) for k in
                       ("id", "title", "model", "mode", "thinking", "project", "created", "updated")}
            break
    else:
        rows.append({k: rec.get(k) for k in
                     ("id", "title", "model", "mode", "thinking", "project", "created", "updated")})
    _write_index(rows)
    return rec


def patch(sid: str, **fields) -> dict | None:
    rec = get(sid)
    if not rec:
        return None
    rec.update({k: v for k, v in fields.items() if v is not None})
    return save(rec)


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
                 thinking=src.get("thinking") or "medium",
                 project=src.get("project") or "")
    rec["messages"] = [dict(m) for m in msgs]
    return save(rec)


def remove(sid: str) -> bool:
    try:
        _path(sid).unlink(missing_ok=True)
    except Exception:
        return False
    rows = [r for r in index() if r.get("id") != sid]
    _write_index(rows)
    return True


def registry(rows: list[dict] | None = None) -> dict:
    rows = rows if rows is not None else index()
    out = []
    for r in sorted(rows, key=lambda s: s.get("updated") or 0, reverse=True)[:200]:
        rec = get(r.get("id", ""))
        msgs = (rec or {}).get("messages") or []
        out.append({**r, "messages": [
            {"role": m.get("role"), "content": m.get("content"),
             "reason": m.get("reason"), "tools": m.get("tools")} for m in msgs[-200:]
        ]})
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

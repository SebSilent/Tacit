"""Learning proposals.

Tacit may notice a rule worth keeping. It may not apply one on its own unless you
have said so, and even then only within limits. So a proposal is an **artifact**:
a stored record with its own state, which you can read, accept, edit, reject,
disable or delete.

The default mode is ``propose``, which cannot change anything.

When a proposal is approved, it is not written into the prompt. It goes to one of
the two places that already exist and are already budgeted:

  kind "skill"                    -> a skill file, indexed by name, read on demand
  rule, correction, preference    -> memory, governed by the memory budget

That is the whole point. Learning must not be a back door around context
discipline, so it borrows the front doors.

Stored at ``~/.tacit/learning.json``, bounded, and readable as plain JSON.
"""

from __future__ import annotations

import re
import time
import uuid

from . import audit, config, memory_store, providers, skills, tokens

KINDS = ("skill", "rule", "correction", "preference")
STATES = ("proposed", "approved", "rejected", "disabled")
RISKS = ("low", "high")
CONFIDENCE = ("low", "medium", "high")
SCOPES = memory_store.SCOPES

MAX_ARTIFACTS = 200
MAX_BODY = 4000

_RISKY = re.compile(
    r"\b(delete|remove|drop|destroy|overwrite|force|reset|wipe|rm\b|kill|"
    r"password|secret|token|api[_-]?key|credential|sudo|chmod|registry)\b", re.I)


def _now() -> float:
    return round(time.time(), 3)


def load() -> list[dict]:
    data = config.read_json(config.LEARNING_FILE, [])
    return data if isinstance(data, list) else []


def _save(rows: list[dict]) -> None:
    # Bounded: rejected and disabled artifacts go first, oldest first, so the
    # file cannot grow without limit.
    if len(rows) > MAX_ARTIFACTS:
        order = {"rejected": 0, "disabled": 1, "proposed": 2, "approved": 3}
        rows = sorted(rows, key=lambda r: (order.get(r.get("state"), 9),
                                           r.get("created_at") or 0))
        rows = rows[len(rows) - MAX_ARTIFACTS:]
    config.write_json(config.LEARNING_FILE, rows)


def get(artifact_id: str) -> dict | None:
    return next((r for r in load() if r.get("id") == artifact_id), None)


def risk_of(body: str) -> str:
    return "high" if _RISKY.search(str(body or "")) else "low"


def mode() -> str:
    return providers.resolve("learning")["id"]


def _entry(row: dict) -> dict:
    return {**row, "display": tokens.label(row.get("tokens") or 0)}


def list_artifacts(state: str = "") -> list[dict]:
    rows = sorted(load(), key=lambda r: r.get("created_at") or 0, reverse=True)
    if state:
        rows = [r for r in rows if r.get("state") == state]
    return [_entry(r) for r in rows]


def stats() -> dict:
    rows = load()
    counts = {s: sum(1 for r in rows if r.get("state") == s) for s in STATES}
    return {**counts, "total": len(rows), "cap": MAX_ARTIFACTS,
            "mode": mode(), "tokens_approved": sum(
                int(r.get("tokens") or 0) for r in rows if r.get("state") == "approved")}


def propose(kind: str, title: str, body: str, *, scope: str = "global",
            scope_key: str = "", confidence: str = "medium",
            source_session: str = "") -> dict:
    """Record a proposal. In `propose` mode this changes nothing anywhere."""
    text = str(body or "").strip()[:MAX_BODY]
    name = str(title or "").strip()[:120]
    if not text:
        return {"ok": False, "error": "a body is required"}
    kind = kind if kind in KINDS else "rule"
    row = {
        "id": "l_" + uuid.uuid4().hex[:10],
        "kind": kind,
        "title": name or text.splitlines()[0][:120],
        "body": text,
        "scope": scope if scope in SCOPES else "global",
        "scope_key": str(scope_key or ""),
        "confidence": confidence if confidence in CONFIDENCE else "medium",
        "risk": risk_of(text),
        "state": "proposed",
        "source_session": str(source_session or ""),
        "created_at": _now(), "updated_at": _now(),
        "last_used_at": 0, "use_count": 0,
        "tokens": tokens.estimate_tokens(text),
        "wrote": "",          # what approving it created, for undo
    }

    current = mode()
    auto = False
    if current == "auto":
        auto = True
    elif current == "auto-low-risk":
        auto = (kind == "preference" and row["risk"] == "low"
                and row["confidence"] in ("medium", "high"))
    elif current == "learn-off":
        return {"ok": False, "error": "learning is off. Set a mode in Settings > "
                                      "Capabilities first."}

    rows = load()
    rows.append(row)
    _save(rows)
    audit.record("learning_proposed", session=row["source_session"], backend=current,
                 mode=current, status="ok", kind=kind, risk=row["risk"],
                 auto=auto, tokens=row["tokens"])
    if auto:
        return approve(row["id"], automatic=True)
    return {"ok": True, "artifact": _entry(row), "auto_applied": False}


def _write_out(row: dict) -> str:
    """Put an approved item where it belongs. Returns what was created."""
    if row["kind"] == "skill":
        res = skills.create(row["title"], row["body"], row["title"])
        return f"skill:{res.get('name') or row['title']}" if res.get("ok") else ""
    res = memory_store.add(
        row["body"], type="decision" if row["kind"] == "rule" else "preference",
        scope=row["scope"], project=row["scope_key"] if row["scope"] == "project" else "",
        source="agent_suggestion", confidence=row["confidence"], enabled=True,
        reason=f"approved from learning proposal {row['id']}")
    return f"memory:{res['memory']['id']}" if res.get("ok") else ""


def _undo(row: dict) -> None:
    wrote = str(row.get("wrote") or "")
    if wrote.startswith("skill:"):
        skills.remove(wrote.split(":", 1)[1])
    elif wrote.startswith("memory:"):
        try:
            memory_store.delete(int(wrote.split(":", 1)[1]))
        except (ValueError, TypeError):
            pass


def approve(artifact_id: str, automatic: bool = False) -> dict:
    """The only action that changes anything. Everything else just records."""
    row = get(artifact_id)
    if row is None:
        return {"ok": False, "error": "no such proposal"}
    if row["state"] == "approved":
        return {"ok": True, "artifact": _entry(row), "already": True}
    wrote = _write_out(row)
    row["state"] = "approved"
    row["wrote"] = wrote
    row["updated_at"] = _now()
    rows = [r if r["id"] != artifact_id else row for r in load()]
    _save(rows)
    audit.record("learning_approved", session=row["source_session"], backend=mode(),
                 mode=mode(), status="ok", kind=row["kind"], wrote=wrote,
                 automatic=automatic, tokens=row["tokens"])
    return {"ok": True, "artifact": _entry(row), "wrote": wrote, "auto_applied": automatic}


def reject(artifact_id: str) -> dict:
    return _mark(artifact_id, "rejected")


def disable(artifact_id: str) -> dict:
    row = get(artifact_id)
    if row is None:
        return {"ok": False, "error": "no such proposal"}
    _undo(row)
    row["wrote"] = ""
    return _mark(artifact_id, "disabled")


def enable(artifact_id: str) -> dict:
    return approve(artifact_id)


def edit(artifact_id: str, title: str = "", body: str = "") -> dict:
    row = get(artifact_id)
    if row is None:
        return {"ok": False, "error": "no such proposal"}
    if title:
        row["title"] = str(title).strip()[:120]
    if body:
        row["body"] = str(body).strip()[:MAX_BODY]
        row["tokens"] = tokens.estimate_tokens(row["body"])
        row["risk"] = risk_of(row["body"])
    row["updated_at"] = _now()
    _save([r if r["id"] != artifact_id else row for r in load()])
    return {"ok": True, "artifact": _entry(row)}


def remove(artifact_id: str) -> dict:
    row = get(artifact_id)
    if row is None:
        return {"ok": False, "error": "no such proposal"}
    _undo(row)
    _save([r for r in load() if r["id"] != artifact_id])
    audit.record("learning_deleted", session=row["source_session"], status="ok",
                 kind=row["kind"])
    return {"ok": True, "removed": artifact_id}


def _mark(artifact_id: str, state: str) -> dict:
    row = get(artifact_id)
    if row is None:
        return {"ok": False, "error": "no such proposal"}
    row["state"] = state
    row["updated_at"] = _now()
    _save([r if r["id"] != artifact_id else row for r in load()])
    audit.record(f"learning_{state}", session=row["source_session"], mode=mode(),
                 status="ok", kind=row["kind"])
    return {"ok": True, "artifact": _entry(row)}


def clear(state: str = "rejected") -> dict:
    rows = load()
    kept = [r for r in rows if r.get("state") != state] if state else []
    _save(kept)
    return {"ok": True, "removed": len(rows) - len(kept)}

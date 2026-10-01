"""Transparent, token-budgeted persistent memory.

Design rules, in order of importance:

1. **Never a token furnace.** Nothing is injected at startup unless it is
   explicitly enabled *and* fits the configured budget. The default budget is
   small on purpose.
2. **Inspectable.** Every record carries its type, scope, source, confidence and
   its own token estimate, so the UI can show exactly what a memory costs.
3. **Revocable.** Disable, unpin, edit or delete any record at any time; the
   startup block recomputes immediately.
4. **Local.** One SQLite file under ``~/.tacit``. FTS5 is used when the SQLite
   build has it, otherwise keyword search degrades to LIKE. No vector database
   is required.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from . import config, tokens

TYPES = ("preference", "project_fact", "decision", "lesson", "pattern", "contact", "other")
SCOPES = ("global", "project", "language", "repository")
CONFIDENCE = ("low", "medium", "high")
SOURCES = ("user", "agent_suggestion", "session_extract")

DEFAULT_BUDGET = 120
HARD_MAX_BUDGET = 500

_LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL DEFAULT 'other',
  scope TEXT NOT NULL DEFAULT 'global',
  project TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'user',
  confidence TEXT NOT NULL DEFAULT 'medium',
  enabled INTEGER NOT NULL DEFAULT 1,
  pinned INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0,
  last_used_at REAL NOT NULL DEFAULT 0,
  use_count INTEGER NOT NULL DEFAULT 0,
  token_estimate INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS compressions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL DEFAULT 0,
  before_tokens INTEGER NOT NULL DEFAULT 0,
  after_tokens INTEGER NOT NULL DEFAULT 0,
  before_text TEXT NOT NULL DEFAULT '',
  after_text TEXT NOT NULL DEFAULT '',
  approved INTEGER NOT NULL DEFAULT 0
);
"""

# Added after the first release, so they are applied to existing databases
# rather than assumed. Each is additive and safe to skip.
_MIGRATIONS = (
    ("scope_key", "ALTER TABLE memories ADD COLUMN scope_key TEXT NOT NULL DEFAULT ''"),
    ("expires_at", "ALTER TABLE memories ADD COLUMN expires_at REAL NOT NULL DEFAULT 0"),
    ("source_session",
     "ALTER TABLE memories ADD COLUMN source_session TEXT NOT NULL DEFAULT ''"),
    ("reason", "ALTER TABLE memories ADD COLUMN reason TEXT NOT NULL DEFAULT ''"),
)


def _migrate(con: sqlite3.Connection) -> None:
    have = {row[1] for row in con.execute("PRAGMA table_info(memories)")}
    for name, sql in _MIGRATIONS:
        if name not in have:
            try:
                con.execute(sql)
            except Exception:  # noqa: BLE001
                pass


class _Conn:
    """Context manager that commits on success and *always* closes the handle.

    ``with sqlite3.connect(...)`` commits but does not close, which leaks file
    handles and locks the database file on Windows.
    """

    def __init__(self):
        self.con = sqlite3.connect(str(config.MEMORY_DB))
        self.con.row_factory = sqlite3.Row
        self.con.executescript(_SCHEMA)
        _migrate(self.con)
        # The search index belongs to the database, so it is created with it.
        # If this SQLite build has no FTS5, searching falls back to LIKE.
        try:
            self.con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                             "USING fts5(content, content='memories', content_rowid='id')")
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> sqlite3.Connection:
        return self.con

    def __exit__(self, exc_type, exc, tb):
        try:
            self.con.commit() if exc_type is None else self.con.rollback()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.con.close()
        return False


def _connect() -> _Conn:
    config.HOME.mkdir(parents=True, exist_ok=True)
    return _Conn()


def has_fts() -> bool:
    """True when this database can do full-text search.

    Asked per call rather than cached: the answer belongs to a database, and
    caching it globally broke every other database the process touched.
    """
    try:
        with _connect() as con:
            row = con.execute("SELECT name FROM sqlite_master WHERE name = 'memories_fts'")
            return row.fetchone() is not None
    except Exception:  # noqa: BLE001
        return False


def _now() -> float:
    return round(time.time(), 3)


def budget() -> int:
    """The configured startup memory token budget (clamped to the hard max)."""
    raw = config.prefs().get("memoryBudget", DEFAULT_BUDGET)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_BUDGET
    override = bool(config.prefs().get("memoryBudgetOverride"))
    ceiling = 100000 if override else HARD_MAX_BUDGET
    return max(0, min(value, ceiling))


def set_budget(value: int, override: bool = False) -> dict:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return {"ok": False, "error": "budget must be a number"}
    config.save_prefs({"memoryBudget": max(0, n), "memoryBudgetOverride": bool(override)})
    return {"ok": True, "budget": budget()}


def _row(record: sqlite3.Row) -> dict:
    row = dict(record)
    row["enabled"] = bool(row.get("enabled"))
    row["pinned"] = bool(row.get("pinned"))
    return row


def add(content: str, *, type: str = "other", scope: str = "global", project: str = "",
        source: str = "user", confidence: str = "medium", pinned: bool = False,
        enabled: bool = True, scope_key: str = "", source_session: str = "",
        ttl_days: int = 0, reason: str = "") -> dict:
    text = str(content or "").strip()
    if not text:
        return {"ok": False, "error": "content is required"}
    kind = type if type in TYPES else "other"
    where = scope if scope in SCOPES else "global"
    trust = confidence if confidence in CONFIDENCE else "medium"
    origin = source if source in SOURCES else "user"
    now = _now()
    est = tokens.estimate_tokens(text)
    expires = now + int(ttl_days) * 86400 if int(ttl_days or 0) > 0 else 0.0
    with _LOCK, _connect() as con:
        cur = con.execute(
            "INSERT INTO memories (type, scope, scope_key, project, content, source, "
            "confidence, enabled, pinned, created_at, updated_at, token_estimate, "
            "expires_at, source_session, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, where, str(scope_key or ""),
             project if where == "project" else "", text, origin, trust,
             int(enabled), int(pinned), now, now, est, expires,
             str(source_session or ""), str(reason or "")))
        new_id = int(cur.lastrowid)
        if has_fts():
            con.execute("INSERT INTO memories_fts (rowid, content) VALUES (?, ?)", (new_id, text))
    return {"ok": True, "memory": get(new_id)}


def reinforce(memory_id: int, ttl_days: int = 0) -> dict:
    """Mark a memory as used: bump the count, and push its expiry out."""
    now = _now()
    with _LOCK, _connect() as con:
        if int(ttl_days or 0) > 0:
            con.execute("UPDATE memories SET last_used_at = ?, use_count = use_count + 1, "
                        "expires_at = ? WHERE id = ?",
                        (now, now + int(ttl_days) * 86400, int(memory_id)))
        else:
            con.execute("UPDATE memories SET last_used_at = ?, use_count = use_count + 1 "
                        "WHERE id = ?", (now, int(memory_id)))
    return {"ok": True, "memory": get(memory_id)}


def is_expired(row: dict) -> bool:
    expires = float(row.get("expires_at") or 0)
    return bool(expires and expires < _now())


def get(memory_id: int) -> dict | None:
    with _LOCK, _connect() as con:
        row = con.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    return _row(row) if row else None


def update(memory_id: int, **fields) -> dict:
    current = get(memory_id)
    if not current:
        return {"ok": False, "error": f"no memory {memory_id}"}
    sets, values = [], []
    if fields.get("content") is not None:
        text = str(fields["content"]).strip()
        if not text:
            return {"ok": False, "error": "content cannot be empty"}
        sets += ["content = ?", "token_estimate = ?"]
        values += [text, tokens.estimate_tokens(text)]
    for key, allowed in (("type", TYPES), ("scope", SCOPES), ("confidence", CONFIDENCE),
                         ("source", SOURCES)):
        value = fields.get(key)
        if value is not None and value in allowed:
            sets.append(f"{key} = ?")
            values.append(value)
    for key in ("enabled", "pinned"):
        if fields.get(key) is not None:
            sets.append(f"{key} = ?")
            values.append(int(bool(fields[key])))
    if fields.get("project") is not None:
        sets.append("project = ?")
        values.append(str(fields["project"]))
    if not sets:
        return {"ok": True, "memory": current}
    sets.append("updated_at = ?")
    values.append(_now())
    values.append(int(memory_id))
    with _LOCK, _connect() as con:
        con.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", values)
        if has_fts() and fields.get("content") is not None:
            con.execute("DELETE FROM memories_fts WHERE rowid = ?", (int(memory_id),))
            con.execute("INSERT INTO memories_fts (rowid, content) VALUES (?, ?)",
                        (int(memory_id), str(fields["content"]).strip()))
    return {"ok": True, "memory": get(memory_id)}


def delete(memory_id: int) -> dict:
    with _LOCK, _connect() as con:
        cur = con.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
        removed = cur.rowcount
        if has_fts():
            con.execute("DELETE FROM memories_fts WHERE rowid = ?", (int(memory_id),))
    return {"ok": bool(removed), "removed": int(removed)}


def clear(scope: str = "", project: str = "") -> dict:
    sql, args = "DELETE FROM memories", []
    if scope:
        sql += " WHERE scope = ?"
        args.append(scope)
        if scope == "project" and project:
            sql += " AND project = ?"
            args.append(project)
    with _LOCK, _connect() as con:
        cur = con.execute(sql, args)
    return {"ok": True, "removed": int(cur.rowcount)}


def list_memories(*, type: str = "", scope: str = "", project: str = "",
                  enabled=None, pinned=None, search: str = "", limit: int = 500) -> list[dict]:
    sql = "SELECT * FROM memories"
    where, args = [], []
    if type:
        where.append("type = ?")
        args.append(type)
    if scope:
        where.append("scope = ?")
        args.append(scope)
    if enabled is not None:
        where.append("enabled = ?")
        args.append(int(bool(enabled)))
    if pinned is not None:
        where.append("pinned = ?")
        args.append(int(bool(pinned)))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY pinned DESC, use_count DESC, updated_at DESC LIMIT ?"
    args.append(max(1, min(int(limit or 500), 2000)))
    with _LOCK, _connect() as con:
        rows = [_row(r) for r in con.execute(sql, args).fetchall()]
    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in (r["content"] or "").lower()]
    if project:
        rows = [r for r in rows if r["scope"] == "global" or r["project"] == project]
    return [r for r in rows if not is_expired(r)]


def recall(query: str = "", *, limit: int = 5, scope: str = "", project: str = "") -> list[dict]:
    """Search memories and mark the hits as used (updates last_used/use_count)."""
    q = str(query or "").strip()
    hits: list[dict] = []
    if q and has_fts():
        with _LOCK, _connect() as con:
            try:
                rows = con.execute(
                    "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                    "WHERE memories_fts MATCH ? AND m.enabled = 1 LIMIT ?",
                    (_fts_query(q), max(1, min(int(limit or 5), 50)))).fetchall()
                hits = [_row(r) for r in rows]
            except Exception:  # noqa: BLE001
                hits = []
    if not hits:
        needle = q.lower()
        pool = list_memories(enabled=True, scope=scope, project=project, limit=2000)
        scored = []
        for row in pool:
            text = (row["content"] or "").lower()
            score = sum(3 for term in needle.split() if term and term in text)
            if needle and needle in text:
                score += 5
            if score:
                scored.append((score, row))
        scored.sort(key=lambda p: (-p[0], -p[1]["use_count"]))
        hits = [row for _s, row in scored[:max(1, min(int(limit or 5), 50))]]
    if hits:
        now = _now()
        ids = [h["id"] for h in hits]
        with _LOCK, _connect() as con:
            con.executemany("UPDATE memories SET last_used_at = ?, use_count = use_count + 1 "
                            "WHERE id = ?", [(now, i) for i in ids])
        for row in hits:
            row["last_used_at"] = now
            row["use_count"] = int(row.get("use_count") or 0) + 1
    return hits


def _fts_query(text: str) -> str:
    terms = [t for t in "".join(c if (c.isalnum() or c.isspace()) else " "
                                for c in str(text)).split() if t]
    return " OR ".join(f'"{t}"' for t in terms) or '""'


# ── startup injection ──────────────────────────────────────────────────────
def _render(rows) -> str:
    return "Memory context (durable notes the user chose to keep):\n" + "\n".join(
        f"- [{r['type']}] {r['content']}" for r in rows)


def startup_selection(project: str = "") -> dict:
    """Pick the memories that may be injected, honouring the budget.

    Priority: pinned > confidence > use_count > recency. The *rendered* block is
    measured at every step, so the header and separators can never push the
    result past the budget — the block never silently expands.
    """
    limit = budget()
    if limit <= 0:
        return {"text": "", "tokens": 0, "used": [], "skipped": 0, "budget": limit}
    pool = list_memories(enabled=True, project=project, limit=2000)
    rank = {"high": 0, "medium": 1, "low": 2}
    pool.sort(key=lambda r: (0 if r["pinned"] else 1,
                             rank.get(r["confidence"], 1),
                             -int(r["use_count"] or 0),
                             -float(r["updated_at"] or 0)))
    chosen, skipped = [], 0
    for row in pool[:200]:
        trial = chosen + [row]
        if tokens.estimate_tokens(_render(trial)) <= limit:
            chosen = trial
        else:
            skipped += 1
    if not chosen:
        return {"text": "", "tokens": 0, "used": [], "skipped": skipped, "budget": limit}
    text = _render(chosen)
    return {"text": text, "tokens": tokens.estimate_tokens(text),
            "used": [r["id"] for r in chosen], "skipped": skipped, "budget": limit}


def stats(project: str = "") -> dict:
    rows = list_memories(project=project, limit=2000)
    enabled = [r for r in rows if r["enabled"]]
    pinned = [r for r in rows if r["pinned"]]
    selection = startup_selection(project)
    total_tokens = sum(int(r["token_estimate"] or 0) for r in enabled)
    return {
        "count": len(rows),
        "enabled": len(enabled),
        "pinned": len(pinned),
        "total_tokens": total_tokens,
        "startup_tokens": selection["tokens"],
        "budget": selection["budget"],
        "budget_remaining": max(0, selection["budget"] - selection["tokens"]),
        "excluded_by_budget": selection["skipped"],
        "fts": has_fts(),
        "exact": tokens.exact(),
        "types": {t: sum(1 for r in rows if r["type"] == t) for t in TYPES if
                  any(r["type"] == t for r in rows)},
    }


# ── compression history ────────────────────────────────────────────────────
def record_compression(before: str, after: str, approved: bool = False) -> dict:
    b = tokens.estimate_tokens(before)
    a = tokens.estimate_tokens(after)
    with _LOCK, _connect() as con:
        con.execute("INSERT INTO compressions (ts, before_tokens, after_tokens, before_text, "
                    "after_text, approved) VALUES (?,?,?,?,?,?)",
                    (_now(), b, a, before, after, int(bool(approved))))
    return {"before_tokens": b, "after_tokens": a, "saved": max(0, b - a)}


def compressions(limit: int = 50) -> list[dict]:
    with _LOCK, _connect() as con:
        rows = con.execute("SELECT * FROM compressions ORDER BY ts DESC LIMIT ?",
                           (max(1, min(int(limit or 50), 500)),)).fetchall()
    return [dict(r) for r in rows]

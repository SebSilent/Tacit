"""Background session analyzer.

Tacit may notice a rule worth keeping. It never applies one on its own, and it
never spends a model call on the chance of finding one.

Design:

* Heuristics first, always. A regex pass over the *user's* turns costs nothing and
  is the only thing that runs by default. An optional model pass exists but is off
  unless a person turns it on, and it still only produces proposals.
* Nothing here touches the agent. No tool is registered, no prompt text is added,
  no schema is injected. The analyzer reads finished transcripts and writes
  proposals; that is the entire surface.
* It runs off the request path, on a timer, and picks up only sessions it has not
  already read (or that have grown since).
* Every proposal is deduplicated, so a repeated correction does not become fifty
  proposals. A second sighting raises confidence instead.

The output is a proposal in the artifact store with its provenance attached, which
means the user can see the exact sentence that caused it. That is the whole point:
autonomous in *finding*, never in *deciding*.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import audit, config, learning, providers, store

MAX_SNIPPET = 400
MAX_PROPOSALS_PER_SESSION = 5
MIN_SNIPPET = 12

# ── the heuristics ────────────────────────────────────────────────────
# Checked in this order, and the first match wins: an explicit "remember that"
# outranks a standing order, which outranks a bare correction, which outranks a
# stated preference. A sentence carrying two signals is filed under the stronger
# one, so "no, don't use X, always use Y" is recorded as a rule rather than a
# complaint.
RULES = (
    ("remember", "preference", 0.80, re.compile(
        r"\b(?:remember (?:that|this)|note that|keep in mind|"
        r"for future reference|don'?t forget)\b", re.I)),
    ("standing_order", "rule", 0.75, re.compile(
        r"\b(?:always|never|from now on|every time|going forward|"
        r"make sure (?:to|that)|be sure to)\b", re.I)),
    ("correction", "correction", 0.70, re.compile(
        r"(?:^|[\s,;(])(?:no|nope|nah)[,.]?\s|"
        r"\b(?:that'?s (?:not|wrong)|not what i|don'?t\b|do not\b|stop\b|"
        r"instead\b|rather than|not like that|i said)\b", re.I)),
    ("convention", "project_convention", 0.65, re.compile(
        r"\b(?:in this (?:project|repo|codebase)|for this (?:project|repo)|"
        r"our (?:convention|style|rule)|the convention|we (?:always|never) use)\b", re.I)),
    ("preference", "preference", 0.60, re.compile(
        r"\b(?:i prefer|i'?d rather|i like|i want you to|please (?:always|use)|"
        r"use \S+ (?:not|instead of)|prefer)\b", re.I)),
)

# Sentence splitting that keeps code fences out of the way.
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_FENCE = re.compile(r"```[\s\S]*?```")
_NOISE = re.compile(r"^\s*(?:ok|okay|thanks|thank you|thx|great|nice|cool|yes|yeah|yep|sure)\W*$", re.I)


def _now() -> float:
    return round(time.time(), 3)


# ── run bookkeeping (SQLite, same file the memory store uses) ─────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS analyzer_runs (
  session_id  TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  analyzed_at REAL NOT NULL,
  turns       INTEGER NOT NULL DEFAULT 0,
  created     INTEGER NOT NULL DEFAULT 0,
  engine      TEXT NOT NULL DEFAULT 'heuristics'
);
CREATE TABLE IF NOT EXISTS proposal_keys (
  key         TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL,
  first_seen  REAL NOT NULL,
  sightings   INTEGER NOT NULL DEFAULT 1
);
"""


def _connect() -> sqlite3.Connection:
    config.HOME.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(config.MEMORY_DB), timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


@contextmanager
def _db():
    """A connection that is always closed.

    sqlite3's own context manager commits or rolls back; it does not close. On
    Windows a left-open handle keeps the file locked, which breaks cleanup and
    any later attempt to move the database.
    """
    con = _connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _fingerprint(messages: list[dict]) -> str:
    """Changes when the transcript changes, so a grown session is re-read."""
    last = messages[-1].get("content") if messages else ""
    raw = f"{len(messages)}:{len(str(last))}:{str(last)[-80:]}"
    return str(abs(hash(raw)) % (10 ** 12))


def already_analyzed(session_id: str, fingerprint: str) -> bool:
    try:
        with _db() as con:
            row = con.execute("SELECT fingerprint FROM analyzer_runs WHERE session_id = ?",
                              (session_id,)).fetchone()
        return bool(row) and row["fingerprint"] == fingerprint
    except Exception:  # noqa: BLE001
        return False


def _mark(session_id: str, fingerprint: str, turns: int, created: int, engine: str) -> None:
    try:
        with _db() as con:
            # `created` accumulates rather than being replaced. A sweep counts only
            # the proposals it genuinely added, so re-reading a grown session must
            # not wipe the tally of what it produced before.
            con.execute(
                "INSERT INTO analyzer_runs (session_id, fingerprint, analyzed_at, turns,"
                " created, engine) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET fingerprint=excluded.fingerprint,"
                " analyzed_at=excluded.analyzed_at, turns=excluded.turns,"
                " created=analyzer_runs.created + excluded.created,"
                " engine=excluded.engine",
                (session_id, fingerprint, _now(), turns, created, engine))
    except Exception:  # noqa: BLE001
        pass


def _key_of(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()[:160]


def _seen_before(key: str) -> str:
    """Returns the existing proposal id when this rule has been seen already."""
    try:
        with _db() as con:
            row = con.execute("SELECT proposal_id, sightings FROM proposal_keys WHERE key = ?",
                              (key,)).fetchone()
            if not row:
                return ""
            con.execute("UPDATE proposal_keys SET sightings = sightings + 1 WHERE key = ?", (key,))
            return row["proposal_id"]
    except Exception:  # noqa: BLE001
        return ""


def _remember_key(key: str, proposal_id: str) -> None:
    try:
        with _db() as con:
            con.execute("INSERT OR IGNORE INTO proposal_keys (key, proposal_id, first_seen)"
                        " VALUES (?,?,?)", (key, proposal_id, _now()))
    except Exception:  # noqa: BLE001
        pass


# ── detection ─────────────────────────────────────────────────────────
def _clean_sentence(text: str) -> str:
    t = _FENCE.sub(" ", str(text or ""))
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^\s*(?:also|and|but|so|well|ok|okay|hey)\b[,;:]?\s*", "", t, flags=re.I)
    return t.strip()


def classify(sentence: str) -> tuple:
    """Return (rule, kind, confidence) for one sentence, or ("", "", 0)."""
    text = _clean_sentence(sentence)
    if len(text) < MIN_SNIPPET or _NOISE.match(text):
        return ("", "", 0.0)
    for name, kind, base, pattern in RULES:
        if pattern.search(text):
            score = base
            # An imperative that names a specific thing is a stronger signal than
            # a vague grumble, and a longer rule carries more of the intent.
            if re.search(r"\b(?:use|run|call|write|name|format|import|install|"
                         r"keep|put|add|avoid)\b", text, re.I):
                score += 0.05
            if len(text) > 120:
                score += 0.05
            return (name, kind, min(round(score, 2), 0.95))
    return ("", "", 0.0)


def _tool_failures(messages: list[dict]) -> dict:
    """Counts errors per tool across the assistant turns."""
    counts: dict = {}
    for m in messages:
        for t in (m.get("tools") or []):
            if t.get("is_error"):
                name = str(t.get("name") or "tool")
                counts[name] = counts.get(name, 0) + 1
    return counts


def detect(session: dict) -> list[dict]:
    """Every learning moment in one session, with its provenance."""
    messages = session.get("messages") or []
    failures = _tool_failures(messages)
    found = []
    for idx, m in enumerate(messages):
        if m.get("role") != "user":
            continue
        text = str(m.get("content") or "")
        if not text.strip():
            continue
        for sentence in _SENTENCE.split(text):
            rule, kind, score = classify(sentence)
            if not rule:
                continue
            clean = _clean_sentence(sentence)[:MAX_SNIPPET]
            why = f"the word pattern for {rule.replace('_', ' ')}"
            # A correction that arrives after a tool has already failed twice is
            # the strongest signal available: the user is fixing a real problem.
            if failures and idx > 0:
                repeated = [n for n, c in failures.items() if c >= 2]
                if repeated and rule in ("correction", "standing_order"):
                    score = min(round(score + 0.15, 2), 0.95)
                    why += (f", and {repeated[0]} had already failed "
                            f"{failures[repeated[0]]} times in this session")
            found.append({
                "rule": rule, "kind": kind, "confidence_score": score,
                "content": clean, "turn": idx, "why": why,
                "snippet": text[:MAX_SNIPPET],
            })
            if len(found) >= MAX_PROPOSALS_PER_SESSION:
                return found
    return found


# ── turning a moment into an artifact ─────────────────────────────────
def to_proposal(session: dict, moment: dict) -> dict:
    """Store the moment as a proposal with its provenance. Never applies it."""
    rows = learning.load()
    key = _key_of(moment["content"])
    existing = _seen_before(key)
    if existing:
        prior = next((r for r in rows if r.get("id") == existing), None)
        if prior is not None:
            # Same rule again: raise confidence rather than pile up duplicates.
            prior["confidence_score"] = min(
                round(float(prior.get("confidence_score") or 0.5) + 0.05, 2), 0.95)
            prior["updated_at"] = _now()
            learning._save(rows)
            return {"ok": True, "artifact_id": existing, "duplicate": True}

    artifact_id = "l_" + uuid.uuid4().hex[:10]
    provenance = {
        "session_id": session.get("id") or "",
        "session_title": session.get("title") or "",
        "turn": int(moment["turn"]),
        "snippet": moment["snippet"],
        "why": moment["why"],
        "rule": moment["rule"],
        "engine": "heuristics",
        "detected_at": _now(),
    }
    body = moment["content"]
    row = {
        "id": artifact_id,
        "kind": moment["kind"] if moment["kind"] in learning.KINDS else "rule",
        "title": body[:120],
        "body": body,
        "scope": "global",
        "scope_key": "",
        "confidence": ("high" if moment["confidence_score"] >= 0.75
                       else "medium" if moment["confidence_score"] >= 0.6 else "low"),
        "confidence_score": moment["confidence_score"],
        "risk": learning.risk_of(body),
        "state": "proposed",
        "origin": "analyzer",
        "provenance": provenance,
        "source_session": session.get("id") or "",
        "created_at": _now(), "updated_at": _now(),
        "last_used_at": 0, "use_count": 0,
        "tokens": learning.tokens.estimate_tokens(body),
        "wrote": "",
    }
    rows.append(row)
    learning._save(rows)
    _remember_key(key, artifact_id)
    audit.record("proposal_generated", session=row["source_session"],
                 backend="analyzer", status="ok", proposal_id=artifact_id,
                 kind=row["kind"], confidence=row["confidence_score"],
                 reason=moment["why"], snippet=moment["snippet"][:200])
    return {"ok": True, "artifact_id": artifact_id, "duplicate": False}


# ── the sweep ─────────────────────────────────────────────────────────
def analyze_session(session: dict, force: bool = False) -> dict:
    messages = session.get("messages") or []
    if not messages:
        return {"session_id": session.get("id"), "proposals": 0, "skipped": "empty"}
    sid = session.get("id") or ""
    fingerprint = _fingerprint(messages)
    if not force and already_analyzed(sid, fingerprint):
        return {"session_id": sid, "proposals": 0, "skipped": "already read"}

    moments = detect(session)
    made = 0
    for moment in moments:
        res = to_proposal(session, moment)
        # A repeat of a rule already proposed is not a new proposal: it raised
        # that one's confidence instead. Counting it here would make the sweep
        # look busier than it was.
        if res.get("ok") and not res.get("duplicate"):
            made += 1
    _mark(sid, fingerprint, len(messages), made, "heuristics")
    return {"session_id": sid, "proposals": made, "skipped": ""}


def run_once(limit: int = 40, force: bool = False) -> dict:
    """Read every session that is new or has grown since the last sweep."""
    rows = store.list_sessions()
    rows.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    created = 0
    read = 0
    for meta in rows[:max(1, int(limit))]:
        rec = store.get(meta.get("id") or "")
        if not rec:
            continue
        res = analyze_session(rec, force=force)
        if not res["skipped"]:
            read += 1
            created += res["proposals"]
    audit.record("analyzer_run", backend="analyzer", status="ok",
                 sessions_read=read, proposals=created)
    return {"ok": True, "sessions_read": read, "proposals": created}


def status() -> dict:
    try:
        with _db() as con:
            sessions_read = con.execute("SELECT COUNT(*) AS c FROM analyzer_runs").fetchone()["c"]
            created = con.execute("SELECT COALESCE(SUM(created),0) AS c FROM analyzer_runs").fetchone()["c"]
            keys = con.execute("SELECT COUNT(*) AS c FROM proposal_keys").fetchone()["c"]
    except Exception:  # noqa: BLE001
        runs = created = keys = 0
    return {"enabled": enabled(), "sessions_read": sessions_read, "proposals_made": int(created),
            "rules_seen": keys, "mode": learning.mode(), "interval_s": interval(),
            "engine": "heuristics", "model_pass": False}


# ── settings ──────────────────────────────────────────────────────────
def _settings() -> dict:
    caps = providers.load()
    return caps.get("analyzer") or {}


def enabled() -> bool:
    """On by default while learning proposes. Off whenever learning is off.

    A default of ON is safe here only because the output is a proposal nobody has
    consulted yet. It still cannot change an answer on its own.
    """
    if learning.mode() == "learn-off":
        return False
    value = _settings().get("enabled")
    return True if value is None else bool(value)


def interval() -> int:
    try:
        return max(30, int(_settings().get("interval_s") or 120))
    except (TypeError, ValueError):
        return 120


def sync_worker() -> dict:
    """Align the background worker with the learning mode.

    The startup check is authoritative: learning off means the worker is not
    even started, and a mode change to off stops a running one. A change back
    to on starts it again — start() is idempotent while the thread is alive.
    Every path that changes the learning mode calls this.
    """
    if enabled():
        worker.start()
    else:
        worker.stop()
    return status()


def configure(enabled_flag: bool | None = None, interval_s: int | None = None) -> dict:
    patch = {}
    if enabled_flag is not None:
        patch["enabled"] = bool(enabled_flag)
    if interval_s is not None:
        patch["interval_s"] = max(30, int(interval_s))
    if patch:
        providers.save({"analyzer": patch})
        audit.record("analyzer_settings", backend="analyzer", status="ok", **patch)
    return status()


# ── the background worker ─────────────────────────────────────────────
class Worker:
    """A daemon thread that sweeps quietly. Stopping it costs nothing."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: dict = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tacit-analyzer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # First sweep waits a little so a server start is not competing with it.
        if self._stop.wait(20):
            return
        while not self._stop.is_set():
            if not enabled():
                # Learning is off (or the analyzer is switched off): the
                # worker is not needed, so it ends itself instead of waking
                # every interval to do nothing. A later mode change starts it
                # again — sync_worker() is the one hook for that.
                return
            try:
                self._last = run_once()
            except Exception:  # noqa: BLE001
                pass
            if self._stop.wait(interval()):
                return

    def last(self) -> dict:
        return self._last


worker = Worker()

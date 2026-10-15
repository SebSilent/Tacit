"""Who owns a plan: the session, in this process, on disk.

Plan mode used to be the socket's private business. The ``Plan`` object lived in
a dict inside the websocket handler, the generator that drove it lived in a
thread that dict started, and answering a question meant finding that same
socket again, so a reload during exploration lost the plan, a second window on
the same session could not see it, and a server restart made the questions
already on screen unanswerable. That is the same bug the connection rework
fixed for turns, in its second place: the work outlives the viewer, or the
viewer is the work's owner.

So this module mirrors ``session_runtime`` exactly. The plan runs in a thread
that belongs to a sid; its events go to ``turn_bus`` on the ``plan`` channel,
which buffers them for any window that arrives late; and a snapshot of where it
got to is written into the session record, so the plan survives the process too.
A socket that reconnects is handed the snapshot in ``hello`` and the buffered
events after it, and a socket that never comes back changes nothing about the
plan's progress.

The phases themselves are ``plan.py``'s generators. Nothing here re-implements
them; this is the part that decides *where* they run and who may start a second
one.
"""

from __future__ import annotations

import asyncio
import threading

from . import assistant, plan, session_state, store, turn_bus

CHANNEL = "plan"

_lock = threading.Lock()
# sid -> {"run": plan.Plan, "thread": Thread | None, "loop": loop}. The entry
# outlives its thread: a plan sitting on "awaiting_answers" is not running
# anything, and is still the plan this session is in the middle of.
_plans: dict[str, dict] = {}


# ── who is doing what ──────────────────────────────────────────────────────
def running(sid: str) -> bool:
    """A plan generator is executing for this session right now."""
    with _lock:
        entry = _plans.get(str(sid or ""))
    t = (entry or {}).get("thread")
    return bool(t and t.is_alive())


def has_plan(sid: str) -> bool:
    """There is a plan this session is in the middle of, running or waiting."""
    return _find(sid) is not None


def get_run(sid: str, rec: dict | None = None):
    """The plan object for this session, live first and disk second.

    The live object is what a running generator is mutating, so it always wins.
    The disk copy is the recovery path: after a restart there is no thread, but
    the questions the person is looking at are still in their session, and
    "no plan in progress" would be a lie they cannot act on.
    """
    with _lock:
        entry = _plans.get(str(sid or ""))
    if entry is not None:
        return entry["run"]
    return _find(sid, rec)


def _find(sid: str, rec: dict | None = None):
    """The plan for a sid: the one in memory, or one rebuilt from the record.

    Rebuilding needs the session's model, which is exactly the thing a restart
    put back in the record and never in the snapshot.
    """
    sid = str(sid or "")
    with _lock:
        entry = _plans.get(sid)
    if entry is not None:
        return entry["run"]
    try:
        rec = rec or store.get(sid) or {}
    except Exception:  # noqa: BLE001
        return None
    data = rec.get("planState")
    if not isinstance(data, dict) or not (data.get("prompt") or data.get("text")):
        return None
    if data.get("phase") in ("cancelled", "failed"):
        return None
    return plan.restore(data, sid, rec.get("model") or None)


def snapshot(sid: str, rec: dict | None = None) -> dict | None:
    """What to tell a viewer that just arrived: the plan as it stands.

    Memory first, because a generator is mid-phase more often than the disk has
    caught up. The record is the fallback and carries plans that finished, which
    is what lets a reload still show an approved plan rather than an empty strip.
    """
    p = get_run(sid, rec)
    if p is not None:
        out = plan.snapshot(p)
        out["running"] = running(sid)
        return out
    sid = str(sid or "")
    try:
        rec = rec or store.get(sid) or {}
    except Exception:  # noqa: BLE001
        return None
    data = rec.get("planState")
    if isinstance(data, dict):
        out = dict(data)
        out["running"] = False
        return out
    return None


def _persist(p) -> None:
    """Write the snapshot where any window can read it back.

    A failure here is not the plan's failure: the run is in memory and on screen
    regardless, and a session that cannot be written would have lost the turn's
    transcript too. So it is swallowed rather than ending a plan the person is
    watching.
    """
    try:
        store.patch(p.sid, planState=plan.snapshot(p))
    except Exception:  # noqa: BLE001
        pass


# ── the driver ─────────────────────────────────────────────────────────────
def _launch(sid: str, p, gen, loop: asyncio.AbstractEventLoop | None) -> None:
    """Run a plan generator to completion, publishing as it goes.

    The generator is blocking work (model calls, subagents) so it runs on its
    own thread; its events go to the session's ``plan`` channel rather than to
    whichever socket happened to ask. ``session_state`` carries busy for the
    duration, so the rest of the interface treats a plan the way it treats a
    turn, including this same session's other windows.
    """
    with _lock:
        entry = _plans.setdefault(sid, {"run": p, "thread": None, "loop": loop})
        entry["run"] = p
        entry["loop"] = loop

    def worker():
        last = None
        try:
            for ev in gen:
                session_state.running(sid)
                ev = {**(ev or {}), "sid": sid}
                turn_bus.publish(sid, ev, channel=CHANNEL)
                phase = ev.get("phase")
                # Persist on movement, not on every event: the explorers'
                # activity lines change dozens of times a phase, and a disk
                # write per line buys a reload nothing the snapshot above does
                # not already answer.
                if ev.get("type") in ("plan_questions", "plan_ready") or \
                        (ev.get("type") == "plan_status" and phase != last):
                    _persist(p)
                    last = phase
        except Exception as exc:  # noqa: BLE001 — a plan that dies says why
            turn_bus.publish(sid, {"type": "plan_status", "phase": "failed",
                                   "detail": str(exc), "explorers": [], "sid": sid},
                             channel=CHANNEL)
            p.phase = "failed"
            p.detail = str(exc)
        finally:
            turn_bus.finish(sid, channel=CHANNEL)
            session_state.end(sid)
            with _lock:
                e = _plans.get(sid)
                if e is not None:
                    e["thread"] = None
            _persist(p)

    session_state.begin(sid, CHANNEL)
    turn_bus.start(sid, loop, channel=CHANNEL)
    t = threading.Thread(target=worker, daemon=True, name=f"plan-{sid[:8]}")
    with _lock:
        _plans.setdefault(sid, {"run": p, "thread": t, "loop": loop})["thread"] = t
    t.start()


def _busy(sid: str) -> str | None:
    """Why a plan may not start here, or None if it may.

    Turns and plans share the session's busy flag but not its channel, and both
    answers matter: a plan started over a running turn would have two agents
    editing the same project, and a second plan over a running plan would put
    two generators in one ``Plan`` object.
    """
    if session_state.busy(sid) or running(sid):
        return "something is already running in this session"
    if assistant_running(sid):
        return "the assistant is replying"
    return None


def assistant_running(sid: str) -> bool:
    """Ask the runtime, do not duplicate its registry."""
    from . import session_runtime
    return session_runtime.assistant_running(sid)


# ── the operations a viewer can ask for ────────────────────────────────────
def start(sid: str, rec: dict, text: str,
          loop: asyncio.AbstractEventLoop | None = None) -> dict:
    """Plan a request from the beginning.

    The transcript append and the title stay with the socket handler that owns
    the conversation; what belongs here is the run itself, and the guard that
    keeps one session from having two.
    """
    sid = str(rec.get("id") or sid or "")
    why = _busy(sid)
    if why:
        return {"ok": False, "error": why}
    p = plan.Plan(sid, text, rec.get("project") or None, rec.get("model") or None)
    _launch(sid, p, plan.explore(p), loop)
    return {"ok": True, "sid": sid}


def answers(sid: str, rec: dict, text: str,
            loop: asyncio.AbstractEventLoop | None = None) -> dict:
    """Take the person's answers and write the plan they imply.

    The answers arrive as the block of ``Q:/A:`` lines the strip builds, because
    that is the shape the synthesiser was already given before this moved
    server-side; changing it would be a change to the prompt, not to the owner.
    """
    sid = str(rec.get("id") or sid or "")
    p = get_run(sid, rec)
    if p is None:
        return {"ok": False, "error": "no plan in progress"}
    if running(sid):
        return {"ok": False, "error": "the plan is still exploring"}
    why = _busy(sid)
    if why:
        return {"ok": False, "error": why}
    p.answers = {"answers": text} if isinstance(text, str) else dict(text or {})
    _launch(sid, p, plan.synthesise(p, p.answers), loop)
    return {"ok": True, "sid": sid}


def approve(sid: str, rec: dict, approved: bool) -> dict:
    """Approve or reject: save the plan, and put it in the project.

    Returns the ``plan.py`` result unchanged so the socket's ``plan_approved``
    message keeps the shape the strip already draws, plus the persisted
    snapshot, which is what makes the approval survive the reload that the
    person will almost certainly do next.
    """
    sid = str(rec.get("id") or sid or "")
    p = get_run(sid, rec)
    if p is None:
        return {"saved": False, "message": "no plan to approve"}
    res = plan.approve(p, bool(approved))
    _persist(p)
    return res


def abort(sid: str) -> dict:
    """Stop a plan, and say which kind of stop it was.

    A generator still running gets its ``cancelled`` flag and finishes itself,
    which is the difference between a plan that stopped and a thread that was
    dropped: the subagents it spawned still unwind. A plan that is only waiting
    for answers has nothing to stop, so it is simply removed.
    """
    sid = str(sid or "")
    with _lock:
        entry = _plans.get(sid)
    p = (entry or {}).get("run")
    if p is None:
        p = _find(sid)
    if p is None:
        return {"ok": False, "error": "no plan in progress"}
    p.cancelled = True
    p.phase = "cancelled"
    p.detail = "cancelled"
    _persist(p)
    if not running(sid):
        with _lock:
            _plans.pop(sid, None)
        turn_bus.publish(sid, {"type": "plan_status", "phase": "cancelled",
                               "detail": "cancelled", "explorers": [], "sid": sid},
                         channel=CHANNEL)
    return {"ok": True, "sid": sid}


def implement(sid: str, rec: dict) -> dict:
    """Hand the approved plan to a session with nothing else in it.

    The fresh session is created here rather than by the viewer because the plan
    is no longer the viewer's to carry: which session implements the work is a
    fact about the work. Planning chatter stays behind on purpose — the context
    that produced a plan is the worst place to implement it.
    """
    sid = str(rec.get("id") or sid or "")
    p = get_run(sid, rec)
    if p is None or not (p.text or "").strip():
        return {"ok": False, "error": "approve a plan first"}
    if running(sid):
        return {"ok": False, "error": "the plan is still working"}
    prompt = plan.implement_prompt(p)
    with _lock:
        _plans.pop(sid, None)
    fresh = store.create(model=rec.get("model") or "",
                         mode=rec.get("mode") or "agent",
                         thinking=rec.get("thinking") or "default",
                         project=rec.get("project") or "")
    return {"ok": True, "data": {"sid": fresh["id"], "name": fresh["title"],
                                 "prompt": prompt, "plan_file": p.plan_file,
                                 "context_file": p.context_file}}


def shutdown() -> int:
    """Cancel every plan this process is running, and report how many.

    Same reasoning as the turn workers: a plan generator that outlives the server
    is a thread making model calls for a session nobody can open. The flag is
    checked between explorers, so this stops the run at the next boundary rather
    than in the middle of a call.
    """
    with _lock:
        sids = [s for s, e in _plans.items() if e.get("thread") and e["thread"].is_alive()]
    for sid in sids:
        abort(sid)
    return len(sids)

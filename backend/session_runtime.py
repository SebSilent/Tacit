"""The session runtime: the server-side owner of every session's work.

A session id is the anchor. Everything a session runs, its agent turn, its
assistant turn, is started, tracked and finished HERE, keyed by sid, never by
a socket. A WebSocket is a viewer and a control channel: it subscribes to the
session's streams and forwards a few commands, and it owns nothing. A browser
that closes, reloads or switches sessions changes nothing about a running
turn: the worker keeps running, the bus keeps buffering, the transcript is
saved by the turn's own epilogue, and any window, this one, a second one,
another device, that opens the session subscribes and is caught up from the
buffer.

A session normally holds two workers, both its own:

- the **turn worker** (``backend/turn_worker.py``, role ``tacit-turn-worker``),
  one process per agent turn, killable, bound to the sid at spawn;
- the **assistant worker** (role ``tacit-assistant-worker``), one process per
  assistant reply, the same machinery, the same two-stage stop.

Sub-agents and research runs are extra workers on top, and there is no limit
on them: they are read-only and independent, so they run concurrently.

The two workers are independent of each other by design, an assistant reply
may stream while the agent's turn runs, which is why the assistant is tracked
here rather than in ``session_state`` (whose single busy row would make the
two mutually exclusive).

Everything blocking that a start performs, compaction's summariser call, the
spawn, runs in the caller's executor thread, never on the event loop: a slow
summariser or a terminal command must not freeze every other session's
stream, which is exactly what the old socket-owned path did.
"""

from __future__ import annotations

import asyncio
import threading
import time

from . import (agent, assistant, audit, config, extras, mcp_registry, memory_modes,
               memory_store, metrics, plugin_manager, project_context,
               session_state, store, turn_bus, turn_worker)
from . import tokens as token_mod
from .ai import prompts

_lock = threading.Lock()
# sid -> {"stop": Event, "steer": list, "worker": TurnWorker | None,
#         "thread": Thread}. Present exactly while the turn runs.
_turns: dict[str, dict] = {}
# sid -> {"stop": Event, "worker": TurnWorker | None, "thread": Thread}
_assistants: dict[str, dict] = {}


def turn_running(sid: str) -> bool:
    with _lock:
        return sid in _turns


def assistant_running(sid: str) -> bool:
    with _lock:
        return sid in _assistants


# ── autonomous mode ────────────────────────────────────────────────────────
# How much of the transcript a cycle prompt reads. The person's own setting is
# never touched by it, this is only what the harness asks for when the harness
# is the one asking, which is after every turn. A wide window here is a wide
# bill multiplied by the cycle count, so the loop reads less than a question.
CYCLE_TURNS = 5
# The Goal and the counters live on disk, read afresh at every decision point.
# A driver thread holds a ``rec`` that may be many minutes old, and the person
# can change the Goal or switch the mode off from the panel while it runs: a
# cached copy would let a stopped run carry on. Disk is the only honest source.
def autonomy_state(sid: str) -> dict:
    try:
        return assistant.autonomy_of(store.get(sid) or {})
    except Exception:  # noqa: BLE001
        return dict(assistant.AUTONOMY_DEFAULTS)


def _autonomy_write(sid: str, **changes) -> dict:
    """Merge changes into the stored state and write it. Returns the new state."""
    try:
        rec = store.get(sid) or {}
        state = assistant.save_autonomy(rec, changes)
        store.patch(sid, assistantAutonomy=state)
        return state
    except Exception:  # noqa: BLE001
        return autonomy_state(sid)


def _autonomy_bump(sid: str, **counts) -> dict:
    try:
        rec = store.get(sid) or {}
        state = assistant.bump_counters(rec, **counts)
        store.patch(sid, assistantAutonomy=state)
        return state
    except Exception:  # noqa: BLE001
        return autonomy_state(sid)


def _autonomy_launch(sid: str, *, ok: bool) -> dict:
    """Say whether this cycle's launch worked, and return the state after it.

    Separate from ``_autonomy_bump`` because a launch that never happened
    charges no cycle — the budget pays for work, not for attempts — but it does
    pay attention, so the consecutive count has to move on its own.
    """
    try:
        rec = store.get(sid) or {}
        state = assistant.note_launch(rec, ok=ok)
        store.patch(sid, assistantAutonomy=state)
        return state
    except Exception:  # noqa: BLE001
        return autonomy_state(sid)


def _autonomy_off(sid: str, reason: str) -> bool:
    """Switch the mode off and say why in the pane.

    Returns whether the mode was actually running when this happened. The Stop
    button needs the difference between "this ended a run" and "this stopped
    nothing", and the notify is only worth showing for the first of those, a
    reason is stored either way, because the person may not be watching when it
    stops and will ask later what it stopped on.
    """
    was = autonomy_state(sid).get("enabled")
    _autonomy_write(sid, enabled=False, reason=reason)
    if was:
        # publish resolves the bus's own loop, so this is safe to call from a
        # driver thread or from the event loop, and is a no-op when nobody is
        # streaming this session, the reason is on disk either way.
        try:
            turn_bus.publish(sid, {"type": "notify", "level": "info", "sid": sid,
                                   "message": f"autonomous mode stopped, {reason}"})
        except Exception:  # noqa: BLE001
            pass
    return bool(was)


def _autonomy_note(sid: str, reason: str) -> dict:
    """Record why a cycle paused without ending the run.

    _autonomy_off is for a run that is over. A start refused against a race is
    not one: the reply that beat us to the spawn has an epilogue of its own,
    which continues the chain, so disabling the mode here would be the exact
    bug the guard above exists to prevent. enabled is left as it was found.

    The reason is still stored and still shown. A pane that reads "running"
    while nothing has moved for a minute is a pane nobody trusts, so this is
    not a silent no-op, it is the difference between "waiting" and "dead".
    """
    state = _autonomy_write(sid, reason=reason)
    if state.get("enabled"):
        try:
            turn_bus.publish(sid, {"type": "notify", "level": "info", "sid": sid,
                                   "message": f"autonomous mode waiting, {reason}"})
        except Exception:  # noqa: BLE001
            pass
    return state


def goal_set(sid: str, text: str) -> str:
    """Pin (or clear) the Goal. Written through patch, so it lands the same
    second whether it came from the panel or the socket, and the next turn —
    including a turn already in flight, since the block is read from disk —
    sees it."""
    goal = str(text or "").strip()[:20000]
    store.patch(sid, assistantGoal=goal)
    return goal


def autonomy_set(sid: str, *, enabled=None, max_cycles=None, max_hours=None,
                 reason=None) -> dict:
    """Switch the mode and/or move its limits, and return the whole new state.

    Enabling zeroes the tally. The person pressing the switch means "run this
    now", not "add to whatever the last run had already spent", and a run that
    resumed an old count would look like it had lost its budget on its own.
    """
    changes = {}
    for key, value in (("enabled", enabled), ("max_cycles", max_cycles),
                       ("max_hours", max_hours), ("reason", reason)):
        if value is not None:
            changes[key] = value
    rec = store.get(sid) or {}
    # Read the state BEFORE save_autonomy, which mutates rec in place. Deciding
    # "is this a fresh start" from rec afterwards would compare the new value
    # against itself and never see a transition, and it would only appear to
    # work, because the disk copy still lags until the patch below.
    was_on = bool(assistant.autonomy_of(rec).get("enabled"))
    state = assistant.save_autonomy(rec, changes)
    turning_on = bool(enabled) and not was_on
    if turning_on:
        if not assistant.goal_of(rec):
            # Report the switch as it actually is: off. save_autonomy has already
            # written enabled=True into the state it returns, and the pane renders
            # straight from this payload (assistant.js: `autonomy = m.autonomy`,
            # then renderRun), so a refusal that echoed enabled=True would show an
            # armed run and a live "running" budget line while nothing on disk is
            # armed, and the code comment claiming the switch "snapped back" would
            # be describing a thing that never happened.
            state["enabled"] = False
            return dict(state, ok=False,
                        error="set a Goal first, there is nothing to work toward")
        state = assistant.reset_counters(rec)
    store.patch(sid, assistantAutonomy=state)
    if turning_on and state.get("enabled"):
        try:
            turn_bus.publish(sid, {"type": "notify", "level": "info", "sid": sid,
                                   "message": "autonomous mode is running, the Assistant "
                                              "will hand the agent its next prompt when "
                                              "this turn ends"})
        except Exception:  # noqa: BLE001
            pass
    return dict(state, ok=True)


def autonomy_reset(sid: str) -> dict:
    """Zero the spend without touching the limits, the Goal, or whether it runs."""
    rec = store.get(sid) or {}
    state = assistant.reset_counters(rec)
    store.patch(sid, assistantAutonomy=state)
    return state


def _autonomy_after_turn(sid: str, rec: dict, loop) -> None:
    """A turn ended: charge it to the budget, then let the Assistant speak.

    The order matters. The cycle is charged before the limit is read, so a run
    with one turn left in its budget gets that turn and no more.
    """
    state = autonomy_state(sid)
    if not state.get("enabled"):
        return
    ok, why = assistant.budget_left(state)
    if not ok:
        _autonomy_off(sid, why)
        return
    if assistant_running(sid):
        # The mirror of the guard in _autonomy_after_assistant: the person asked
        # the Assistant something by hand and that reply is still in flight. It
        # is not a second chain, its own epilogue is what continues this one,
        # so there is nothing to start and nothing to cancel. Starting anyway
        # would fail with "already running", which reads as a dead run.
        return
    try:
        res = start_assistant(sid, rec, assistant.CYCLE_PROMPT, loop=loop,
                              turns=CYCLE_TURNS)
    except Exception as exc:  # noqa: BLE001
        _autonomy_off(sid, f"the Assistant could not be started ({exc})")
        return
    if res and not res.get("ok"):
        # A refused start is not always fatal to the run: the one case that
        # reaches here is a race against the guard above, where a reply began
        # between the check and the spawn. Leave the mode alone and let that
        # reply's epilogue carry on.
        _autonomy_note(sid, res.get("error") or "the Assistant could not start")


def _autonomy_after_assistant(sid: str, rec: dict, loop) -> None:
    """The Assistant answered: its last fenced block is the next agent prompt.

    No block means the model had nothing to say about the work, which the
    contract reads as "carry on", so the harness sends a bare continue rather
    than ending a run the person started. A block of ``DONE`` ends it.
    """
    state = autonomy_state(sid)
    if not state.get("enabled"):
        return
    if turn_running(sid):
        # The race this exists for: the person asked the Assistant something by
        # hand while an agent turn was mid-flight. That turn's own epilogue
        # continues the chain, so this reply has nothing to hand off to, and
        # calling start_turn here would fail with "a turn is already running",
        # which the branch below reads as fatal and would end a run the person
        # never stopped. Returning WITHOUT disabling is the whole point: a
        # manual question must not cost the run its remaining budget.
        return
    rows = (store.get(sid) or {}).get("assistant") or []
    answer = ""
    for row in reversed(rows):
        if row.get("role") == "assistant" and str(row.get("content") or "").strip():
            answer = row.get("content") or ""
            break
    prompt = assistant.extract_prompt(answer).strip()
    if prompt.upper() in ("DONE", "`DONE`", "**DONE**"):
        _autonomy_off(sid, "the Assistant reported the Goal met")
        return
    ok, why = assistant.budget_left(state)
    if not ok:
        _autonomy_off(sid, why)
        return
    try:
        out = start_turn(sid, rec, prompt or assistant.CONTINUE_PROMPT, loop=loop,
                     origin="autonomous")
    except Exception as exc:  # noqa: BLE001
        # An exception is not a hiccup: the spawn itself threw, so there is no
        # second attempt to hope for. End it now.
        _autonomy_launch(sid, ok=False)
        _autonomy_off(sid, f"the agent could not be started ({exc})")
        return
    if not (out or {}).get("ok"):
        err = (out or {}).get("error") or ""
        # The same race as the guard above, arriving between the check and the
        # spawn: a turn started while this reply was being read. It is not a
        # dead run (that turn's epilogue continues the chain) so ending the
        # mode here would let a person typing a question cost the run its
        # remaining budget, which is the failure this whole branch exists to
        # prevent. Note it, do not stop it.
        if "already running" in err:
            # Not counted as a failure either: nothing went wrong with the run.
            _autonomy_note(sid, "an agent turn was already in flight, that turn continues the chain")
            return
        # One refusal is noted and the loop tries again next cycle; two in a
        # row ends it. The person may not be watching, and a run that cannot
        # launch but keeps being asked to is the worst kind of alive.
        after = _autonomy_launch(sid, ok=False)
        if int(after.get("failures") or 0) >= assistant.MAX_LAUNCH_FAILURES:
            _autonomy_off(sid, f"{err or 'the agent could not be started'}, twice in a row")
        else:
            _autonomy_note(sid, err or "the agent could not be started")
        return
    # Charged only now: a cycle counts when it launches, not when it is
    # attempted. Bumping before the start meant a start refused against a race
    # still spent a turn of a budget the person never used. A launch that
    # worked also clears the failure count, so the count means *consecutive*.
    _autonomy_launch(sid, ok=True)
    after = _autonomy_bump(sid, cycles=1)
    # A launched cycle used to be silent on the bus: nothing separated "a new
    # cycle started" from the turn events that followed it, so an audible tick had
    # no moment to attach to. Published once per *successful* launch, a refused
    # launch is not new work, and a tick announcing work that never started is
    # worse than no tick.
    turn_bus.publish(sid, {"type": "autonomy_cycle", "sid": sid,
                           "cycle": int(after.get("cycles") or 0)})


# ── shared helpers (moved here from the socket handler they used to live in) ─
def _context_window(rec: dict) -> int:
    """This session's model context window, or 0 when it is not known."""
    ctx = (rec.get("usage") or {}).get("context") or {}
    window = ctx.get("window") or 0
    if window:
        return int(window)
    try:
        return int((config.resolve_model(rec.get("model")) or {}).get("contextWindow") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _set_context(rec: dict, tokens: int) -> None:
    """Write one number into the meter, and the percentage it implies."""
    u = rec.setdefault("usage", {})
    window = _context_window(rec)
    ctx = u.setdefault("context", {})
    ctx["tokens"] = max(1, int(tokens))
    ctx["window"] = window
    ctx["percent"] = round(ctx["tokens"] / window * 100, 1) if window else None
    ctx["reserve"] = max(0, window - config.context_fill_target(window)) if window else 0


def _raise_context(rec: dict, messages: list[dict]) -> None:
    """Count the request that is about to be sent, if it is bigger than the meter.

    The provider's figure describes the call that already happened. Between that
    call and this one the transcript gained a user turn, the standing prompt, the
    project's instructions, the task list and this turn's memory, and a session
    that has just been sent a new message cannot honestly be shown as no fuller
    than it was before. So the meter is raised to whichever is larger: what the
    provider billed, or what is assembled here. The provider keeps the seat when
    its number is the bigger one, which it usually is, because it alone counts
    the tool schemas.

    The local figure is measured in the same units the mid-turn trigger uses
    (:func:`agent.wire_chars` against ``context_budget``), so the bar and the
    compaction that is supposed to follow it cannot disagree about the size.
    """
    if not messages:
        return
    local = agent.wire_chars(messages) // token_mod.CHARS_PER_TOKEN
    current = int(((rec.get("usage") or {}).get("context") or {}).get("tokens") or 0)
    if local > current:
        _set_context(rec, local)


def _usage_event(rec: dict, usage: dict, subagent: bool = False) -> dict:
    u = rec.setdefault("usage", {})
    tok = u.setdefault("tokens", {"input": 0, "output": 0, "total": 0,
                                  "cacheRead": 0, "cacheWrite": 0})
    # One arithmetic for the live meter and the headless benchmark both: the
    # parsing lives in metrics.normalize_usage so the two can never drift apart.
    n = metrics.normalize_usage(usage)
    in_tok = n["input"]
    out_tok = n["output"]
    tok["input"] += in_tok
    tok["output"] += out_tok
    tok["total"] += n["total"]
    # The names the token meter already reads. A turn re-sends its transcript on
    # every step, so these two numbers are the difference between a cheap long
    # session and an expensive one, and they were always blank.
    tok["cacheRead"] = int(tok.get("cacheRead") or 0) + n["cache_read"]
    tok["cacheWrite"] = int(tok.get("cacheWrite") or 0) + n["cache_write"]
    if n["cache_read"]:
        metrics.bump(rec, cached_tokens=n["cache_read"])

    prev = dict(u.get("context") or {})
    window = prev.get("window") or _context_window(rec)
    # A call that reported no usage used to write None over the meter, so the
    # interface showed no context percentage at all on a session that had just
    # billed half a million tokens. Keep the last real figure, and fall back to a
    # local measure of the transcript so the number is never simply absent.
    #
    # `in_tok` is the whole prompt of that call, cached bytes included, because
    # that is what occupied the window, both producers normalize to that basis
    # (`anthropic.usage_of`, `engine._usage`). Adding `out_tok` is what makes the
    # meter read *forward*: the next call re-sends this prompt plus the reply it
    # just got, so the two together are the size of the request about to be made.
    # Measuring only what was billed fresh would leave a cached session reading
    # near zero forever, and `should_compact`, which reads this same number,
    # would never fire.
    ctx_tokens = (in_tok + out_tok) or None
    if subagent:
        # Billed here, but it says nothing about how full the main window is.
        ctx_tokens = prev.get("tokens")
    if not ctx_tokens:
        ctx_tokens = prev.get("tokens") or max(
            1, agent._chars(rec.get("messages") or []) // token_mod.CHARS_PER_TOKEN)
    percent = round(ctx_tokens / window * 100, 1) if (window and ctx_tokens) else None
    ctx = {"tokens": ctx_tokens, "window": window, "percent": percent,
           "reserve": max(0, window - config.context_fill_target(window)) if window else 0}
    u["context"] = ctx
    u["compacting"] = False
    u["speed"] = 0
    metrics.bump(rec, prompt_tokens=in_tok, completion_tokens=out_tok)
    return {"type": "usage", "tokens": tok, "context": ctx, "compacting": False, "speed": 0,
            "subagent": subagent}


def _lower_context(rec: dict, saved: int) -> None:
    """Move the meter down when a compaction reclaims tokens from the transcript.

    ``usage.context.tokens`` is the size of the last call's prompt, and a
    compaction rewrites that prompt without a call being made, so the number
    would otherwise go on describing a transcript that no longer exists: the
    interface would look un-compacted until the next turn reported, and
    :func:`agent.should_compact`, which reads this same field, would judge
    against it. The saving is measured on the provider's own basis, so it is
    subtracted rather than re-estimated locally.
    """
    ctx = (rec.get("usage") or {}).get("context")
    saved = int(saved or 0)
    if not ctx or not saved:
        return
    # Subtracted, exactly as this function's name and docstring say. It used to
    # read `max(current, saved)`, which can only ever hold the line up or push it
    # past the real figure: a compaction that reclaimed 300,000 tokens left the
    # bar at 99% and the trigger judging against a number that had just been
    # written off, so the interface disagreed with the record it described.
    # The saving is measured on the stored record and the meter holds the last
    # billed prompt, which are not the same base: subtracting can land below the
    # transcript that actually exists, and a bar reading 0% right after a
    # compaction that kept 40,000 characters of work is its own kind of lie. The
    # record's own size is the floor; the provider corrects upward on the next
    # call, because it alone counts the tool schemas.
    floor = agent.stored_tokens(rec.get("messages") or [])
    tokens = max(1, int(ctx.get("tokens") or 0) - saved, floor)
    ctx["tokens"] = tokens
    window = ctx.get("window") or 0
    ctx["percent"] = round(tokens / window * 100, 1) if window else None


def summariser_name(ref: str | None) -> str:
    """Which model actually wrote a compaction note.

    Named in every notice that reports one, because a summary written by a model
    nobody picked is precisely the thing this program does not get to hide. The
    resolution may land on the Settings fallback when the session's own model is
    gone, and then the notice says so.
    """
    return (config.resolve_model(ref) or {}).get("ref") or ref or "the session model"


def compacting_notice(bus, sid: str):
    """A callable that tells every viewer of one session whether it is compacting.

    Sent on the bus rather than only to the socket that asked, because a hand
    compaction rewrites the transcript every window is looking at, and a turn
    started from a second device against a record mid-fold is the race the busy
    mark exists to stop.
    """
    def notice(on: bool) -> None:
        try:
            if bus is not None:
                bus.publish({"type": "compacting", "sid": sid, "on": bool(on)})
        except Exception:  # noqa: BLE001
            pass    # a viewer that cannot be told is not a reason to lose the fold
    return notice


def compact_now(rec: dict, *, force: bool = False, notice=None) -> dict | None:
    """Compact the stored record and account for it, in one place.

    Two callers need exactly this: the turn start, and the interface's forced
    "Compact context" command. The accounting used to live only in the turn start,
    so a hand-compaction rewrote the transcript and told nobody: the meter kept
    reading 99% over a record that had just been folded, and the dashboard's
    saved-by-compaction row stayed at zero. One owner for both, so the second
    caller cannot be forgotten again.

    The ``compacting`` flag goes up around the summariser because it is a model
    call and the caller waits on it. Nothing ever set that flag before, so the
    interface's "compacting…" pulse could not fire and a turn that paused for
    seconds to write a handover note read as a turn that had hung.
    """
    if not force and not agent.should_compact(rec):
        return None
    ref = rec.get("model") or None
    u = rec.setdefault("usage", {})
    u["compacting"] = True
    if notice:
        notice(True)
    try:
        info = agent.compact_history(rec, ref=ref, force=force)
    finally:
        u["compacting"] = False
        if notice:
            notice(False)
    if not info or not info.get("compacted"):
        return info
    saved = max(0, token_mod.estimate_tokens("x" * int(info.get("chars_before") or 0))
                - token_mod.estimate_tokens("x" * int(info.get("chars_after") or 0)))
    metrics.bump(rec, saved_compaction=saved)
    _lower_context(rec, saved)
    # Named in the notice, because which model wrote the handover note is a fact
    # about the session and not an implementation detail: the transcript is the
    # only place a person can see that the Settings fallback wrote it.
    info["saved_tokens"] = saved
    info["model"] = summariser_name(ref)
    return info


def _account_context(rec: dict) -> None:
    """Record what this turn is about to inject, and what it avoided."""
    try:
        report = mcp_registry.injection_report()
        tools = agent.tools_for()
        metrics.bump(rec, tool_schema_tokens=token_mod.estimate_tools_tokens(tools),
                     mcp_tool_tokens=report.get("injected_tokens") or 0,
                     saved_lazy_tools=report.get("saved_tokens") or 0)
    except Exception:  # noqa: BLE001
        pass


def _history(rec: dict) -> list[dict]:
    """Rebuild the model's view of the conversation from the stored transcript.

    The transcript is a display record, so calls and results live on the assistant
    message that made them. They are expanded back into the protocol shape here:
    an assistant message carrying tool_calls, followed by one tool message per call.
    Without that the model could not see anything it had done, and would read its
    own completed work as a promise it had not kept.
    """
    return agent.transcript_messages(rec.get("messages") or [],
                                     reasoning=agent.model_reasons(rec.get("model")))


def _content_parts(text: str, images) -> list:
    parts = [{"type": "text", "text": text or ""}]
    for im in images or []:
        mime = (im or {}).get("mimeType") or "image/png"
        data = (im or {}).get("data") or ""
        if data:
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{data}"}})
    return parts


def _memory_block(project: str | None) -> dict:
    """The budgeted memory selection, or an empty one when memory is off.

    The mode decides what may be injected, and every item is explained by the
    module that produced it. Memory never merges into the base prompt. The whole
    selection is returned rather than just its text, so the caller can report what
    the budget held back as well as what it let through.
    """
    try:
        return memory_modes.startup(project or "") or {}
    except Exception:  # noqa: BLE001
        return {}


def _title(text: str) -> str:
    line = " ".join((text or "").split())
    for prefix in ("can you please ", "can you ", "could you ", "please ", "i want you to ",
                   "i need you to ", "help me "):
        if line.lower().startswith(prefix):
            line = line[len(prefix):]
            break
    for q, a in (("how do i ", "how to "), ("how can i ", "how to "), ("what is ", "what is ")):
        if line.lower().startswith(q):
            line = a + line[len(q):]
            break
    words = line.split()
    short = " ".join(words[:8])
    return (short[:48].rstrip() or "New session")


# ── the turn's driver: the thread that owns a running turn ─────────────────
def _drive_turn(sid: str, rec: dict, messages: list[dict], *, project, ref, chat: bool,
                has_instructions: bool, reasoning, trace: list, run: dict,
                bus: "turn_bus.TurnBus") -> None:
    """Consume one turn's worker events, account for them, publish them.

    This thread belongs to the runtime, not to the socket that asked for the
    turn: it was started before :func:`start_turn` returned, so no browser
    event can prevent it from existing, and its ``finally`` — the epilogue —
    is the only writer of the transcript.
    """
    stop = run["stop"]
    steer_q = run["steer"]
    worker = run.get("worker")
    started = time.time()
    terminal = None
    # A compaction that ran inside the worker, held for the epilogue: the stored
    # transcript is written there, and it is the only place the reclamation can
    # still be seen by the next turn.
    pending: dict | None = None
    try:
        stream = (worker.events() if worker is not None else
                  agent.run_turn(messages, project=project, ref=ref, chat=chat,
                                 session=sid, has_instructions=has_instructions,
                                 stop=stop, steer=steer_q, reasoning=reasoning,
                                 trace=trace))
        for ev in stream:
            kind = ev.get("type")
            # Delegated work is invisible in the main transcript by design,
            # the sub-agent's calls never enter it, so the only way the
            # person can see what it is doing is a dedicated event.
            if kind in ("text", "reason", "tool_start", "tool_end", "notify") and \
                    (ev.get("subagent") or ev.get("research")):
                bus.publish({**ev, "type": "delegation_activity", "sid": sid})
            if kind == "usage":
                ev = _usage_event(rec, ev.get("usage") or {},
                                  subagent=bool(ev.get("subagent")))
            elif kind == "delegation":
                metrics.bump(rec, saved_subagent=int(ev.get("saved") or 0))
                ev = {"type": "notify", "level": "info",
                      "message": ("sub-agent spent "
                                  f"{token_mod.label(ev.get('spent') or 0)} tokens in its "
                                  "own context; "
                                  f"{token_mod.label(ev.get('saved') or 0)} kept out of "
                                  "this one")}
            elif kind == "compaction_start":
                # The summariser call is in flight inside the worker. The bar
                # pulses and the composer shuts for this session while it does, so
                # a person cannot start a second turn against a transcript that is
                # being rewritten, and cannot mistake the pause for a hung turn.
                rec.setdefault("usage", {})["compacting"] = True
                ev = {"type": "compacting", "on": True}
            elif kind == "compaction":
                # Mid-turn compaction is a real saving and has to reach the
                # ledger, or the dashboard's row stays at zero while the
                # turn it describes actually reclaimed the tokens.
                saved = int(ev.get("saved") or 0)
                metrics.bump(rec, saved_compaction=saved)
                # The meter is not moved here. The next call reports its own,
                # smaller prompt and the provider's number wins; if the turn ends
                # before another call, the epilogue's fold moves it once. Lowering
                # it here as well would subtract the same reclamation twice.
                rec.setdefault("usage", {})["compacting"] = False
                bus.publish({"type": "compacting", "on": False, "sid": sid})
                # Hold the note for the epilogue. The worker's transcript dies
                # with it, and `trace` is what rebuilds the record, so folding
                # here would fold away steps that have not been written yet: the
                # length of `trace` at this moment is the boundary the epilogue
                # needs to tell the kept tail from the work that came after it.
                if (ev.get("summary") or "").strip():
                    pending = {"summary": ev["summary"],
                               "kept_tokens": int(ev.get("kept_tokens") or 0),
                               "at": len(trace)}
                ev = {"type": "notify", "level": "info",
                      "message": (f"compacted {ev.get('compacted')} earlier steps "
                                  f"({ev.get('chars_before')} → {ev.get('chars_after')} "
                                  f"chars), written by {summariser_name(ref)}")}
            elif kind == "tool_start":
                rec["tool_calls"] = int(rec.get("tool_calls") or 0) + 1
            if kind in ("text", "reason", "usage", "tool_start", "tool_end"):
                session_state.running(sid)
            # One publish, once. The bus is the single fan-out point: every
            # viewer, the window that started the turn, a reload, a second
            # device, sees the same stream.
            if kind == "done":
                # Held back until the epilogue has written the record. `done`
                # is what tells a viewer the transcript is complete, and a
                # viewer that reloads on it reads the store, so the event
                # must never outrun the write it announces.
                terminal = {**ev, "sid": sid}
                continue
            bus.publish({**ev, "sid": sid})
    except Exception as e:  # noqa: BLE001
        bus.publish({"type": "error", "message": str(e), "sid": sid})
    finally:
        # An orphan is never left behind: a worker still alive after the
        # stream ended abnormally is stopped through the same two-stage path
        # the Stop button uses. A stream that ended normally has an exited
        # worker, so this is a no-op there.
        if worker is not None and worker.alive():
            try:
                worker.cancel()
            except Exception:  # noqa: BLE001
                pass
        # The epilogue belongs to the turn, not to the socket that happened
        # to start it: a browser that disconnects mid-turn must not take the
        # transcript save, the idle mark or the registry rows down with it.
        for step in trace:
            extra = {}
            if step.get("reason"):
                extra["reason"] = step["reason"]
            if step.get("tools"):
                extra["tools"] = step["tools"]
            if not (step.get("text") or "").strip() and not extra:
                continue
            store.append(rec, "assistant", step.get("text") or "", extra or None)
        # A compaction inside the turn only becomes real here. The worker folded
        # its own wire list to stay under the window, the parent writes the record
        # from `trace` — every step verbatim — and unless the note crosses that
        # boundary the session keeps the whole transcript while the meter, and
        # `should_compact` reading it, describe the shrunken request. That is the
        # pair of facts that let a session sit at 99% for sixteen turns with
        # millions of tokens reported as saved and nothing ever reclaimed.
        if pending is not None:
            reclaimed = agent.apply_stored_summary(
                rec, pending["summary"], pending["kept_tokens"],
                max(0, len(trace) - pending["at"]))
            if reclaimed:
                _lower_context(rec, reclaimed)
        store.save(rec)
        # Only now may the transcript be called complete.
        if terminal is not None:
            bus.publish(terminal)
        # The registry is the turn's own responsibility, not the socket's: a
        # browser that disconnects mid-turn must not mark the session idle
        # while its turn is still running in here.
        session_state.end(sid)
        with _lock:
            _turns.pop(sid, None)
        # Push side of the state verifier, turn end: every open view of this
        # session stops showing "working" the moment the turn ends.
        bus.publish({"type": "state_delta", "sid": sid, "busy": False, "starting": False})
        # Last: the stream. Emptying the buffer is what tells a viewer
        # arriving later that the transcript in the record is the whole
        # story; forgetting an unwatched bus is what keeps idle sessions free.
        turn_bus.finish(sid)
        # The autonomous chain runs after the stream is closed, so a cycle that
        # starts here publishes on a fresh bus rather than into one that viewers
        # have already been told to stop replaying.
        if not chat:
            try:
                _autonomy_bump(sid, work_seconds=time.time() - started)
                _autonomy_after_turn(sid, rec, run.get("loop"))
            except Exception:  # noqa: BLE001
                pass


def _drive_assistant(sid: str, rec: dict, mini: dict, text: str, cfg: dict,
                     trace: list, run: dict, bus: "turn_bus.TurnBus") -> None:
    """The assistant reply's driver: same shape as the turn's, its own channel."""
    stop = run["stop"]
    worker = run.get("worker")
    try:
        stream = (worker.events() if worker is not None else
                  assistant.run_turn(mini, text, cfg, stop=stop, trace=trace))
        for ev in stream:
            if ev.get("type") == "trace":
                # Kill-safe: the worker re-sends its cumulative trace, so the
                # last one seen is the most complete.
                trace[:] = list(ev.get("steps") or [])
                continue
            bus.publish({**ev, "type": "assistant_" + str(ev.get("type")), "sid": sid})
    except Exception as exc:  # noqa: BLE001
        bus.publish({"type": "assistant_error", "message": str(exc), "sid": sid})
    finally:
        if worker is not None and worker.alive():
            try:
                worker.cancel()
            except Exception:  # noqa: BLE001
                pass
        for step in trace:
            extra = {}
            if step.get("reason"):
                extra["reason"] = step["reason"]
            if step.get("tools"):
                extra["tools"] = step["tools"]
            if not (step.get("text") or "").strip() and not extra:
                continue
            assistant.append(rec, "assistant", step.get("text") or "", extra or None)
        store.save(rec)
        with _lock:
            _assistants.pop(sid, None)
        try:
            preview = assistant.preview(rec)
        except Exception:  # noqa: BLE001
            preview = {}
        bus.publish({"type": "assistant_saved", "sid": sid, "preview": preview})
        turn_bus.finish(sid, channel="assistant")
        # The handoff: whatever the Assistant ended with becomes the next agent
        # turn. Skipped when the person stopped this reply by hand, a Stop on
        # the Assistant is a full stop, not a suggestion to carry on.
        if not stop.is_set():
            try:
                _autonomy_after_assistant(sid, rec, run.get("loop"))
            except Exception:  # noqa: BLE001
                pass


# ── starting work ──────────────────────────────────────────────────────────
def start_turn(sid: str, rec: dict, text: str, images=None,
               loop: asyncio.AbstractEventLoop | None = None,
               origin: str = "you") -> dict:
    """Start the session's agent turn. Called from a socket; owned here.

    `origin` says who was at the keyboard: "you" (typed in the composer),
    "assistant" (the Send button) or "autonomous" (the loop). It is display
    metadata for the person's transcript and nothing more, the agent receives
    a plain user row either way, because it has no idea an Assistant exists.

    Runs in the caller's executor thread (the socket handler awaits it), so
    the blocking parts (a compaction summariser call, the spawn) never sit
    on the event loop. Returns ``{"ok": True}`` or ``{"ok": False, "error"}``;
    the caller turns a refusal into a notify.
    """
    text = str(text or "")
    if not text.strip():
        return {"ok": False, "error": "empty prompt"}
    if loop is None:
        loop = asyncio.get_running_loop()
    # The claim is atomic: guard, run row and busy mark under one lock, so two
    # windows prompting the same session at the same instant produce one turn
    # and one refusal, one session, one running turn.
    with _lock:
        if session_state.busy(sid):
            return {"ok": False, "error": "a turn is already running"}
        # The loop travels with the run so the autonomous chain can start the
        # next worker from a driver thread: the bus needs the server's loop,
        # and a driver thread has no running loop of its own.
        run = {"stop": threading.Event(), "steer": [], "worker": None,
               "thread": None, "loop": loop}
        _turns[sid] = run
    # The bus opens before the session is marked busy, so any viewer that can
    # observe busy can also subscribe to the turn behind it.
    bus = turn_bus.start(sid, loop)
    session_state.begin(sid, "turn")
    bus.publish({"type": "state_delta", "sid": sid, "busy": True, "starting": True})
    trace: list[dict] = []
    instructions: dict = {}
    project = rec.get("project") or None
    ref = rec.get("model") or None
    reasoning = config.reasoning_for(rec.get("thinking"))
    try:
        if not rec.get("model"):
            rec["model"] = config.registry().get("default") or ""
            ref = rec["model"] or None
        info = compact_now(rec, notice=compacting_notice(bus, sid))
        if info and info.get("compacted"):
            # Before the record is written, so a session that is reloaded
            # mid-compaction does not come back showing the old fill.
            store.save(rec)
            bus.publish({"type": "notify", "level": "info", "sid": sid,
                         "message": (f"compacted {info['compacted']} earlier messages "
                                     f"({info['chars_before']} \u2192 "
                                     f"{info['chars_after']} chars), written by "
                                     f"{info['model']}")})

        # Stored only when it is not the person, so an ordinary row stays
        # exactly the row it always was. It cannot reach the model: the wire
        # builder (agent.transcript_messages) rebuilds a user row as
        # {role, content} and drops every other key, which is the whole
        # point, the agent must not be able to tell the two senders apart.
        extra = None if origin == "you" else {"origin": origin}
        user_row = store.append(rec, "user", text, extra)
        if (rec.get("title") or "New session") == "New session":
            rec["title"] = _title(text)
        store.save(rec)
        bus.publish({"type": "state_delta", "sid": sid, "name": rec["title"],
                     "model": rec.get("model")})
        # Published, not merely stored. The browser echoes the person's own
        # prompt locally and the server used to say nothing about it, so a
        # prompt that did not come from this keyboard never got a row: with no
        # user row to end the previous group, ensureStreamingBubble() reused
        # the last assistant row and every delta of the new turn landed inside
        # it, one agent bubble growing forever instead of a conversation.
        # `ts` rides along because this event is the only way a window that did
        # not type the prompt learns when it was sent. Every viewer stamps its
        # own clock as a fallback, and a fallback is not the truth: the server's
        # value is the one that lands in the stored row and survives a reload.
        bus.publish({"type": "user_message", "sid": sid, "text": text,
                     "origin": origin, "ts": user_row.get("ts")})

        # The mode comes off disk, not from this socket's copy of the record.
        # A CHAT↔AGENT click lands through POST /api/sessions/{sid}/metadata →
        # store.patch, and the `rec` a socket holds was read when it opened, it
        # is minutes old, and it is the same shape of staleness that PRESERVED
        # (store.py:83) exists for. Before Stage 1b was undone, a mode click hid
        # this by opening a fresh socket, whose URL re-read the record; switching
        # in place exposes it, so the turn asks the disk which mode it is in.
        # One read, two consumers: the mode, and the plan pointer below. Neither is
        # owned by this socket — the mode by the metadata POST, the plan by plan
        # mode's driver thread — so both come off disk.
        disk = store.get(sid) or {}
        if disk.get("mode"):
            rec["mode"] = disk["mode"]
        chat = (rec.get("mode") or "agent") == "chat"
        messages = [{"role": "system",
                     "content": prompts.system_prompt(rec.get("project"), False, chat=chat,
                                                      session=rec.get("id") or "")}]
        # The project's own instructions go in the standing prefix, before the
        # transcript: they depend only on the folder, so putting them here keeps
        # the cached prefix stable across turns instead of invalidating it.
        instructions = {} if chat else project_context.block(rec.get("project"))
        if instructions.get("text"):
            messages.append({"role": "system", "content": instructions["text"]})
            metrics.bump(rec, instruction_tokens=instructions.get("tokens") or 0)
            audit.record("project_instructions", session=sid, backend="context",
                         mode=str(instructions.get("mode") or ""),
                         tokens=int(instructions.get("tokens") or 0),
                         files=[f["name"] for f in instructions.get("files") or []],
                         held_back=int(instructions.get("held_back") or 0))
        # An approved plan is a file, and a file the agent does not know the path to
        # is a file it will not read. The pointer goes in the standing prefix beside
        # the project's own instructions for the same reason they do: the transcript
        # gets summarised and the prefix is rebuilt every turn, so this outlives the
        # message that first named the path. Chat mode is skipped because it has no
        # read tool, and a pointer to a file a model cannot open is a tax with no
        # purchase.
        if not chat:
            from . import plan as plan_mod  # lazy: plan's driver imports this module
            pointer = plan_mod.pointer(disk)
            if pointer:
                messages.append({"role": "system", "content": pointer})
        messages.extend(_history(rec))

        # The Goal is NOT injected here, and nothing on this side may start to
        # inject it. It belongs to the Assistant alone: the Goal exists so the
        # Assistant holds the objective steady after compaction has rewritten
        # everything else, not so the agent can be told what to want. The agent
        # learns of the Goal only through the prompt the Assistant chose to
        # write, which arrives as an ordinary user row below, the one channel
        # between them, and the only one that is meant to exist.
        # (The leak this note replaced: assistant.goal_block() was appended to
        # the agent's user turn every turn. It is now a cost report with no
        # caller on the agent's side; metrics has no goal row, because the agent
        # never pays for one.)

        # The task list is no longer injected here. It changes while the turn runs,
        # so a snapshot taken at turn start is the one thing that makes the feature
        # useless: the model finishes items and then cannot see the list it is
        # supposed to be working through. `agent.run_turn` re-reads it on every step.
        # What is recorded now is only its cost, for the ledger and the dashboard.
        if not chat and plugin_manager.is_enabled("task_list"):
            try:
                from .plugins import task_list as _tl
                metrics.bump(rec, task_tokens=(_tl.block(sid) or {}).get("tokens") or 0)
            except Exception:  # noqa: BLE001
                pass

        block = {} if chat else _memory_block(rec.get("project"))
        text_block = (block or {}).get("text") or ""
        if text_block:
            messages.append({"role": "system", "content": text_block})
            injected = token_mod.estimate_tokens(text_block)
            metrics.bump(rec, memory_tokens=injected)
            # What the budget refused is as much a fact as what it allowed. This
            # is the number behind the dashboard's "saved by memory budgeting"
            # row, which was permanently zero because nothing ever recorded it.
            try:
                available = int(memory_store.stats(rec.get("project") or "")
                                .get("total_tokens") or 0)
            except Exception:  # noqa: BLE001
                available = 0
            held = max(0, available - injected)
            if held:
                metrics.bump(rec, saved_memory_budget=held)
            audit.record("memory_injection", session=sid, backend="memory",
                         mode=str(block.get("mode") or ""), tokens=injected,
                         count=int(block.get("count") or 0),
                         held_back=int(block.get("held_back") or 0),
                         budget=int(block.get("budget") or 0))

        if not chat:
            _account_context(rec)

        if images:
            if config.accepts_images(rec.get("model")):
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "user":
                        messages[i] = {"role": "user", "content": _content_parts(text, images)}
                        break
            else:
                bus.publish({"type": "notify", "level": "warn", "sid": sid,
                             "message": (f"{rec.get('model') or 'this model'} does not accept "
                                         "images, sent the text only")})

        # The meter moves when the request is made, not only when it is billed.
        _raise_context(rec, messages)

        # Spawn and driver-thread start happen back to back with no await
        # between them: from the moment the worker exists, its reader exists,
        # so a browser dying here cannot orphan it. This pair is the whole
        # anti-orphan guarantee on the start path.
        worker = turn_worker.spawn(sid)
        use_process = worker.start(
            project=project, ref=ref, chat=chat, session=sid,
            has_instructions=bool(instructions.get("text")),
            reasoning=reasoning, max_steps=None, messages=messages, trace=trace)
        run["worker"] = worker if use_process else None
    except Exception as exc:  # noqa: BLE001
        # The turn never got its driver: the bus, the busy mark and the run
        # row are cleaned here, because their real owner does not exist yet.
        turn_bus.finish(sid)
        session_state.end(sid)
        with _lock:
            _turns.pop(sid, None)
        return {"ok": False, "error": str(exc)}

    t = threading.Thread(
        target=_drive_turn,
        args=(sid, rec, messages),
        kwargs={"project": project, "ref": ref, "chat": chat,
                "has_instructions": bool(instructions.get("text")),
                "reasoning": reasoning, "trace": trace, "run": run, "bus": bus},
        daemon=True)
    run["thread"] = t
    t.start()
    return {"ok": True, "sid": sid}


def start_assistant(sid: str, rec: dict, text: str,
                    loop: asyncio.AbstractEventLoop | None = None,
                    turns: int | None = None) -> dict:
    """Start the session's assistant reply, the session's second worker.

    Independent of the agent turn by design: both may run at once. The user's
    prompt is persisted before the worker exists, so a worker that never
    starts still leaves the prompt in the conversation.
    """
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "empty prompt"}
    if loop is None:
        loop = asyncio.get_running_loop()
    with _lock:
        if sid in _assistants:
            return {"ok": False, "error": "the assistant is already replying"}
        run = {"stop": threading.Event(), "worker": None, "thread": None, "loop": loop}
        _assistants[sid] = run
    bus = turn_bus.start(sid, loop, channel="assistant")
    trace: list[dict] = []
    try:
        if not rec.get("model"):
            rec["model"] = config.registry().get("default") or ""
        # Settings come from the disk copy, not this socket's long-lived one:
        # the panel may have changed them minutes ago through REST, and a stale
        # in-memory copy would silently answer with the old turn window.
        disk = store.get(sid) or {}
        if disk.get("assistantSettings") is not None:
            rec["assistantSettings"] = disk["assistantSettings"]
        cfg = assistant.settings_of(rec)
        # The worker's view of the conversation is the history as it stands,
        # before this prompt: run_turn appends the prompt itself, so handing
        # it the post-append history would read the question twice.
        mini = dict(rec)
        mini["assistant"] = [dict(m) for m in (rec.get("assistant") or [])]
        if turns:
            # A cycle prompt fires after every turn, so it reads a narrower
            # window than a question somebody typed. Per-call, not a setting:
            # the stored value is untouched, so the panel still shows and
            # controls what the person chose.
            cfg = dict(cfg)
            cfg["turns"] = max(1, min(200, int(turns)))
            mini["assistantSettings"] = cfg
        assistant.append(rec, "user", text)
        store.save(rec)
        worker = turn_worker.spawn(sid)
        use_process = worker.start_assistant(session=sid, state=mini, trace=trace)
        run["worker"] = worker if use_process else None
    except Exception as exc:  # noqa: BLE001
        turn_bus.finish(sid, channel="assistant")
        with _lock:
            _assistants.pop(sid, None)
        return {"ok": False, "error": str(exc)}
    t = threading.Thread(
        target=_drive_assistant,
        args=(sid, rec, mini, text, cfg, trace, run, bus),
        daemon=True)
    run["thread"] = t
    t.start()
    return {"ok": True, "sid": sid}


# ── control: stop and steer, scoped to one sid by construction ─────────────
def cancel(sid: str) -> dict:
    """Stop everything the session is running: its turn, its assistant, its
    background jobs.

    Scoped to sid by construction, the registries are keyed by sid and there
    is no code path here that can touch another session. May block up to the
    cancel grace period waiting for a cooperative exit, so callers off the
    event loop (the socket handler runs it in an executor).
    """
    out: dict = {"turn": None, "assistant": None, "jobs": 0}
    with _lock:
        run = _turns.get(sid)
        arun = _assistants.get(sid)
    if run is not None:
        run["stop"].set()
        run["steer"].clear()
        w = run.get("worker")
        if w is not None and w.alive():
            # Two-stage: cooperative cancel first, tree kill after the grace.
            out["turn"] = w.cancel()
        else:
            out["turn"] = {"ok": True, "how": "no-worker"}
    if arun is not None:
        arun["stop"].set()
        w = arun.get("worker")
        if w is not None and w.alive():
            out["assistant"] = w.cancel()
    stopped_jobs = extras.stop_session_jobs(sid)
    out["jobs"] = stopped_jobs
    # Instant off. The Stop button is the person saying "no more of this", and a
    # mode that carried on into its next cycle after being stopped had not
    # understood the instruction. Done before the audit rows and before this
    # returns, so the state a reload reads back is already the stopped state,
    # and every chaining decision re-reads it from disk, so a turn still
    # winding down cannot start a cycle on the strength of a stale "enabled".
    out["autonomy"] = _autonomy_off(sid, "stopped")
    if out["turn"] is not None:
        audit.record("turn_cancel", session=sid, backend="turn_worker",
                     status="ok", **(out["turn"] or {}))
    if stopped_jobs:
        audit.record("turn_jobs_stopped", session=sid, backend="bg",
                     status="ok", count=stopped_jobs)
    return out


def steer(sid: str, text: str) -> bool:
    """Carry a mid-turn steer to the session's running turn.

    In process mode it rides the worker's stdin; in thread mode it lands on
    the run's steer list, which ``run_turn`` reads between events. False when
    the session has no running turn.
    """
    with _lock:
        run = _turns.get(sid)
    if run is None:
        return False
    w = run.get("worker")
    if w is not None and w.steer(text):
        return True
    run["steer"].append(str(text or ""))
    return True


def shutdown() -> dict:
    """Lifespan shutdown: stop every worker this server spawned.

    A restart that lands the partial transcripts instead of orphaning the
    children. Each worker gets the two-stage stop; its driver's epilogue,
    the transcript save, is given a moment to run before the process exits.
    """
    with _lock:
        sids = sorted(set(_turns) | set(_assistants))
        threads = [run.get("thread") for run in
                   list(_turns.values()) + list(_assistants.values())]
    for sid in sids:
        try:
            cancel(sid)
        except Exception:  # noqa: BLE001
            pass
    for t in threads:
        if t is not None and t.is_alive():
            try:
                t.join(timeout=3.0)
            except Exception:  # noqa: BLE001
                pass
    return {"stopped": sids}
"""The WebSocket protocol: a viewer and a control channel, nothing more.

Every session's work (its agent turn, its assistant reply) is owned by
``backend/session_runtime.py``, keyed by session id. This handler owns nothing:
it subscribes to the session's streams when it opens, pumps them to the
browser, and forwards the few commands a browser may send. A socket that dies
takes only its subscription with it; the turn keeps running, the stream keeps
buffering, and the transcript is saved by the turn's own epilogue. The same
session opened on two devices sees the same stream, because both are just
subscribers of the same buffer.

Blocking commands, a turn's start (compaction can call a model), an abort's
grace wait, a terminal command, a forced compaction, run in the executor,
never on the event loop. One slow summariser or shell command must not freeze
every other session's stream, which is exactly what the old socket-owned path
did.

Plan mode is owned the same way, by ``backend/plan_runtime.py``. It used to be
the exception, its state lived in a dict inside this handler on the argument
that it is interactive, asking questions and waiting for the answers, but the
answers are the *least* fragile part of it: they arrive minutes later, from a
socket that may be a different one, after a reload or a laptop sleep. Waiting
for a person is precisely the thing that must not be owned by a connection.
"""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import (agent, assistant, config, extras, plan_runtime, session_runtime,
                session_state, store, turn_bus)
from ..session_runtime import _title

router = APIRouter()

# Bounds for transcript fields a browser may push back. Stored tool results are
# what the agent reads as its own memory next turn, so they are kept, but clipped
# to the same limit a live tool result already carries.
MAX_REASON = 8000
MAX_TOOLS_PER_STEP = 20
MAX_TOOL_RESULT = 6000


def _json(msg: dict) -> str:
    return json.dumps(msg, ensure_ascii=False)


def _usage_snapshot(rec: dict) -> dict:
    u = rec.get("usage") or {}
    toks = u.get("tokens") or {}
    ctx = dict(u.get("context") or {})
    window = ctx.get("window") or 0
    if not window:
        try:
            window = (config.resolve_model(rec.get("model")) or {}).get("contextWindow") or 0
        except Exception:
            window = 0
    if window and not ctx.get("window"):
        ctx["window"] = window
        ctx["reserve"] = max(0, window, config.context_fill_target(window))
    return {
        "tokens": {"input": toks.get("input") or 0,
                   "output": toks.get("output") or 0,
                   "total": toks.get("total") or 0,
                   "cacheRead": toks.get("cacheRead") or 0,
                   "cacheWrite": toks.get("cacheWrite") or 0},
        "context": {"tokens": ctx.get("tokens"), "window": ctx.get("window") or 0,
                    "percent": ctx.get("percent"), "reserve": ctx.get("reserve") or 0},
        "compacting": bool(u.get("compacting")),
        "speed": u.get("speed") or 0,
    }


def _meta(rec: dict) -> dict:
    # Live state comes from the registry keyed by sid, never from a socket's
    # local flag: two windows can hold the same session open, and the truth is
    # whether a worker for that sid is running in this process.
    live = session_state.get(rec["id"])
    return {
        "sid": rec["id"], "model": rec.get("model") or "",
        "mode": rec.get("mode") or "agent", "thinking": rec.get("thinking") or "default",
        "workdir": rec.get("project") or "", "name": rec.get("title") or "New session",
        "allowed_tools": "", "excluded_tools": "", "permission_mode": "accept-all",
        "busy": bool(live.get("busy")), "starting": bool(live.get("starting")),
        "assistantBusy": session_runtime.assistant_running(rec["id"]),
        # Where the session's plan got to, so a window that just opened can
        # redraw the strip instead of starting a second plan to find out. Read
        # from the runtime rather than from this handler's copy of ``rec``, which
        # is as old as the connection and would show a plan already superseded.
        # The record is passed, not just the id: `snapshot` falls back to
        # `store.get(sid)` when it has nothing in hand, and `_meta` is called on
        # every hello and every get_state. Reading the disk here re-reads a record
        # the caller is already holding, and for a caller that synthesized one
        # (tests, and any future internal use) it reaches whatever store the
        # process currently points at — which is how a contract test that never
        # redirects the store ended up opening a real session file.
        "plan": plan_runtime.snapshot(rec["id"], rec),
        "usage": _usage_snapshot(rec),
    }


def _stats_payload(rec: dict) -> dict:
    """The shape the client's stats toast/meter reads: totalMessages, toolCalls,
    tokens, contextUsage{percent, contextWindow, tokens}."""
    msgs = rec.get("messages") or []
    chars = sum(len(m.get("content") or "") for m in msgs)
    usage = _usage_snapshot(rec)
    ctx = usage.get("context") or {}
    return {
        "messages": len(msgs),
        "totalMessages": len(msgs),
        "chars": chars,
        "toolCalls": int(rec.get("tool_calls") or 0),
        "tokens": usage.get("tokens") or {},
        "contextUsage": {"tokens": ctx.get("tokens"),
                         "contextWindow": ctx.get("window") or 0,
                         "percent": ctx.get("percent")},
        "model": rec.get("model"),
    }


def _adoptable(prior: list, incoming: list) -> bool:
    """May a client-supplied transcript replace the stored one?

    `truncate_from` treats the browser's visible list as authoritative, and that
    is deliberate: it is how an offline edit, or a server that restarted before
    the first persist, gets rescued. But /api/sessions/registry now ships each
    session as a PREVIEW TAIL (see `REGISTRY_PREVIEW` in backend/store.py), and a
    tail is the END of a conversation. Adopting one would replace 300 stored rows
    with their last 24 — and the rewind index below is counted from the same
    fragment, so "edit this old turn" would then cut an unrelated message and
    delete real history while looking like it worked.

    A legitimate client list starts where the record starts. So: adopt when
    nothing is stored, when the client holds at least as much history as we do,
    or when both lists open with the same message.
    """
    if not prior or not incoming:
        return True
    if len(incoming) >= len(prior):
        return True
    a, b = prior[0], incoming[0]
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if a.get("role") != b.get("role"):
        return False
    return (a.get("content") or "")[:200] == (b.get("content") or "")[:200]


def _clean_history(items) -> list[dict]:
    """Normalise a client-supplied transcript to the stored message shape."""
    out = []
    for m in items or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant", "summary"):
            continue
        content = m.get("content")
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        row = {"role": role, "content": content}
        ts = m.get("ts")
        if isinstance(ts, (int, float)):
            row["ts"] = ts
        # The display-only sender tag survives an adopted transcript, but only
        # from the known set: a client cannot stamp its own words as the
        # Assistant's, and "you" is the absence of the field, never a value a
        # browser gets to assert.
        origin = m.get("origin")
        if isinstance(origin, str) and origin in ("assistant", "autonomous"):
            row["origin"] = origin
        # reason and tools are kept, because dropping them erased the history that
        # lets the agent remember its own work. A client may push anything here, so
        # they are reduced to known fields and clipped rather than passed through.
        reason = m.get("reason")
        if isinstance(reason, str) and reason.strip():
            row["reason"] = reason[:MAX_REASON]
        tools = m.get("tools")
        if isinstance(tools, list):
            kept = []
            for t in tools[:MAX_TOOLS_PER_STEP]:
                if not isinstance(t, dict) or not t.get("id"):
                    continue          # an unpaired call would break the transcript
                args = t.get("args")
                kept.append({
                    "id": str(t["id"])[:64],
                    "name": str(t.get("name") or "")[:64],
                    "args": args if isinstance(args, dict) else {},
                    "result": str(t.get("result") or "")[:MAX_TOOL_RESULT],
                    "is_error": bool(t.get("is_error")),
                })
            if kept:
                row["tools"] = kept
        out.append(row)
    return out


@router.websocket("/ws/{sid}")
async def ws_session(ws: WebSocket, sid: str):
    await ws.accept()
    loop = asyncio.get_running_loop()
    q = ws.query_params
    rec = store.get(sid)
    if rec is None:
        # A socket may not create a session. Only the interface's own New
        # session action asks for one, and it says so with ?create=1. Anything
        # else is a stale id, and it is refused rather than silently honoured
        # (honouring it grew the session list on every page load).
        if q.get("create") != "1":
            await ws.close(code=4404, reason="unknown session")
            return
        rec = store.create(title=q.get("name") or "New session",
                           model=q.get("model") or "",
                           mode=q.get("mode") or "agent",
                           thinking=q.get("thinking") or "default",
                           project=q.get("workdir") or "", sid=sid)
        if rec is None:
            # The id was deleted. `create` refuses it rather than re-materialising
            # a session the person threw away, which is the one way a delete can
            # still be undone from the browser side: a tab that kept the id in its
            # own copy of the list asks for it back after a 4404, and until now the
            # store obliged. Say the same thing again and let the view drop the row.
            await ws.close(code=4404, reason="session deleted")
            return
        sid = rec["id"]
    else:
        changed = False
        for key in ("model", "mode", "thinking"):
            if q.get(key) is not None and q.get(key) != rec.get(key):
                rec[key] = q.get(key)
                changed = True
        # workdir query param maps to the stored 'project' field
        if q.get("workdir") is not None and q.get("workdir") != rec.get("project"):
            rec["project"] = q.get("workdir")
            changed = True
        if changed:
            store.save(rec)

    # The socket joins the session's streams before it says anything: the
    # subscription is live from this moment, and the replay below covers
    # everything published before it, no gap, no duplication. The
    # subscription outlives turns, because the stream is the session's, not
    # the turn's: the next turn's events arrive on the same queue with
    # nothing to re-open.
    turn_replay, turn_q = turn_bus.subscribe(sid, loop)
    _as_replay, as_q = turn_bus.subscribe(sid, loop, channel="assistant")
    plan_replay, plan_q = turn_bus.subscribe(sid, loop, channel="plan")

    await ws.send_text(_json({"type": "hello", **_meta(rec)}))
    # A hello that lands mid-turn is followed by the turn's buffered events so
    # far. The viewer draws the past from the replay instead of waiting on
    # events that already streamed past; an empty buffer means the transcript
    # in the session record is the whole story.
    if turn_replay:
        await ws.send_text(_json({"type": "replay", "sid": rec["id"],
                                  "events": turn_replay}))
    # The plan's replay is forwarded as the events themselves, not wrapped: a
    # viewer draws the strip from plan_status / plan_questions / plan_ready, and
    # replaying them in order lands on whichever of those it was on. The hello
    # above already carried the authoritative snapshot, so a viewer that
    # reconnects long after the buffer emptied still gets its state, the replay
    # is only what the snapshot cannot show, which is the explorers' live detail.
    for ev in plan_replay:
        await ws.send_text(_json(ev))
    await ws.send_text(_json({"type": "session_ready", "sid": rec["id"],
                              "model": rec.get("model"), "mode": rec.get("mode"),
                              "thinking": rec.get("thinking"), "name": rec.get("title")}))

    async def pump(queue: asyncio.Queue, channel: str):
        """One channel's live events to this socket, for the life of the socket.

        The stream does not end when a turn does, the next turn's events
        arrive on the same queue — so the only exits are the socket dying or
        the handler closing. Either way this viewer's subscription is dropped;
        the stream itself is untouched.
        """
        try:
            while True:
                ev = await queue.get()
                if ev is None:          # defensive; finish() keeps subscribers
                    break
                await ws.send_text(_json(ev))
        except Exception:
            pass
        finally:
            turn_bus.unsubscribe(sid, queue, channel)

    pumps = [asyncio.create_task(pump(turn_q, "turn")),
             asyncio.create_task(pump(as_q, "assistant")),
             asyncio.create_task(pump(plan_q, "plan"))]

    async def start_plan(text: str):
        """Write the request into the conversation, then hand the planning over.

        The transcript and the title stay here because this socket owns the
        conversation those belong to: a plan request is a user message first and
        a plan second. Everything after that — the thread, the events, whether a
        second plan may start, is the session's business, so it goes to
        ``plan_runtime``, which returns as soon as the run exists rather than
        driving it to the end. This handler does not await a plan.

        The guard is checked before the message is appended, so a request that
        cannot be planned is not left in the transcript as though it were.
        """
        if not (text or "").strip():
            return
        if session_state.busy(rec["id"]) or plan_runtime.running(rec["id"]):
            await ws.send_text(_json({"type": "notify", "level": "warn",
                                      "message": "a turn or plan is already running"}))
            return
        if not rec.get("model"):
            rec["model"] = config.registry().get("default") or ""
        res = plan_runtime.start(rec["id"], rec, text, loop)
        if not res.get("ok"):
            await ws.send_text(_json({"type": "notify", "level": "warn",
                                      "message": res.get("error") or "could not start a plan"}))
            return
        store.append(rec, "user", text)
        if (rec.get("title") or "New session") == "New session":
            rec["title"] = _title(text)
        store.save(rec)
        await ws.send_text(_json({"type": "state_delta", "name": rec["title"]}))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")

            if kind == "prompt":
                if msg.get("plan"):
                    asyncio.create_task(start_plan(msg.get("message") or ""))
                else:
                    # The start runs in the executor: compaction may call a
                    # model, and the spawn takes real time. This socket's
                    # message loop waits, its pumps do not, and no other
                    # session's stream waits with it.
                    res = await loop.run_in_executor(
                        None, session_runtime.start_turn,
                        rec["id"], rec, msg.get("message") or "",
                        msg.get("images"), loop)
                    if not res.get("ok"):
                        await ws.send_text(_json({
                            "type": "notify", "level": "warn", "sid": rec["id"],
                            "message": res.get("error") or "could not start the turn"}))

            elif kind == "steer":
                text = msg.get("message") or ""
                if text.strip():
                    # The runtime finds the turn's worker by sid, so a steer
                    # arriving on any socket of this session reaches the turn.
                    carried = session_runtime.steer(rec["id"], text)
                    await ws.send_text(_json({"type": "notify", "level": "info",
                                              "message": "steering applied" if carried
                                              else "nothing is running to steer",
                                              "sid": rec["id"]}))

            elif kind == "abort":
                # Scoped to this socket's session by construction: the
                # runtime's registries are keyed by sid. The cancel may wait
                # out the cooperative grace period, so it runs off the event
                # loop; the stop is not awaited, exactly as before.
                loop.run_in_executor(None, session_runtime.cancel, rec["id"])

            elif kind == "assistant_prompt":
                res = await loop.run_in_executor(
                    None, session_runtime.start_assistant,
                    rec["id"], rec, msg.get("message") or "", loop)
                if not res.get("ok"):
                    await ws.send_text(_json({
                        "type": "notify", "level": "warn", "sid": rec["id"],
                        "message": res.get("error") or "the assistant could not start"}))

            elif kind == "assistant_clear":
                assistant.clear(rec)
                store.save(rec)
                await ws.send_text(_json({"type": "assistant_cleared", "sid": rec["id"],
                                          "preview": assistant.preview(rec)}))

            elif kind == "assistant_settings":
                cfg = assistant.save_settings(rec, msg.get("patch") or {})
                store.patch(rec["id"], assistantSettings=cfg)
                await ws.send_text(_json({"type": "assistant_settings", "settings": cfg,
                                          "preview": assistant.preview(rec), "sid": rec["id"]}))

            elif kind == "assistant_goal":
                goal = session_runtime.goal_set(rec["id"], msg.get("goal") or "")
                # Echo the whole mode state, not just the Goal: the panel dims
                # the switch while there is nothing to run toward, and it must
                # learn that from the write that caused it.
                await ws.send_text(_json({"type": "assistant_goal", "sid": rec["id"],
                                          "goal": goal,
                                          "autonomy": session_runtime.autonomy_state(rec["id"])}))

            elif kind == "assistant_autonomy":
                # Executor: this reads and writes the record, and enabling can
                # be the start of a long run. autonomy_set is keyword-only, and
                # run_in_executor takes positionals, so the call closes over the
                # body rather than trying to thread keywords through it.
                enabled = msg.get("enabled")
                max_cycles = msg.get("max_cycles")
                max_hours = msg.get("max_hours")
                state = await loop.run_in_executor(
                    None, lambda: session_runtime.autonomy_set(
                        rec["id"], enabled=enabled,
                        max_cycles=max_cycles, max_hours=max_hours))
                await ws.send_text(_json({"type": "assistant_autonomy", "sid": rec["id"],
                                          "autonomy": state}))

            elif kind == "assistant_reset":
                state = session_runtime.autonomy_reset(rec["id"])
                await ws.send_text(_json({"type": "assistant_autonomy", "sid": rec["id"],
                                          "autonomy": state}))

            elif kind == "assistant_delete":
                if assistant.delete_message(rec, msg.get("index")):
                    store.save(rec)
                await ws.send_text(_json({
                    "type": "assistant_state", "sid": rec["id"],
                    "messages": rec.get("assistant") or [],
                    "settings": assistant.settings_of(rec),
                    "preview": assistant.preview(rec)}))

            elif kind == "assistant_send":
                # The Send button on an offered prompt: straight to the agent.
                # Steering is the instant path when it is mid-turn; otherwise
                # this is a normal turn start, and the transcript shows it as
                # one, an autonomous prompt and a typed one must not be two
                # different kinds of thing.
                # An index means "the prompt inside that stored reply". The
                # extraction stays here in Python, next to extract_prompt, so a
                # button in the browser cannot send the prose around the block
                # and have the agent read the essay as the task.
                text = (msg.get("message") or "").strip()
                index = msg.get("index")
                if isinstance(index, int):
                    stored = (rec.get("assistant") or [])
                    if 0 <= index < len(stored):
                        text = assistant.extract_prompt(
                            stored[index].get("content") or "").strip()
                if text:
                    if session_runtime.turn_running(rec["id"]):
                        carried = session_runtime.steer(rec["id"], text)
                        await ws.send_text(_json({
                            "type": "notify", "level": "info", "sid": rec["id"],
                            "message": "sent to the agent" if carried
                                       else "the agent could not take it mid-turn"}))
                    else:
                        out = await loop.run_in_executor(
                            None, session_runtime.start_turn,
                            rec["id"], rec, text, None, loop, "assistant")
                        # start_turn answers {"ok": False, "error": ...} instead
                        # of raising. Swallowing it meant a prompt that never
                        # reached the agent left no trace anywhere, the pane
                        # showed the message, the transcript showed nothing, and
                        # the person had no way to know the agent was idle.
                        if not (out or {}).get("ok"):
                            await ws.send_text(_json({
                                "type": "notify", "level": "error", "sid": rec["id"],
                                "message": ("could not send to the agent: "
                                            + (out or {}).get("error", "no reason given"))}))

            elif kind == "switch":
                # Deleted in stage 1b. A socket is born attached to one
                # session and stays attached; switching is the client opening
                # a different socket. The command is kept only to answer old
                # clients with the reason, instead of the generic
                # "not supported", and it must never rebind this
                # connection's session record, which is how a turn started in
                # session A came to be steered, stopped and saved into
                # session B.
                await ws.send_text(_json({
                    "type": "error", "sid": rec["id"],
                    "message": ("switch is no longer supported: every session has its "
                                "own socket now, reload the page to pick up the new "
                                "interface")}))

            elif kind == "set_model":
                rec["model"] = msg.get("model") or rec.get("model")
                store.save(rec)
                await ws.send_text(_json({"type": "state_delta", "model": rec["model"],
                                          "rpc": "set_model"}))

            elif kind == "set_thinking_level":
                rec["thinking"] = msg.get("level") or rec.get("thinking")
                store.save(rec)
                await ws.send_text(_json({"type": "state_delta", "thinking": rec["thinking"]}))

            elif kind == "set_session_name":
                rec["title"] = (msg.get("name") or "").strip() or rec.get("title")
                store.save(rec)
                await ws.send_text(_json({"type": "state_delta", "name": rec["title"]}))

            elif kind == "set_session_tool_set":
                pass

            elif kind in ("new_session", "client_new_session"):
                # Stage 1b, per the deleted plan's decision: no server-side rebinding,
                # ever. A socket is born attached to one session and stays
                # attached. Creating a session hands the new id back; the
                # client closes nothing and opens a fresh socket for it.
                fresh = store.create(model=rec.get("model") or "", mode=rec.get("mode") or "agent",
                                     thinking=rec.get("thinking") or "default",
                                     project=rec.get("project") or "")
                await ws.send_text(_json({
                    "type": "rpc_response", "command": kind, "ok": True,
                    "data": {"sid": fresh["id"], "name": fresh["title"],
                             "model": fresh.get("model") or "", "mode": fresh.get("mode") or "agent",
                             "thinking": fresh.get("thinking") or "default",
                             "workdir": fresh.get("project") or ""},
                    "sid": rec["id"]}))

            elif kind == "get_state":
                await ws.send_text(_json({"type": "hello", **_meta(rec)}))

            elif kind == "get_session_stats":
                await ws.send_text(_json({
                    "type": "rpc_response", "command": "get_session_stats", "ok": True,
                    "data": _stats_payload(rec), "sid": rec["id"]}))

            elif kind == "truncate_from":
                # The browser owns the visible transcript: it sends the full
                # message list plus the 0-based index of the user turn being
                # edited. Adopt that list, rewind to just before that turn, and
                # echo userTurnIndex so the client can match the acknowledgement.
                incoming = msg.get("messages")
                if isinstance(incoming, list) and incoming:
                    if not _adoptable(rec.get("messages") or [], incoming):
                        # A fragment that does not start this conversation — a
                        # preview tail left in an old tab. Its `userTurnIndex` was
                        # counted from the same fragment, so the cut below would
                        # rewind to the wrong turn and delete rows the user never
                        # named. Refuse the whole request, leave the record
                        # untouched, and let the client reopen and try again.
                        await ws.send_text(_json({
                            "type": "truncate_failed", "sid": rec["id"],
                            "error": "this browser holds only a fragment of the session - reopen it, then edit"}))
                        continue
                    rec["messages"] = _clean_history(incoming)
                msgs = rec.get("messages") or []
                turn = msg.get("userTurnIndex")
                idx = msg.get("index")
                cut = None
                if isinstance(turn, int) and turn >= 0:
                    seen = -1
                    for i, m in enumerate(msgs):
                        if m.get("role") == "user":
                            seen += 1
                            if seen == turn:
                                cut = i
                                break
                if cut is None and isinstance(idx, int) and 0 <= idx <= len(msgs):
                    cut = idx
                if cut is None:
                    await ws.send_text(_json({"type": "truncate_failed", "sid": rec["id"],
                                              "error": "could not find that user turn to rewind to"}))
                else:
                    rec["messages"] = msgs[:cut]
                    store.save(rec)
                    # Rows from `cut` onward no longer exist, so any snapshot that
                    # named one must stop naming it before the client re-renders:
                    # `truncated` makes app.js redraw and re-fetch /api/snapshots,
                    # and a stale index would then decorate a surviving, unrelated
                    # row. The undo points themselves are untouched.
                    extras.drop_message_links(rec["id"], cut)
                    await ws.send_text(_json({"type": "truncated", "index": cut,
                                              "userTurnIndex": turn if isinstance(turn, int) else cut,
                                              "sid": rec["id"]}))

            elif kind == "bash":
                # A terminal command can run for minutes; it must not hold the
                # event loop, every other session's stream would freeze with it.
                res = await loop.run_in_executor(
                    None, agent.run_terminal, msg.get("command") or "",
                    rec.get("project") or None)
                await ws.send_text(_json({"type": "rpc_response", "command": "bash",
                                          "ok": bool(res.get("ok")), "data": res,
                                          "sid": rec["id"]}))

            elif kind in ("fork", "clone_session"):
                suffix = " (fork)" if kind == "fork" else " (copy)"
                copy = store.duplicate(rec["id"], suffix, msg.get("at"))
                if not copy:
                    await ws.send_text(_json({"type": "rpc_response", "command": kind,
                                              "ok": False, "error": "session not found",
                                              "sid": rec["id"]}))
                else:
                    await ws.send_text(_json({
                        "type": "rpc_response", "command": kind, "ok": True,
                        "data": {"sid": copy["id"], "name": copy["title"]},
                        "sid": rec["id"]}))

            elif kind == "plan_answers":
                # Handing back the answers starts the synthesiser in the
                # runtime's thread. This handler does not wait for it, so a
                # socket closed while the model is writing the plan loses a
                # viewer and nothing else, which is the case that used to
                # discard a plan the person had already paid for.
                res = plan_runtime.answers(rec["id"], rec, msg.get("answers") or "", loop)
                if not res.get("ok"):
                    await ws.send_text(_json({"type": "rpc_response", "command": "plan_answers",
                                              "ok": False, "error": res.get("error"),
                                              "sid": rec["id"]}))

            elif kind == "plan_approve":
                # Approving is where a plan stops being conversation and becomes
                # a file: the approved text is saved under ~/.tacit/plans, which
                # is now the only copy — the project is never written into. What
                # reaches the implementing agent is a pointer to that path in its
                # standing prefix, so the plan survives compaction without being
                # pasted back into context. The result is forwarded unchanged so
                # the strip can name the file it wrote.
                res = plan_runtime.approve(rec["id"], rec, bool(msg.get("approved")))
                store.save(rec)
                await ws.send_text(_json({"type": "plan_approved", **res, "sid": rec["id"]}))

            elif kind == "plan_abort":
                # The runtime decides what "abort" means here: a generator still
                # exploring gets its flag and unwinds itself, a plan merely
                # waiting for answers is just removed. Either way the cancelled
                # event arrives through the plan pump, not from this handler.
                res = plan_runtime.abort(rec["id"])
                if not res.get("ok"):
                    await ws.send_text(_json({"type": "notify", "level": "warn",
                                              "message": res.get("error"),
                                              "sid": rec["id"]}))

            elif kind == "plan_implement":
                # Stage 1b, per the deleted plan's decision: no server-side rebinding.
                # The implementation runs in a fresh session (the approval
                # notice promises a clean context, leaving the planning
                # discussion behind), and the client opens a socket for it,
                # this connection stays attached to the planning session.
                res = plan_runtime.implement(rec["id"], rec)
                if res.get("ok"):
                    await ws.send_text(_json({
                        "type": "rpc_response", "command": "plan_implement", "ok": True,
                        "data": res["data"], "sid": rec["id"]}))
                else:
                    await ws.send_text(_json({"type": "notify", "level": "warn",
                                              "message": res.get("error"),
                                              "sid": rec["id"]}))

            elif kind == "compact":
                # A forced compaction may call the summariser model; it runs
                # in the executor like every other blocking command. The
                # accounting is the runtime's, not this handler's: the command
                # used to fold the record and tell nobody, so the bar went on
                # reading 99% over a transcript that had just been compacted and
                # the dashboard's saved-by-compaction row stayed at zero.
                #
                # It also claims the session. A hand compaction rewrites the
                # transcript and can take seconds; a turn started in another
                # window in that window would race the fold and write back over
                # it, so the busy mark is what makes "wait for it" true rather
                # than merely polite.
                if session_state.busy(rec["id"]):
                    await ws.send_text(_json({
                        "type": "notify", "level": "warn", "sid": rec["id"],
                        "message": "this session is busy, nothing to compact while it runs"}))
                    continue
                session_state.begin(rec["id"], "compact")

                def _tell(on: bool) -> None:
                    ev = {"type": "compacting", "on": on, "sid": rec["id"]}
                    turn_bus.publish(rec["id"], ev)
                    try:
                        asyncio.run_coroutine_threadsafe(
                            ws.send_text(_json(ev)), loop)
                    except Exception:
                        pass

                try:
                    info = await loop.run_in_executor(
                        None, lambda: session_runtime.compact_now(
                            rec, force=True,
                            notice=lambda on: _tell(bool(on))))
                    store.save(rec)
                    await ws.send_text(_json({
                        "type": "rpc_response", "command": "compact",
                        "ok": bool(info and info.get("compacted")), "data": info, "sid": rec["id"]}))
                finally:
                    _tell(False)
                    session_state.end(rec["id"])

            else:
                await ws.send_text(_json({
                    "type": "rpc_response", "command": kind, "ok": False,
                    "error": f"{kind} is not supported", "sid": rec["id"]}))

    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass
    finally:
        # The viewer leaves; the stream stays. Cancelling the pumps drops
        # this socket's subscriptions (each pump unsubscribes in its own
        # finally), the turn, the buffer and the transcript are untouched.
        for t in pumps:
            t.cancel()
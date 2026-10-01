import asyncio
import json
import threading

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import agent, assistant, config, mcp_registry, memory_modes, metrics, plan, plugin_manager, store
from .. import tokens as token_mod
from ..ai import prompts

router = APIRouter()


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
        ctx["reserve"] = int(window * (1 - config.COMPACT_AT))
    return {
        "tokens": {"input": toks.get("input") or 0,
                   "output": toks.get("output") or 0,
                   "total": toks.get("total") or 0},
        "context": {"tokens": ctx.get("tokens"), "window": ctx.get("window") or 0,
                    "percent": ctx.get("percent"), "reserve": ctx.get("reserve") or 0},
        "compacting": bool(u.get("compacting")),
        "speed": u.get("speed") or 0,
    }


def _meta(rec: dict, running: dict | None = None) -> dict:
    busy = bool(running and running.get("busy"))
    starting = bool(running and running.get("starting"))
    return {
        "sid": rec["id"], "model": rec.get("model") or "",
        "mode": rec.get("mode") or "agent", "thinking": rec.get("thinking") or "medium",
        "workdir": rec.get("project") or "", "name": rec.get("title") or "New session",
        "allowed_tools": "", "excluded_tools": "", "permission_mode": "accept-all",
        "busy": busy, "starting": starting,
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


def _usage_event(rec: dict, usage: dict) -> dict:
    u = rec.setdefault("usage", {})
    tok = u.setdefault("tokens", {"input": 0, "output": 0, "total": 0})
    in_tok = int(usage.get("input") or 0)
    out_tok = int(usage.get("output") or 0)
    tok["input"] += in_tok
    tok["output"] += out_tok
    tok["total"] += int(usage.get("total") or 0) or (in_tok + out_tok)

    window = 0
    try:
        window = (config.resolve_model(rec.get("model")) or {}).get("contextWindow") or 0
    except Exception:
        window = 0
    ctx_tokens = (in_tok + out_tok) or None
    percent = round(ctx_tokens / window * 100, 1) if (window and ctx_tokens) else None
    ctx = {"tokens": ctx_tokens, "window": window, "percent": percent,
           "reserve": int(window * (1 - config.COMPACT_AT)) if window else 0}
    u["context"] = ctx
    u["compacting"] = False
    u["speed"] = 0
    metrics.bump(rec, prompt_tokens=in_tok, completion_tokens=out_tok)
    return {"type": "usage", "tokens": tok, "context": ctx, "compacting": False, "speed": 0}


def _history(rec: dict) -> list[dict]:
    out = []
    for m in rec.get("messages") or []:
        role = m.get("role")
        body = m.get("content") or ""
        if not body.strip():
            continue
        if role == "summary":
            out.append({"role": "system", "content": f"{prompts.SUMMARISED}\n{body}"})
        elif role in ("user", "assistant"):
            out.append({"role": role, "content": body})
    return out


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
        out.append(row)
    return out


def _content_parts(text: str, images) -> list:
    parts = [{"type": "text", "text": text or ""}]
    for im in images or []:
        mime = (im or {}).get("mimeType") or "image/png"
        data = (im or {}).get("data") or ""
        if data:
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{data}"}})
    return parts


def _memory_block(project: str | None) -> str:
    """The budgeted memory block, or '' when memory is off.

    The mode decides what may be injected, and every item is explained by the
    module that produced it. Memory never merges into the base prompt.
    """
    try:
        return memory_modes.startup(project or "").get("text") or ""
    except Exception:  # noqa: BLE001
        return ""


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
                           thinking=q.get("thinking") or "medium",
                           project=q.get("workdir") or "", sid=sid)
        sid = rec["id"]
    else:
        changed = False
        for key in ("model", "mode", "thinking"):
            if q.get(key) and q.get(key) != rec.get(key):
                rec[key] = q.get(key)
                changed = True
        if changed:
            store.save(rec)
    running = {"thread": None, "stop": threading.Event(), "steer": [], "busy": False,
               "starting": False, "assistant": False, "assistant_stop": threading.Event()}
    plan_state = {"run": None}

    await ws.send_text(_json({"type": "hello", **_meta(rec, running)}))
    await ws.send_text(_json({"type": "session_ready", "sid": rec["id"],
                              "model": rec.get("model"), "mode": rec.get("mode"),
                              "thinking": rec.get("thinking"), "name": rec.get("title")}))

    async def pump(queue: asyncio.Queue):
        while True:
            ev = await queue.get()
            if ev is None:
                break
            await ws.send_text(_json(ev))

    async def _drive(gen, sid: str):
        queue: asyncio.Queue = asyncio.Queue()

        def worker():
            try:
                for ev in gen:
                    running["starting"] = False
                    loop.call_soon_threadsafe(queue.put_nowait, {**ev, "sid": sid})
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait,
                    {"type": "plan_status", "phase": "failed", "detail": str(e),
                     "explorers": [], "sid": sid})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=worker, daemon=True).start()
        await pump(queue)

    async def start_plan(text: str):
        if not (text or "").strip() or running["busy"]:
            return
        if not rec.get("model"):
            rec["model"] = config.registry().get("default") or ""
        running["busy"] = True
        running["starting"] = True
        store.append(rec, "user", text)
        if (rec.get("title") or "New session") == "New session":
            rec["title"] = _title(text)
        store.save(rec)
        await ws.send_text(_json({"type": "state_delta", "name": rec["title"]}))

        run = plan.Plan(rec["id"], text, rec.get("project") or None, rec.get("model") or None)
        plan_state["run"] = run
        await _drive(plan.explore(run), rec["id"])
        running["busy"] = False

    async def start_assistant(text: str):
        """A turn in the side conversation. It never touches the agent's window."""
        if not (text or "").strip():
            return
        if running["assistant"]:
            await ws.send_text(_json({"type": "notify", "level": "warn",
                                      "message": "the assistant is already replying"}))
            return
        if not rec.get("model"):
            rec["model"] = config.registry().get("default") or ""
        cfg = assistant.settings_of(rec)
        assistant.append(rec, "user", text)
        store.save(rec)
        running["assistant"] = True
        running["assistant_stop"] = threading.Event()

        queue: asyncio.Queue = asyncio.Queue()
        collected = []

        def worker():
            try:
                for ev in assistant.run_turn(rec, text, cfg, ref=rec.get("model") or None,
                                             stop=running["assistant_stop"]):
                    if ev.get("type") == "text":
                        collected.append(ev["delta"])
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {**ev, "type": "assistant_" + str(ev.get("type")), "sid": rec["id"]})
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait,
                                          {"type": "assistant_error", "message": str(e),
                                           "sid": rec["id"]})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=worker, daemon=True).start()
        await pump(queue)
        running["assistant"] = False
        if collected:
            assistant.append(rec, "assistant", "".join(collected))
        store.save(rec)
        await ws.send_text(_json({"type": "assistant_saved", "sid": rec["id"],
                                  "preview": assistant.preview(rec)}))

    async def start_turn(text: str, images=None):
        if running["busy"]:
            await ws.send_text(_json({"type": "notify", "level": "warn",
                                      "message": "a turn is already running"}))
            return
        if not (text or "").strip():
            return
        if not rec.get("model"):
            first = config.registry().get("default") or ""
            if first:
                rec["model"] = first

        if agent.should_compact(rec):
            info = agent.compact_history(rec, ref=rec.get("model") or None)
            if info and info.get("compacted"):
                store.save(rec)
                saved = token_mod.estimate_tokens("x" * int(info.get("chars_before") or 0)) \
                    - token_mod.estimate_tokens("x" * int(info.get("chars_after") or 0))
                metrics.bump(rec, saved_compaction=max(0, saved))
                await ws.send_text(_json({
                    "type": "notify", "level": "info", "sid": rec["id"],
                    "message": (f"compacted {info['compacted']} earlier messages "
                                f"({info['chars_before']} \u2192 {info['chars_after']} chars)")}))

        running["busy"] = True
        running["starting"] = True
        running["stop"] = threading.Event()
        store.append(rec, "user", text)
        if (rec.get("title") or "New session") == "New session":
            rec["title"] = _title(text)
        store.save(rec)
        await ws.send_text(_json({"type": "state_delta", "name": rec["title"],
                                  "model": rec.get("model")}))

        chat = (rec.get("mode") or "agent") == "chat"
        messages = [{"role": "system",
                     "content": prompts.system_prompt(rec.get("project"), False, chat=chat)}]
        messages.extend(_history(rec))

        block = "" if chat else _memory_block(rec.get("project"))
        if block:
            messages.append({"role": "system", "content": block})
            metrics.bump(rec, memory_tokens=token_mod.estimate_tokens(block))

        if not chat:
            _account_context(rec)

        if images:
            if config.accepts_images(rec.get("model")):
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "user":
                        messages[i] = {"role": "user", "content": _content_parts(text, images)}
                        break
            else:
                await ws.send_text(_json({
                    "type": "notify", "level": "warn", "sid": rec["id"],
                    "message": (f"{rec.get('model') or 'this model'} does not accept images — "
                                f"sent the text only")}))
        queue: asyncio.Queue = asyncio.Queue()
        project = rec.get("project") or None
        ref = rec.get("model") or None
        collected = []

        def worker():
            try:
                for ev in agent.run_turn(messages, project=project, ref=ref, chat=chat,
                                         stop=running["stop"], steer=running["steer"],
                                         reasoning=config.reasoning_for(rec.get("thinking"))):
                    kind = ev.get("type")
                    if kind == "text":
                        collected.append(ev["delta"])
                    elif kind == "usage":
                        ev = _usage_event(rec, ev.get("usage") or {})
                    elif kind == "tool_start":
                        rec["tool_calls"] = int(rec.get("tool_calls") or 0) + 1
                    if kind in ("text", "reason", "usage", "tool_start", "tool_end"):
                        running["starting"] = False
                    loop.call_soon_threadsafe(queue.put_nowait, {**ev, "sid": rec["id"]})
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait,
                                          {"type": "error", "message": str(e), "sid": rec["id"]})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        t = threading.Thread(target=worker, daemon=True)
        running["thread"] = t
        t.start()
        await pump(queue)
        running["busy"] = False
        if collected:
            store.append(rec, "assistant", "".join(collected))
        store.save(rec)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")

            if kind == "prompt":
                if msg.get("plan") and not running["busy"]:
                    asyncio.create_task(start_plan(msg.get("message") or ""))
                else:
                    asyncio.create_task(start_turn(msg.get("message") or "", msg.get("images")))

            elif kind == "steer":
                text = msg.get("message") or ""
                if text.strip():
                    running["steer"].append(text)
                    await ws.send_text(_json({"type": "notify", "level": "info",
                                              "message": "steering applied",
                                              "sid": rec["id"]}))

            elif kind == "abort":
                running["stop"].set()
                running["assistant_stop"].set()
                running["steer"].clear()

            elif kind == "assistant_prompt":
                asyncio.create_task(start_assistant(msg.get("message") or ""))

            elif kind == "assistant_clear":
                assistant.clear(rec)
                store.save(rec)
                await ws.send_text(_json({"type": "assistant_cleared", "sid": rec["id"],
                                          "preview": assistant.preview(rec)}))

            elif kind == "assistant_settings":
                cfg = assistant.save_settings(rec, msg.get("patch") or {})
                store.save(rec)
                await ws.send_text(_json({"type": "assistant_settings", "settings": cfg,
                                          "preview": assistant.preview(rec), "sid": rec["id"]}))

            elif kind == "switch":
                target = msg.get("sid") or ""
                nxt = store.get(target) if target else None
                if nxt:
                    rec = nxt
                    sid = rec["id"]
                for key in ("model", "mode", "thinking"):
                    if msg.get(key):
                        rec[key] = msg[key]
                store.save(rec)
                await ws.send_text(_json({"type": "hello", **_meta(rec, running)}))

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
                fresh = store.create(model=rec.get("model") or "", mode=rec.get("mode") or "agent",
                                     thinking=rec.get("thinking") or "medium",
                                     project=rec.get("project") or "")
                rec = fresh
                sid = rec["id"]
                await ws.send_text(_json({"type": "hello", **_meta(rec, running)}))
                await ws.send_text(_json({"type": "session_ready", "sid": rec["id"],
                                          "model": rec.get("model"), "mode": rec.get("mode"),
                                          "thinking": rec.get("thinking"),
                                          "name": rec.get("title")}))

            elif kind == "get_state":
                await ws.send_text(_json({"type": "hello", **_meta(rec, running)}))

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
                    await ws.send_text(_json({"type": "truncated", "index": cut,
                                              "userTurnIndex": turn if isinstance(turn, int) else cut,
                                              "sid": rec["id"]}))

            elif kind == "bash":
                res = agent.run_terminal(msg.get("command") or "", rec.get("project") or None)
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
                run = plan_state.get("run")
                if not run:
                    await ws.send_text(_json({"type": "rpc_response", "command": "plan_answers",
                                              "ok": False, "error": "no plan in progress",
                                              "sid": rec["id"]}))
                else:
                    running["busy"] = True
                    running["starting"] = True
                    await _drive(plan.synthesise(run, msg.get("answers") or {}), rec["id"])
                    running["busy"] = False

            elif kind == "plan_approve":
                run = plan_state.get("run")
                res = plan.approve(run, bool(msg.get("approved"))) if run else \
                    {"saved": False, "message": "no plan to approve"}
                store.save(rec)
                await ws.send_text(_json({"type": "plan_approved", **res, "sid": rec["id"]}))

            elif kind == "plan_abort":
                run = plan_state.get("run")
                if run:
                    run.cancelled = True
                plan_state["run"] = None
                running["busy"] = False
                await ws.send_text(_json({"type": "plan_status", "phase": "cancelled",
                                          "detail": "cancelled", "explorers": [], "sid": rec["id"]}))

            elif kind == "plan_implement":
                run = plan_state.get("run")
                if run and run.text.strip():
                    prompt = plan.implement_prompt(run)
                    plan_state["run"] = None
                    # The approval notice promises a fresh session: give the
                    # implementation a clean context and leave the planning
                    # discussion (and its explorers) behind.
                    fresh = store.create(model=rec.get("model") or "",
                                         mode=rec.get("mode") or "agent",
                                         thinking=rec.get("thinking") or "medium",
                                         project=rec.get("project") or "")
                    rec = fresh
                    sid = rec["id"]
                    await ws.send_text(_json({"type": "hello", **_meta(rec, running)}))
                    await ws.send_text(_json({"type": "session_ready", "sid": rec["id"],
                                              "model": rec.get("model"), "mode": rec.get("mode"),
                                              "thinking": rec.get("thinking"),
                                              "name": rec.get("title")}))
                    asyncio.create_task(start_turn(prompt))
                else:
                    await ws.send_text(_json({"type": "notify", "level": "warn",
                                              "message": "approve a plan first",
                                              "sid": rec["id"]}))

            elif kind == "compact":
                info = agent.compact_history(rec, ref=rec.get("model") or None, force=True)
                store.save(rec)
                await ws.send_text(_json({
                    "type": "rpc_response", "command": "compact",
                    "ok": bool(info and info.get("compacted")), "data": info, "sid": rec["id"]}))

            else:
                await ws.send_text(_json({
                    "type": "rpc_response", "command": kind, "ok": False,
                    "error": f"{kind} is not supported", "sid": rec["id"]}))

    except WebSocketDisconnect:
        return
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass

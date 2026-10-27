import asyncio

import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import (agent, anthropic, config, folders, hosting, mcp_registry, memory_modes,
                memory_store, metrics, plugin_manager, project_context, session_runtime,
                session_state, skills, store, vcs)
from .. import tokens as token_mod
from ..ai import engine, prompts

router = APIRouter()

# The old fixed vocabulary. Nothing in the interface is built from it any more: a
# level is offered only after the model has been seen to accept it. It survives so a
# preference saved before this change is not rejected as invalid input.
THINKING_LEVELS = ["default", "off", "minimal", "low", "medium", "high", "xhigh", "max"]

# Model refs with a detection probe already running. The picker polls while a
# probe is in flight; without this set every poll started another probe.
_thinking_probes: set[str] = set()


def thinking_meta(ref: str | None = None) -> dict:
    from .. import thinking

    return thinking.meta(ref)


def _thinking_block(ref: str | None = None) -> dict:
    from .. import thinking

    return {
        "thinking_levels": thinking.levels(ref),
        "thinking_info": thinking.meta(ref),
        "thinking_supports_off": thinking.supports_off(ref),
    }


def _ok(**kw):
    return {"ok": True, **kw}


def _fail(error: str):
    return JSONResponse({"ok": False, "error": error})


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@router.get("/api/info")
async def info():
    reg = config.registry()
    default = reg.get("default") or ""
    rows = config.model_list()
    resolved = config.resolve_model(default) if default else None
    from .. import autoupdater
    return {
        "models": rows,
        "default": default,
        "providers": list((reg.get("providers") or {}).keys()),
        "llm_url": (resolved or {}).get("baseUrl", ""),
        "llm_online": True,
        "thinking_levels": _thinking_block(default or (resolved or {}).get("ref"))["thinking_levels"],
        "thinking_info": thinking_meta(default or (resolved or {}).get("ref")),
        "default_thinking": config.prefs().get("thinking", "default"),
        # the deleted plan item 5: the brand slot says which checkout this is. This is the
        # same string the auto-updater compares against, read through the same
        # function, empty when the install predates version tracking, and the
        # client says so rather than showing a blank that looks like a bug.
        "version": autoupdater.local_version(),
    }


@router.get("/api/providers")
async def providers():
    reg = config.registry()
    out = {}
    for pid, spec in (reg.get("providers") or {}).items():
        key = spec.get("apiKey")
        out[pid] = {
            "baseUrl": spec.get("baseUrl", ""),
            "api": spec.get("api", "openai-completions"),
            "apiKey": ({"kind": "env", "env": key, "env_set": bool(config.key_for(spec))}
                       if isinstance(key, str) and key.isupper() else
                       {"kind": "value", "set": bool(config.key_for(spec))}),
            "models": spec.get("models") or [],
        }
    return {"default": reg.get("default", ""), "providers": out}


@router.post("/api/providers")
async def add_provider(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    base = (body.get("baseUrl") or "").strip().rstrip("/")
    if not name or not base:
        return _fail("name and baseUrl are required")
    reg = config.registry()
    reg.setdefault("providers", {})[name] = {
        "baseUrl": base,
        "api": body.get("api") or "openai-completions",
        "apiKey": body.get("apiKey") or "",
        "models": body.get("models") or [],
    }
    config.save_registry(reg)
    found = []
    try:
        details = engine.remote_model_details(base, config.key_for(reg["providers"][name]))
        have = {m.get("id") for m in reg["providers"][name]["models"]}
        for d in details:
            mid = d["id"]
            if mid not in have:
                reg["providers"][name]["models"].append(
                    {"id": mid, "name": mid,
                     "contextWindow": d.get("contextWindow") or 0,
                     "maxTokens": d.get("maxTokens") or 0})
                found.append(mid)
        config.save_registry(reg)
    except engine.EngineError as e:
        return _ok(provider=name, discovered=[], probe_error=str(e))
    return _ok(provider=name, discovered=found)


@router.patch("/api/providers/{name}")
async def patch_provider(name: str, request: Request):
    body = await request.json()
    reg = config.registry()
    spec = (reg.get("providers") or {}).get(name)
    if not spec:
        return _fail("provider not found")
    for k in ("baseUrl", "api", "apiKey"):
        if body.get(k) is not None:
            spec[k] = body[k]
    config.save_registry(reg)
    return _ok(provider=name)


@router.delete("/api/providers/{name}")
async def delete_provider(name: str):
    reg = config.registry()
    if name not in (reg.get("providers") or {}):
        return _fail("provider not found")
    del reg["providers"][name]
    cur = reg.get("default") or ""
    if cur == name or cur.startswith(f"{name}/"):
        nxt = next(iter(reg.get("providers") or {}), "")
        first = next((m.get("id") for m in (reg["providers"].get(nxt, {}).get("models") or [])
                      if m.get("id")), "")
        reg["default"] = f"{nxt}/{first}" if nxt and first else ""
    config.save_registry(reg)
    return _ok(default=reg.get("default", ""))


@router.post("/api/providers/{name}/models")
async def add_model(name: str, request: Request):
    body = await request.json()
    mid = (body.get("id") or "").strip()
    if not mid:
        return _fail("id required")
    reg = config.registry()
    spec = (reg.get("providers") or {}).get(name)
    if not spec:
        return _fail("provider not found")
    spec.setdefault("models", [])
    if any(m.get("id") == mid for m in spec["models"]):
        return _fail("model already present")
    spec["models"].append({"id": mid, "name": body.get("name") or mid,
                           "reasoning": bool(body.get("reasoning")),
                           "contextWindow": _int(body.get("contextWindow")),
                           "maxTokens": _int(body.get("maxTokens"))})
    config.save_registry(reg)
    return _ok(added=mid)


@router.post("/api/providers/{name}/models/remove")
async def remove_model(name: str, request: Request):
    body = await request.json()
    mid = (body.get("id") or "").strip()
    reg = config.registry()
    spec = (reg.get("providers") or {}).get(name)
    if not spec:
        return _fail("provider not found")
    spec["models"] = [m for m in (spec.get("models") or []) if m.get("id") != mid]
    config.save_registry(reg)
    return _ok(removed=mid)


@router.post("/api/providers/{name}/probe")
def probe_provider(name: str):
    # `def`: engine.remote_models() is a blocking HTTP request to the provider with a
    # real timeout, and this is a button in Settings. On the loop it could hold the
    # whole server for the length of a network round-trip, or until it times out.
    reg = config.registry()
    spec = (reg.get("providers") or {}).get(name)
    if not spec:
        return _fail("provider not found")
    try:
        ids = engine.remote_models(spec.get("baseUrl", ""), config.key_for(spec))
    except engine.EngineError as e:
        return {"ok": False, "error": str(e), "ids": []}
    have = {m.get("id") for m in (spec.get("models") or [])}
    # Reconcile in both directions. Additions are offered for import; entries the
    # provider stopped listing are marked, because a retired model left in the picker
    # fails only later, at send time, with a status code nobody explains.
    gone = sorted(str(mid) for mid in have if mid and mid not in ids)
    changed = False
    for m in (spec.get("models") or []):
        want = m.get("id") in gone
        if bool(m.get("stale")) != want:
            m["stale"] = want
            changed = True
    if changed:
        config.save_registry(reg)
    return _ok(ids=ids, models=ids, new=[i for i in ids if i not in have], gone=gone)


@router.post("/api/providers/{name}/prune")
async def prune_models(name: str):
    """Drop the entries this provider stopped listing.

    Marking happens automatically on probe; deleting stays a click. A model that
    comes back should not have to be re-imported, and a person may still want the
    row for the history that references it.
    """
    reg = config.registry()
    spec = (reg.get("providers") or {}).get(name)
    if not spec:
        return _fail("provider not found")
    rows = spec.get("models") or []
    removed = [m.get("id") for m in rows if m.get("stale")]
    if removed:
        spec["models"] = [m for m in rows if not m.get("stale")]
        config.save_registry(reg)
    return _ok(removed=removed, left=len(spec["models"]))


@router.post("/api/providers/{name}/import")
async def import_models(name: str, request: Request):
    body = await request.json()
    ids = body.get("ids") or []
    reg = config.registry()
    spec = (reg.get("providers") or {}).get(name)
    if not spec:
        return _fail("provider not found")
    spec.setdefault("models", [])
    have = {m.get("id") for m in spec["models"]}
    # enrich newly imported models with context windows if the endpoint exposes them
    meta = {}
    try:
        meta = {d["id"]: d for d in engine.remote_model_details(
            spec.get("baseUrl", ""), config.key_for(spec))}
    except engine.EngineError:
        meta = {}
    added = []
    for mid in ids:
        if mid and mid not in have:
            d = meta.get(mid) or {}
            spec["models"].append({"id": mid, "name": mid,
                                   "contextWindow": d.get("contextWindow") or 0,
                                   "maxTokens": d.get("maxTokens") or 0})
            added.append(mid)
    config.save_registry(reg)
    # Published thinking controls are attached for free where the provider exposes
    # them. The probe path is deliberately not used here: importing thirty models
    # should not silently spend thirty sets of requests.
    if added and "ollama" in str(spec.get("baseUrl") or ""):
        from .. import thinking
        for mid in added:
            try:
                ref = f"{name}/{mid}"
                thinking.save(ref, thinking.detect(ref, probe=False))
            except Exception:  # noqa: BLE001
                pass
    return _ok(added=added)


@router.post("/api/default")
async def set_default(request: Request):
    body = await request.json()
    ref = (body.get("model") or "").strip()
    reg = config.registry()
    # `known_model`, not `resolve_model`: the fallback must never answer for a
    # name the caller typed and make it look valid.
    if not ref or not config.known_model(ref):
        return _fail("unknown model")
    pid, _, mid = ref.partition("/")
    entry = next((m for m in ((reg.get("providers") or {}).get(pid) or {}).get("models") or []
                  if m.get("id") == mid), {})
    if entry.get("stale"):
        # Better to say it now than to fail mid-turn with a 410 nobody explains.
        return _fail("%s is no longer offered by %s" % (mid, pid))
    reg["default"] = ref
    config.save_registry(reg)
    # Learning what this model can do is worth a few small requests the first time
    # a person chooses it, and only the once: the answer is cached on the model.
    try:
        from .. import thinking
        thinking.ensure(ref)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "default": ref}


@router.post("/api/terminal")
async def terminal(request: Request):
    body = await request.json()
    # `session` so a terminal with no project opens in that session's own scratch
    # folder rather than the shared default one.
    return agent.run_terminal(body.get("command") or "", body.get("project") or None,
                              body.get("timeout"), session=body.get("session") or "")


def _enrich_projects(items: list[dict]) -> list[dict]:
    """Annotate projects with git state for the Git panel (full=1)."""
    for p in items:
        path = p.get("path") or ""
        p["isRepo"] = False
        p["branch"] = ""
        p["github_full_name"] = ""
        top = vcs.vc(path, ["rev-parse", "--show-toplevel"])
        if not top.get("ok"):
            continue
        p["isRepo"] = True
        root = (top.get("stdout") or "").strip() or path
        st = vcs.vc(root, ["status", "--porcelain=v1", "--branch"])
        if st.get("ok"):
            p["branch"] = vcs.parse_status(st.get("stdout") or "").get("branch") or ""
        for r in vcs.remotes(root):
            parsed = hosting.parse_remote(r.get("url") or "")
            if parsed:
                p["github_full_name"] = parsed["full_name"]
                break
    return items


@router.get("/api/projects")
async def projects(full: int = 0):
    """Recent workspaces only, a folder is a workspace because the user
    pointed at it, never because it sits under some guessed root."""
    rows = store.list_sessions()
    recent, seen = [], set()
    for s in rows:
        p = s.get("project")
        if not p or p in seen or not config.Path(p).is_dir():
            continue
        seen.add(p)
        recent.append({"name": config.Path(p).name, "path": p})
        if len(recent) >= 12:
            break
    items = [{"name": r["name"], "path": r["path"], "root": ""} for r in recent]
    if full:
        # version-control probing spawns subprocesses, so keep it off the event
        # loop and only pay for it when the panel explicitly asks (full=1).
        items = await asyncio.to_thread(_enrich_projects, items)
    return {"ok": True, "recent": recent, "projects": items,
            "roots": config.project_roots()}


@router.get("/api/fs/roots")
async def fs_roots():
    return _ok(roots=config.fs_roots(), home=str(config.USER_HOME))


@router.get("/api/fs/list")
async def fs_list(path: str = ""):
    return config.fs_list(path)


@router.post("/api/fs/mkdir")
async def fs_mkdir(request: Request):
    body = await request.json()
    parent = str(body.get("path") or "").strip()
    name = str(body.get("name") or "").strip()
    if not parent or not name or any(ch in name for ch in "/\\"):
        return _fail("a folder name is required")
    target = config.Path(parent) / name
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _fail("that folder already exists")
    except Exception as e:
        return _fail(str(e))
    return _ok(path=str(target))


@router.get("/api/tokens/dashboard")
def token_dashboard(sid: str = "", project: str = ""):
    """Local token accounting: what Tacit injects, and what it avoided.

    The baseline is *our own* full-context estimate (every discovered MCP schema
    and every enabled memory injected directly). No competitor numbers are
    guessed, the two figures compared are both Tacit's.
    """
    rec = store.get(sid) if sid else None
    base_prompt = token_mod.estimate_tokens(prompts.system_prompt(project or None, False, chat=False))
    tools = agent.tools_for()
    tool_tokens = token_mod.estimate_tools_tokens(tools)

    mcp = mcp_registry.injection_report()
    mem_stats = memory_store.stats(project)
    # Both figures come from the mode, because the mode is the one switch that
    # decides memory. `memory_store.startup_selection` selects on budget alone
    #, it would happily report a startup block for a session in `off` mode,
    # while `memory_modes.startup` is what the turn actually injects. The
    # dashboard used to gate on the retired Memory Vault plugin, which let it
    # describe a setting nobody had just changed.
    mem_block = memory_modes.startup(project)
    mem_on = memory_modes.is_on()

    dash = metrics.dashboard(
        rec or {},
        base_prompt_tokens=base_prompt,
        tools=tool_tokens,
        mcp_discovered=mcp["discovered_tokens"],
        memory_total=mem_stats["total_tokens"],
        mcp_injected=mcp["injected_tokens"],
    )
    dash["memory_enabled"] = mem_on
    dash["memory_injected_tokens"] = mem_block["tokens"] if mem_on else 0
    if not mem_on:
        dash["actual_startup"] -= dash["memory_tokens"]
        dash["memory_tokens"] = 0
        dash["saved_by_discipline"] = max(0, dash["full_context_baseline"], dash["actual_startup"])
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
                "base_prompt_tokens",
                "tool_schema_tokens", "mcp_discovered_tokens", "mcp_injected_tokens",
                "memory_tokens", "saved_lazy_tools", "saved_memory_budget",
                "saved_compaction", "saved_subagent", "saved_total",
                "full_context_baseline", "actual_startup", "saved_by_discipline"):
        dash[key + "_display"] = token_mod.label(dash.get(key) or 0)
    dash["exact"] = token_mod.exact()
    dash["cache_hit_rate"] = dash.get("cache_hit_rate") or 0.0
    # Both of these enlarge the starting prompt, so both are reported rather than
    # folded silently into the base figure. The instruction block is per-project;
    # the task list exists only once something is in it.
    m = metrics.get(rec or {})
    dash["instruction_tokens"] = m["instruction_tokens"]
    dash["instruction_tokens_display"] = token_mod.label(m["instruction_tokens"])
    dash["task_tokens"] = m["task_tokens"]
    dash["task_tokens_display"] = token_mod.label(m["task_tokens"])
    try:
        dash["instructions"] = project_context.cost_preview(project or None)
    except Exception:  # noqa: BLE001
        dash["instructions"] = {}
    # Which caching strategy applies to the model in use, and why. A provider that
    # caches automatically needs nothing sent; one that does not gets breakpoints;
    # and saying which is which is the same rule the sandbox follows when it reports
    # `mechanism: none` rather than implying a boundary it cannot enforce.
    try:
        resolved = config.resolve_model((rec or {}).get("model")) or {}
        kind = anthropic.flavour(resolved)
        markers = anthropic.cache_enabled(resolved)
        dash["caching"] = {
            "mode": config.PROMPT_CACHE,
            "flavour": kind,
            "breakpoints": bool(markers and kind == "anthropic"),
            "passthrough": bool(markers and kind == "openrouter"),
            "automatic": kind == "openai",
            # "active" means caching is in play by some mechanism, not that Tacit
            # sent markers. An OpenAI-compatible endpoint caches its prefix on its
            # own (a measured 71% hit rate on one) and reporting that as inactive
            # would understate what the user is actually getting.
            "active": bool(markers or kind == "openai"),
            "sends_markers": bool(markers),
            "summary": {
                "anthropic": "native Messages API, cache breakpoints sent on tools, "
                             "system and the last message",
                "openrouter": "OpenAI shape, one breakpoint on the system block, "
                              "forwarded to Anthropic models",
                "openai": "OpenAI shape, the provider caches the prefix itself; "
                          "nothing added to the request",
            }.get(kind, ""),
        }
    except Exception:  # noqa: BLE001
        dash["caching"] = {}
    dash["mcp"] = mcp
    dash["memory"] = mem_stats
    dash["plugins"] = plugin_manager.token_impact()
    dash["tool_count"] = len(tools)
    return {"ok": True, **dash}


@router.get("/api/prompts")
def prompt_inventory(sid: str = "", project: str = ""):
    """Every prompt Tacit hardcodes, read from the constants that send them.

    The standing prompt's cost has always been on the dashboard; this is the
    rest of it: tool descriptions, guidance, the delivery orders, the Assistant
    persona, the compaction, plan, research, learning and memory text. The
    operator can read what the harness says rather than trusting it.
    """
    from .. import prompts_catalog
    rec = store.get(sid) if sid else None
    return prompts_catalog.catalog(project or (rec or {}).get("project") or None, sid)


@router.get("/api/snapshots")
def snapshots(limit: int = 200, session: str = ""):
    from .. import extras
    rows = extras.snapshot_index(limit, session=session)
    total = sum(int(r.get("bytes") or 0) for r in rows)
    return {"ok": True, "snapshots": rows, "count": len(rows), "bytes": total}


@router.get("/api/tasks/{sid}")
def tasks_state(sid: str):
    """The session's task list, for the strip above the composer.

    Read-only: the model owns the list. The strip shows what it holds and
    refreshes on tool_end events for the tasks tool.
    """
    from ..plugins import task_list
    if not plugin_manager.is_enabled("task_list"):
        return _fail("the task list plugin is not enabled")
    rows = task_list.load(sid)
    return {"ok": True, "tasks": rows,
            "open": sum(1 for r in rows if not r.get("done")),
            "count": len(rows)}


@router.post("/api/snapshots/compare")
async def snapshot_compare(request: Request):
    from .. import extras
    body = await request.json()
    res = extras.snapshot_compare(body.get("name") or "", body.get("project") or "")
    return res if res.get("ok") else _fail(res.get("error") or "could not compare")


@router.post("/api/snapshots/restore")
async def snapshot_restore(request: Request):
    from .. import extras
    body = await request.json()
    name, project = body.get("name") or "", body.get("project") or ""
    if not name or not project:
        return _fail("name and project are required")
    out = extras.restore(name, project)
    if isinstance(out, str) and out.startswith("ERROR"):
        return _fail(out)
    return _ok(message=out, name=name, project=project)


@router.get("/api/assistant/{sid}")
def assistant_state(sid: str):
    """The side conversation, its settings, and what those settings cost."""
    from .. import assistant
    rec = store.get(sid)
    if not rec:
        return _fail("session not found")
    cfg = assistant.settings_of(rec)
    return {"ok": True, "messages": rec.get("assistant") or [], "settings": cfg,
            "preview": assistant.preview(rec, cfg),
            # The run state rides with the thread state: the panel needs both to
            # render, and one fetch per open is one fewer thing that can be
            # momentarily out of step with the other.
            "goal": assistant.goal_of(rec),
            "autonomy": session_runtime.autonomy_state(sid),
            "models": config.model_list(),
            "thinking_levels": _thinking_block(rec.get("model") or None)["thinking_levels"],
            "thinking_info": thinking_meta(rec.get("model") or None),
            "session_model": rec.get("model") or "",
            "session_thinking": rec.get("thinking") or "default"}


@router.post("/api/assistant/{sid}/settings")
async def assistant_settings(sid: str, request: Request):
    from .. import assistant
    rec = store.get(sid)
    if not rec:
        return _fail("session not found")
    cfg = assistant.save_settings(rec, await request.json())
    # patch, not save: this endpoint owns assistantSettings and nothing else.
    # A full save here would race the record a running turn is holding.
    store.patch(sid, assistantSettings=cfg)
    return {"ok": True, "settings": cfg, "preview": assistant.preview(rec, cfg)}


@router.delete("/api/assistant/{sid}")
async def assistant_clear(sid: str):
    from .. import assistant
    rec = store.get(sid)
    if not rec:
        return _fail("session not found")
    assistant.clear(rec)
    store.save(rec)
    return {"ok": True, "preview": assistant.preview(rec)}


@router.delete("/api/assistant/{sid}/message/{index}")
async def assistant_message_delete(sid: str, index: int):
    """Remove one Assistant message. Index is the row position the pane shows."""
    from .. import assistant
    rec = store.get(sid)
    if not rec:
        return _fail("session not found")
    if not assistant.delete_message(rec, index):
        return _fail("no such message")
    store.save(rec)
    cfg = assistant.settings_of(rec)
    return {"ok": True, "messages": rec.get("assistant") or [],
            "preview": assistant.preview(rec, cfg)}


# ── autonomous mode ────────────────────────────────────────────────────────
# Both of these answer with the full state, not just what changed: the pane
# shows a cycle count and an elapsed budget it must not have to assemble
# itself, and a second tab that toggled the mode should not be looking at a
# stale one.
@router.get("/api/assistant/{sid}/autonomy")
async def autonomy_get(sid: str):
    from .. import assistant
    rec = store.get(sid)
    if not rec:
        return _fail("session not found")
    return {"ok": True, "goal": assistant.goal_of(rec),
            "autonomy": assistant.autonomy_of(rec)}


@router.post("/api/assistant/{sid}/autonomy")
async def autonomy_post(sid: str, request: Request):
    from .. import assistant
    if not store.get(sid):
        return _fail("session not found")
    body = await request.json()
    if "goal" in body:
        session_runtime.goal_set(sid, body.get("goal"))
    state = session_runtime.autonomy_set(
        sid, enabled=body.get("enabled"), max_cycles=body.get("max_cycles"),
        max_hours=body.get("max_hours"))
    if state.get("ok") is False:
        return _fail(state.get("error") or "could not start the run")
    return {"ok": True, "autonomy": state, "goal": assistant.goal_of(store.get(sid) or {})}


@router.post("/api/assistant/{sid}/autonomy/reset")
async def autonomy_reset(sid: str):
    """Zero the tally without touching the switch, the person is about to
    judge the run against a fresh budget, not start a new one."""
    from .. import assistant
    rec = store.get(sid)
    if not rec:
        return _fail("session not found")
    state = assistant.reset_counters(rec)
    store.patch(sid, assistantAutonomy=state)
    return {"ok": True, "autonomy": state}


@router.get("/api/folders")
async def list_folders():
    """Folders the user has used before, for the directory pickers."""
    return _ok(folders=folders.list_folders())


@router.post("/api/folders")
async def remember_folder(request: Request):
    body = await request.json()
    res = folders.add(body.get("path") or "")
    return res if res.get("ok") else _fail(res.get("error") or "could not save that folder")


@router.post("/api/folders/remove")
async def forget_folder(request: Request):
    body = await request.json()
    res = folders.remove(body.get("path") or "")
    return res if res.get("ok") else _fail(res.get("error") or "could not remove that folder")


@router.get("/api/sessions")
def list_sessions():
    return {"sessions": store.list_sessions()}


@router.get("/api/sessions/state")
async def sessions_state():
    """Which sessions have a turn running right now, keyed by sid.

    This is the interface's source for the per-session indicators (plan stage
    2): busy, starting, the kind of work, when it began, and how many turns
    the session has started. Process-local by design, a session is busy
    because a worker in this process is running its turn, and for no other
    reason.
    """
    states = session_state.all_states()
    return {"ok": True, "states": states,
            "busy": [sid for sid, s in states.items() if s.get("busy")]}


@router.get("/api/sessions/registry")
def get_registry():
    # `def`, not `async def`: store.registry() opens and parses one file per session
    # (up to 200) and this is the largest and slowest thing the page fetches at boot.
    # On the loop it delayed everything else in the process, including the handshake.
    return store.registry()


@router.post("/api/sessions/registry")
async def post_registry(request: Request):
    return store.merge(await request.json())


@router.get("/api/sessions/{sid}")
def get_session(sid: str):
    rec = store.get(sid)
    if not rec:
        return _fail("session not found")
    return rec


@router.get("/api/sessions/{sid}/transcript")
def transcript(sid: str):
    rec = store.get(sid)
    if not rec:
        return _fail("session not found")
    return {"messages": rec.get("messages") or []}


@router.post("/api/sessions/{sid}/metadata")
async def set_metadata(sid: str, request: Request):
    body = await request.json()
    rec = store.patch(sid, title=body.get("name"), model=body.get("model"),
                      mode=body.get("mode"), thinking=body.get("thinking"),
                      project=body.get("workdir") or body.get("project"))
    if not rec:
        return _fail("session not found")
    return _ok(session=rec["id"])


@router.post("/api/sessions/{sid}/rename")
async def rename(sid: str, request: Request):
    body = await request.json()
    rec = store.patch(sid, title=(body.get("title") or "").strip() or None)
    if not rec:
        return _fail("session not found")
    return _ok(name=rec.get("title"))


@router.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    return _ok(removed=store.remove(sid))


@router.get("/api/harness/thinking")
async def get_thinking(ref: str = "", ensure: int = 0):
    from .. import thinking

    chosen = ref or config.prefs().get("model") or config.registry().get("default") or ""
    detected = False
    if ensure and chosen and not thinking.known(chosen):
        # ensure() is several synchronous probe requests (up to 120s each against a
        # dead endpoint). Awaiting it here blocks uvicorn's whole event loop, the
        # interface, every socket and every poll with it, so it is run in a spare
        # thread instead: this request answers at once with what is already known,
        # and the picker re-reads the cache once the probe lands.
        import threading as _threading

        # The picker polls every few seconds while a probe runs, and each poll
        # would start another probe here; several threads of paid requests against
        # the same endpoint, all racing to cache the same answer. One at a time per
        # model; the pollers keep asking and pick the result up when it lands.
        detected = True
        if chosen not in _thinking_probes:
            _thinking_probes.add(chosen)

            def _detect():
                try:
                    thinking.ensure(chosen)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    _thinking_probes.discard(chosen)
            _threading.Thread(target=_detect, daemon=True).start()
    return {"default_thinking": config.prefs().get("thinking", "default"),
            "ref": chosen, **_thinking_block(chosen),
            "detected": detected,
            "thinking_info": thinking.meta(chosen)}


@router.post("/api/harness/thinking")
async def set_thinking(request: Request):
    from .. import thinking

    body = await request.json()
    level = body.get("default_thinking") or body.get("level")
    ref = body.get("ref") or config.registry().get("default") or ""
    allowed = thinking.levels(ref)
    if level not in allowed and level not in THINKING_LEVELS:
        return _fail("this model does not offer " + str(level))
    config.save_prefs({"thinking": level})
    return _ok(default_thinking=level, thinking_levels=allowed,
               will_send=thinking.send(ref, config.reasoning_for(level)))


@router.post("/api/thinking/detect")
async def detect_thinking(request: Request):
    """Ask the model what it can do, and cache the answer on it.

    Free for ollama, which publishes a list. A handful of small requests for anyone
    else, because an OpenAI-compatible endpoint that accepts a word and ignores it is
    indistinguishable from one that supports it without trying it.
    """
    from .. import thinking

    body = await request.json()
    ref = (body.get("ref") or "").strip()
    if not ref or not config.known_model(ref):
        return _fail("unknown model")
    found = thinking.detect(ref, deep=bool(body.get("deep")))
    if not found.get("ok"):
        return _fail(found.get("error") or "the model did not answer")
    saved = thinking.save(ref, found)
    # `saved` already carries ref/values/source/graded; passing ref again would
    # duplicate the keyword and 500 the endpoint.
    saved.pop("ref", None)
    return _ok(ref=ref, **saved, **_thinking_block(ref))


@router.get("/api/harness/context-files")
async def get_context_files():
    return _ok(no_context_files=bool(config.prefs().get("noContextFiles")))


@router.post("/api/harness/context-files")
async def set_context_files(request: Request):
    body = await request.json()
    config.save_prefs({"noContextFiles": bool(body.get("no_context_files"))})
    return _ok()


@router.get("/api/harness/version-control")
async def get_version_control():
    """Whether the agent may run version-control commands. Off by default."""
    return _ok(allow_version_control=config.allow_vcs())


@router.post("/api/harness/version-control")
async def set_version_control(request: Request):
    body = await request.json()
    config.save_prefs({"allowVersionControl": bool(body.get("allow"))})
    return _ok(allow_version_control=config.allow_vcs())


@router.get("/api/harness/delegate-model")
async def get_delegate_model():
    """The model delegated work (sub-agents, research) runs on. Empty = the session's."""
    return _ok(delegate_model=config.delegate_model(),
               models=config.model_list())


@router.post("/api/harness/delegate-model")
async def set_delegate_model(request: Request):
    body = await request.json()
    ref = str(body.get("delegate_model") or "").strip()
    if ref and not config.known_model(ref):
        return _fail("unknown model")
    config.save_prefs({"delegateModel": ref})
    return _ok(delegate_model=config.delegate_model())


@router.get("/api/harness/fallback-model")
async def get_fallback_model():
    """The model a session runs on when its own is gone. Empty = fail instead."""
    return _ok(fallback_model=config.fallback_model(),
               models=config.model_list())


@router.post("/api/harness/fallback-model")
async def set_fallback_model(request: Request):
    body = await request.json()
    ref = str(body.get("fallback_model") or "").strip()
    if ref and not config.known_model(ref):
        return _fail("unknown model")
    config.save_prefs({"fallbackModel": ref})
    return _ok(fallback_model=config.fallback_model())


@router.get("/api/harness/summariser-model")
async def get_summariser_model():
    """The model that writes compaction notes. Empty = the session's own."""
    return _ok(summariser_model=config.summariser_model(),
               models=config.model_list())


@router.post("/api/harness/summariser-model")
async def set_summariser_model(request: Request):
    body = await request.json()
    ref = str(body.get("summariser_model") or "").strip()
    if ref and not config.known_model(ref):
        return _fail("unknown model")
    config.save_prefs({"summariserModel": ref})
    return _ok(summariser_model=config.summariser_model())


@router.get("/api/harness/deliver-guarantee")
async def get_deliver_guarantee():
    """Whether the turn is forced to land the file the task names. On by default."""
    return _ok(deliver_guarantee=config.deliver_guarantee())


@router.post("/api/harness/deliver-guarantee")
async def set_deliver_guarantee(request: Request):
    body = await request.json()
    config.save_prefs({"deliverGuarantee": bool(body.get("deliver_guarantee"))})
    return _ok(deliver_guarantee=config.deliver_guarantee())


@router.get("/api/harness/limits")
async def get_limits():
    """The turn budgets, as the next turn will read them, plus their bounds."""
    return _ok(limits=config.limits(), bounds=config.limit_bounds(),
               pinned=config.limits_pinned())


@router.post("/api/harness/limits")
async def set_limits(request: Request):
    """Raise or lower a turn budget. Only the keys that arrived are written.

    Out of range is refused rather than clamped quietly: a panel that accepted 500
    and ran 200 would be reporting a number that is not the one in force.
    """
    body = await request.json()
    changes = {}
    for name, (pref_key, _env, _const, lo, hi) in config.LIMIT_KEYS.items():
        if name not in body:
            continue
        try:
            value = int(str(body.get(name)).strip())
        except (TypeError, ValueError):
            return _fail(f"{name} is not a number")
        if value < lo or value > hi:
            return _fail(f"{name} must be between {lo} and {hi}")
        changes[pref_key] = value
    if not changes:
        return _fail("no budget to save")
    config.save_prefs(changes)
    return _ok(limits=config.limits(), pinned=config.limits_pinned())


@router.get("/api/harness/sound")
async def get_sound():
    """The two chime switches: the turn-end pair (on by default) and mid-turn
    alerts (off by default)."""
    return _ok(turn_sound=config.turn_sound(), alert_sound=config.alert_sound())


@router.post("/api/harness/sound")
async def set_sound(request: Request):
    body = await request.json()
    # Only the keys that actually arrived are written. The panel's two switches
    # post on their own, and a handler that defaulted the missing one would
    # silence the other switch every time either was clicked.
    changes = {}
    if "turn_sound" in body:
        changes["turnSound"] = bool(body.get("turn_sound"))
    if "alert_sound" in body:
        changes["alertSound"] = bool(body.get("alert_sound"))
    if changes:
        config.save_prefs(changes)
    return _ok(turn_sound=config.turn_sound(), alert_sound=config.alert_sound())


@router.get("/api/tools")
async def get_tools():
    from .. import agent as agent_mod
    disabled = set(config.prefs().get("disabledTools") or [])
    rows = []
    for t in agent_mod.TOOLS:
        fn = t["function"]
        # The archive reader's switch is its own pref, not the shared
        # disabledTools list: its default lives in config.archive_tool, and a
        # default that lived in disabledTools would have to be seeded into
        # every existing install to be true. The tab therefore shows the state
        # the switch actually owns.
        enabled = (config.archive_tool() if fn["name"] == agent_mod.ARCHIVE_TOOL
                   else fn["name"] not in disabled)
        rows.append({"name": fn["name"], "description": fn["description"],
                     "enabled": enabled})
    # `shellOverlap` rides along because the Tools tab is its home and the tab
    # must render the current mode, not assume the default. The vocabulary comes
    # from config so the switch cannot drift from what shell_overlap() accepts;
    # the labels stay in the JS, where they belong.
    return {"builtin": rows, "tools": rows, "disabled": sorted(disabled),
            "shellOverlap": config.shell_overlap(),
            "shellOverlapModes": list(config.SHELL_OVERLAP_MODES),
            "shellOverlapPinned": config.shell_overlap_pinned(),
            "archiveToolPinned": config.archive_tool_pinned(),
            "note": "Toggles apply to new agent turns. Chat mode arms no tools at all."}


@router.post("/api/tools/toggle")
async def toggle_tool(request: Request):
    body = await request.json()
    name = body.get("name")
    if not name:
        return _fail("name required")
    # shellOverlap is not a tool, it is a three-state behaviour switch that
    # lives on this endpoint because the Tools tab is its only home. It takes a
    # `mode`, never an `enabled`: a boolean cannot say "advise", and silently
    # coercing one would turn "off" into "block" on the way through.
    if name == "shellOverlap":
        mode = str(body.get("mode") or "").strip().lower()
        if mode not in config.SHELL_OVERLAP_MODES:
            return _fail("mode must be one of " + " / ".join(config.SHELL_OVERLAP_MODES))
        if config.shell_overlap_pinned():
            return _fail("TACIT_SHELL_OVERLAP pins this to " + config.shell_overlap()
                         + "; unset it to change the mode here")
        config.save_prefs({"shellOverlap": mode})
        return _ok(name="shellOverlap", mode=mode)
    # The archive reader toggles its own pref, for the same reason /api/tools
    # reports its state from that pref: the default lives in config, not in a
    # disabledTools entry that would have to be seeded everywhere.
    from .. import agent as agent_mod
    if name == agent_mod.ARCHIVE_TOOL:
        if config.archive_tool_pinned():
            return _fail("TACIT_ARCHIVE_TOOL pins this switch; unset it to change it here")
        config.save_prefs({"archiveTool": bool(body.get("enabled"))})
        return _ok(name=name, enabled=config.archive_tool())
    disabled = set(config.prefs().get("disabledTools") or [])
    if body.get("enabled"):
        disabled.discard(name)
    else:
        disabled.add(name)
    config.save_prefs({"disabledTools": sorted(disabled)})
    return _ok(name=name, enabled=name not in disabled)


@router.get("/api/skills")
async def skills_list():
    rows = skills.load()

    def row(s):
        return {"name": s["name"], "description": s["description"], "path": s["path"],
                "body": s.get("body") or "", "kind": s.get("kind") or "skill",
                "tokens": s.get("tokens") or 0}

    # One list: every skill and knowledge note, distinguished by the `kind`
    # tag alone. The old shape split them into three lists, one of them
    # (`tool_skills`) hardcoded empty, and the panel rendered the empties as
    # sections. Nothing external consumes this shape, so the API and the
    # panel moved together.
    return {"skills": [row(s) for s in rows],
            "user_skills_dir": str(config.SKILLS_DIR),
            "knowledge_dir": str(config.KNOWLEDGE_DIR)}


@router.post("/api/skills/user")
async def skill_create(request: Request):
    body = await request.json()
    res = skills.create(body.get("name"), body.get("body") or body.get("text"),
                        body.get("description") or "")
    return res if res.get("ok") else _fail(res.get("error") or "could not create the skill")


@router.delete("/api/skills/user/{name}")
async def skill_delete(name: str):
    res = skills.remove(name)
    return res if res.get("ok") else _fail(res.get("error") or "could not delete the skill")


@router.post("/api/server/restart")
async def server_restart():
    """Stop this server process and start a fresh one on the same port."""
    from .. import extras
    res = extras.restart_server()
    return res if res.get("ok") else _fail(res.get("error") or "could not restart")


@router.get("/api/server/status")
def server_status():
    """Where this process listens and which pid it is, the General tab's Server section."""
    from .. import agent as agent_mod
    from .. import extras
    helpers = [t["function"]["name"] for t in agent_mod.tools_for()
               if t["function"]["name"] in agent_mod.MCP_HELPER_NAMES]
    # `last_restart` is the restart that just happened, successful or not. The
    # watcher that knows a restart failed has no server to report through, so it
    # leaves the outcome on disk and this reports it, which also means a server
    # started by hand after a failure reports the failure it replaced.
    return _ok(host=config.HOST, port=config.PORT, pid=os.getpid(),
               mcp_helpers=len(helpers),
               mcp_servers=len(mcp_registry.load()["servers"]),
               last_restart=extras.restart_state())


@router.get("/api/processes")
def processes():
    """Everything Tacit has spawned and not yet reaped, from the live registry.

    The point is attribution: on a machine full of python.exe, this says which
    pids are Tacit's, what role each plays, and which session owns it, so a
    stuck child can be killed deliberately instead of guessed at.
    """
    from .. import proctools
    return _ok(processes=proctools.rows(), pid=os.getpid())


# ═══════════════════════════ AUTOUPDATER ═══════════════════════════════════
@router.get("/api/autoupdater/status")
def autoupdater_status():
    """Get autoupdater status: enabled, last check, last commit, etc."""
    from .. import autoupdater
    return _ok(**autoupdater.get_status())


@router.post("/api/autoupdater/check")
async def autoupdater_check(request: Request):
    """Check git remote for available updates."""
    from .. import autoupdater
    res = await autoupdater.check_for_updates()
    return res if res.get("ok") else _fail(res.get("error") or "check failed")


@router.post("/api/autoupdater/pull")
async def autoupdater_pull(request: Request):
    """Pull updates from git remote."""
    from .. import autoupdater
    body = await request.json()
    dry_run = bool(body.get("dry_run"))
    res = await autoupdater.pull_updates(dry_run=dry_run)
    return res if res.get("ok") else _fail(res.get("error") or "pull failed")


@router.post("/api/autoupdater/toggle")
async def autoupdater_toggle(request: Request):
    """Enable or disable the autoupdater."""
    from .. import autoupdater
    body = await request.json()
    enabled = bool(body.get("enabled"))
    res = autoupdater.set_enabled(enabled)
    return res if res.get("ok") else _fail(res.get("error") or "toggle failed")


@router.post("/api/autoupdater/interval")
async def autoupdater_interval(request: Request):
    """Set the auto-check interval in seconds."""
    from .. import autoupdater
    body = await request.json()
    seconds = int(body.get("seconds") or 3600)
    res = autoupdater.set_check_interval(seconds)
    return res if res.get("ok") else _fail(res.get("error") or "interval update failed")

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import agent, anthropic, config, folders, hosting, mcp_registry, memory_store, metrics, plugin_manager, project_context, skills, store, vcs
from .. import tokens as token_mod
from ..ai import engine, prompts

router = APIRouter()

# The old fixed vocabulary. Nothing in the interface is built from it any more: a
# level is offered only after the model has been seen to accept it. It survives so a
# preference saved before this change is not rejected as invalid input.
THINKING_LEVELS = ["default", "off", "minimal", "low", "medium", "high", "xhigh", "max"]


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
    return {
        "models": rows,
        "default": default,
        "providers": list((reg.get("providers") or {}).keys()),
        "llm_url": (resolved or {}).get("baseUrl", ""),
        "llm_online": True,
        "thinking_levels": _thinking_block(default or (resolved or {}).get("ref"))["thinking_levels"],
        "thinking_info": thinking_meta(default or (resolved or {}).get("ref")),
        "default_thinking": config.prefs().get("thinking", "default"),
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
async def probe_provider(name: str):
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
    if not ref or not config.resolve_model(ref):
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
    return agent.run_terminal(body.get("command") or "", body.get("project") or None,
                              body.get("timeout"))


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
    """Recent workspaces only — a folder is a workspace because the user
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
async def token_dashboard(sid: str = "", project: str = ""):
    """Local token accounting: what Tacit injects, and what it avoided.

    The baseline is *our own* full-context estimate (every discovered MCP schema
    and every enabled memory injected directly). No competitor numbers are
    guessed — the two figures compared are both Tacit's.
    """
    rec = store.get(sid) if sid else None
    base_prompt = token_mod.estimate_tokens(prompts.system_prompt(project or None, False, chat=False))
    tools = agent.tools_for()
    tool_tokens = token_mod.estimate_tools_tokens(tools)

    mcp = mcp_registry.injection_report()
    mem_stats = memory_store.stats(project)
    mem_block = memory_store.startup_selection(project)
    mem_on = plugin_manager.is_enabled("memory_vault")

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
        dash["saved_by_discipline"] = max(0, dash["full_context_baseline"] - dash["actual_startup"])
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
            # own — a measured 71% hit rate on one — and reporting that as inactive
            # would understate what the user is actually getting.
            "active": bool(markers or kind == "openai"),
            "sends_markers": bool(markers),
            "summary": {
                "anthropic": "native Messages API — cache breakpoints sent on tools, "
                             "system and the last message",
                "openrouter": "OpenAI shape — one breakpoint on the system block, "
                              "forwarded to Anthropic models",
                "openai": "OpenAI shape — the provider caches the prefix itself; "
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


@router.get("/api/snapshots")
async def snapshots(limit: int = 200, session: str = ""):
    from .. import extras
    rows = extras.snapshot_index(limit, session=session)
    total = sum(int(r.get("bytes") or 0) for r in rows)
    return {"ok": True, "snapshots": rows, "count": len(rows), "bytes": total}


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
async def assistant_state(sid: str):
    """The side conversation, its settings, and what those settings cost."""
    from .. import assistant
    rec = store.get(sid)
    if not rec:
        return _fail("session not found")
    cfg = assistant.settings_of(rec)
    return {"ok": True, "messages": rec.get("assistant") or [], "settings": cfg,
            "preview": assistant.preview(rec, cfg),
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
    store.save(rec)
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
async def list_sessions():
    return {"sessions": store.list_sessions()}


@router.get("/api/sessions/registry")
async def get_registry():
    return store.registry()


@router.post("/api/sessions/registry")
async def post_registry(request: Request):
    return store.merge(await request.json())


@router.get("/api/sessions/{sid}")
async def get_session(sid: str):
    rec = store.get(sid)
    if not rec:
        return _fail("session not found")
    return rec


@router.get("/api/sessions/{sid}/transcript")
async def transcript(sid: str):
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
    found = None
    if ensure and chosen and not thinking.known(chosen):
        # Asked for explicitly by the picker the first time a model is shown, so the
        # cost of measuring is paid once, in the view that needs the answer.
        found = thinking.ensure(chosen)
    return {"default_thinking": config.prefs().get("thinking", "default"),
            "ref": chosen, **_thinking_block(chosen),
            "detected": bool(found and found.get("ok") and not found.get("cached")),
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
    if not ref or not config.resolve_model(ref):
        return _fail("unknown model")
    found = thinking.detect(ref, deep=bool(body.get("deep")))
    if not found.get("ok"):
        return _fail(found.get("error") or "the model did not answer")
    saved = thinking.save(ref, found)
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


@router.get("/api/tools")
async def get_tools():
    from .. import agent as agent_mod
    disabled = set(config.prefs().get("disabledTools") or [])
    rows = []
    for t in agent_mod.TOOLS:
        fn = t["function"]
        rows.append({"name": fn["name"], "description": fn["description"],
                     "enabled": fn["name"] not in disabled})
    return {"builtin": rows, "tools": rows, "disabled": sorted(disabled),
            "note": "Toggles apply to new agent turns. Chat mode arms no tools at all."}


@router.post("/api/tools/toggle")
async def toggle_tool(request: Request):
    body = await request.json()
    name = body.get("name")
    if not name:
        return _fail("name required")
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

    return {"tool_skills": [],
            "knowledge": [row(s) for s in rows if s.get("kind") == "knowledge"],
            "user_skills": [row(s) for s in rows if s.get("kind") != "knowledge"],
            "user_skills_dir": str(config.SKILLS_DIR)}


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

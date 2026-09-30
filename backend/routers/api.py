from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import agent, config, skills, store
from ..ai import engine

router = APIRouter()

THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]


def _ok(**kw):
    return {"ok": True, **kw}


def _fail(error: str):
    return JSONResponse({"ok": False, "error": error})


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
        "thinking_levels": THINKING_LEVELS,
        "default_thinking": config.prefs().get("thinking", "medium"),
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
        ids = engine.remote_models(base, config.key_for(reg["providers"][name]))
        have = {m.get("id") for m in reg["providers"][name]["models"]}
        for mid in ids:
            if mid not in have:
                reg["providers"][name]["models"].append({"id": mid, "name": mid})
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
                           "reasoning": bool(body.get("reasoning"))})
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
    return _ok(ids=ids)


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
    added = []
    for mid in ids:
        if mid and mid not in have:
            spec["models"].append({"id": mid, "name": mid})
            added.append(mid)
    config.save_registry(reg)
    return _ok(added=added)


@router.post("/api/default")
async def set_default(request: Request):
    body = await request.json()
    ref = (body.get("model") or "").strip()
    reg = config.registry()
    if not ref or not config.resolve_model(ref):
        return _fail("unknown model")
    reg["default"] = ref
    config.save_registry(reg)
    return {"ok": True, "default": ref}


@router.post("/api/terminal")
async def terminal(request: Request):
    body = await request.json()
    return agent.run_terminal(body.get("command") or "", body.get("project") or None,
                              body.get("timeout"))


@router.get("/api/projects")
async def projects():
    rows = store.list_sessions()
    roots = config.all_project_roots(rows)
    recent, seen = [], set()
    for s in rows:
        p = s.get("project")
        if not p or p in seen or not config.Path(p).is_dir():
            continue
        seen.add(p)
        recent.append({"name": config.Path(p).name, "path": p})
        if len(recent) >= 8:
            break
    return {"ok": True, "roots": roots, "root": roots[0] if roots else "",
            "recent": recent, "projects": config.list_projects(roots)}


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
async def get_thinking():
    return {"default_thinking": config.prefs().get("thinking", "medium"),
            "levels": THINKING_LEVELS}


@router.post("/api/harness/thinking")
async def set_thinking(request: Request):
    body = await request.json()
    level = body.get("default_thinking") or body.get("level")
    if level not in THINKING_LEVELS:
        return _fail("unknown level")
    config.save_prefs({"thinking": level})
    return _ok(default_thinking=level)


@router.get("/api/harness/context-files")
async def get_context_files():
    return _ok(no_context_files=bool(config.prefs().get("noContextFiles")))


@router.post("/api/harness/context-files")
async def set_context_files(request: Request):
    body = await request.json()
    config.save_prefs({"noContextFiles": bool(body.get("no_context_files"))})
    return _ok()


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
    return {"tool_skills": [], "knowledge": [],
            "user_skills": [{"name": s["name"], "description": s["description"],
                             "path": s["path"], "tokens": s["tokens"]} for s in rows],
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

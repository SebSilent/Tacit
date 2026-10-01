"""HTTP surface for capabilities, providers and the audit ledger."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import audit, config, providers

router = APIRouter()


def _ok(**kw):
    return {"ok": True, **kw}


def _fail(error: str):
    return JSONResponse({"ok": False, "error": error})


@router.get("/api/capabilities")
async def capabilities():
    from .. import memory_modes, sandbox

    return _ok(sandbox_mode=sandbox.describe(), memory_report=memory_modes.report(),
               **providers.summary())


@router.get("/api/capabilities/providers")
async def provider_list(kind: str = ""):
    rows = providers.by_kind(kind) if kind else providers.registry()
    return _ok(providers=rows, kinds=list(providers.KINDS))


def _set_backend(kind: str, value: str) -> dict:
    """Only ever accept a backend that is actually usable right now."""
    row = providers.get(value, kind)
    if row is None:
        return _fail(f"'{value}' is not a known {kind} backend")
    if not row["implemented"]:
        return _fail(f"'{value}' has no adapter in Tacit yet, so it cannot be selected")
    if not row["available"]:
        return _fail(f"'{value}' is not available: {row['reason'] or 'unavailable'}")
    return row


@router.post("/api/capabilities/sandbox")
async def set_sandbox(request: Request):
    body = await request.json()
    backend = str(body.get("backend") or "").strip()
    patch = {}
    if backend:
        found = _set_backend("sandbox", backend)
        if isinstance(found, JSONResponse) or not found.get("ok", True):
            return found
        patch["backend"] = backend
    limits = {k: body[k] for k in ("network", "timeout", "memory_mb", "cpu_seconds",
                                   "readonly_project") if k in body}
    if limits:
        patch.update(limits)
    if not patch:
        return _fail("nothing to change")
    providers.save({"sandbox": patch})
    audit.record("sandbox_mode_changed", backend=providers.resolve("sandbox")["id"],
                 mode=str(limits or backend), status="ok")
    return _ok(sandbox=providers.resolve("sandbox"))


@router.post("/api/capabilities/memory")
async def set_memory(request: Request):
    body = await request.json()
    mode = str(body.get("mode") or "").strip()
    patch = {}
    if mode:
        found = _set_backend("memory", mode)
        if isinstance(found, JSONResponse) or not found.get("ok", True):
            return found
        patch["mode"] = mode
    for key in ("budget", "ttl_days", "reinforce"):
        if key in body:
            patch[key] = body[key]
    if not patch:
        return _fail("nothing to change")
    providers.save({"memory": patch})
    audit.record("memory_mode_changed", backend=providers.resolve("memory")["id"],
                 mode=mode or "", status="ok")
    return _ok(memory=providers.resolve("memory"))


@router.post("/api/capabilities/learning")
async def set_learning(request: Request):
    body = await request.json()
    mode = str(body.get("mode") or "").strip()
    if not mode:
        return _fail("a mode is required")
    found = _set_backend("learning", mode)
    if isinstance(found, JSONResponse) or not found.get("ok", True):
        return found
    providers.save({"learning": {"mode": mode}})
    audit.record("learning_mode_changed", backend=mode, mode=mode, status="ok")
    return _ok(learning=providers.resolve("learning"))


@router.post("/api/capabilities/profile")
async def set_profile(request: Request):
    body = await request.json()
    name = str(body.get("profile") or "").strip()
    if not name:
        return _fail("a profile is required")
    providers.save({"profile": name})
    audit.record("profile_changed", mode=name, status="ok")
    return _ok(profile=name, **providers.summary())


@router.post("/api/capabilities/gateway")
async def set_gateway(request: Request):
    body = await request.json()
    gid = str(body.get("id") or "").strip()
    if not gid:
        return _fail("a gateway is required")
    if gid != "none":
        row = providers.get(gid, "gateway")
        if row is None:
            return _fail(f"'{gid}' is not a known gateway")
        if not row["available"]:
            return _fail(f"'{gid}' is not available: {row['reason'] or 'unavailable'}")
        if not row["implemented"]:
            return _fail(f"'{gid}' has no adapter in Tacit yet")
    providers.save({"gateway": {"id": gid}})
    audit.record("gateway_mode_changed", backend=gid, mode=gid, status="ok")
    return _ok(gateway=providers.resolve("gateway"))


@router.get("/api/deps")
async def deps_status():
    """What optional dependencies exist, and what is missing here."""
    from .. import deps

    return _ok(**deps.status())


@router.get("/api/deps/{name}")
async def deps_check(name: str):
    from .. import deps

    got = deps.check(name)
    return got if got.get("ok") else _fail(got.get("error") or "unknown group")


@router.get("/api/migrate/hint")
async def migrate_hint():
    """One line stating that nothing is detected. Shown in the interface."""
    from .. import migrate

    return _ok(hint=migrate.sources_hint(), suffixes=list(migrate.TEXT_SUFFIXES),
               max_files=migrate.MAX_FILES)


@router.post("/api/migrate/preview")
async def migrate_preview(request: Request):
    """Read the path you named and report what an import would do. Writes nothing."""
    import asyncio

    from .. import migrate

    body = await request.json()
    res = await asyncio.to_thread(migrate.preview, body.get("path") or "")
    return res if res.get("ok") else _fail(res.get("error") or "nothing found there")


@router.post("/api/migrate/import")
async def migrate_import(request: Request):
    """One-time import from a path you chose. Additive, then finished."""
    import asyncio

    from .. import migrate

    body = await request.json()
    res = await asyncio.to_thread(migrate.import_items, body.get("path") or "",
                                  body.get("session") or "")
    return res if res.get("ok") else _fail(res.get("error") or "nothing to import")


@router.get("/api/migrate/export")
async def migrate_export(style: str = "prose"):
    """Tacit memory as portable text, plus its skills. Placing it is your call."""
    from .. import migrate

    return _ok(text=migrate.export_text(style), **migrate.export_skills())


@router.get("/api/learning")
async def learning_list(state: str = ""):
    from .. import learning

    return _ok(artifacts=learning.list_artifacts(state), stats=learning.stats(),
               kinds=list(learning.KINDS), scopes=list(learning.SCOPES))


@router.post("/api/learning/propose")
async def learning_propose(request: Request):
    from .. import learning

    body = await request.json()
    res = learning.propose(body.get("kind") or "rule", body.get("title") or "",
                           body.get("body") or "", scope=body.get("scope") or "global",
                           scope_key=body.get("scope_key") or "",
                           confidence=body.get("confidence") or "medium",
                           source_session=body.get("session") or "")
    return res if res.get("ok") else _fail(res.get("error") or "could not record it")


@router.post("/api/learning/{artifact_id}/{action}")
async def learning_action(artifact_id: str, action: str):
    from .. import learning

    fn = {"approve": learning.approve, "reject": learning.reject,
          "disable": learning.disable, "enable": learning.enable}.get(action)
    if fn is None:
        return _fail(f"unknown action '{action}'")
    res = fn(artifact_id)
    return res if res.get("ok") else _fail(res.get("error") or "could not do that")


@router.patch("/api/learning/{artifact_id}")
async def learning_edit(artifact_id: str, request: Request):
    from .. import learning

    body = await request.json()
    res = learning.edit(artifact_id, body.get("title") or "", body.get("body") or "")
    return res if res.get("ok") else _fail(res.get("error") or "could not edit it")


@router.delete("/api/learning/{artifact_id}")
async def learning_delete(artifact_id: str):
    from .. import learning

    res = learning.remove(artifact_id)
    return res if res.get("ok") else _fail(res.get("error") or "could not delete it")


# ── the background analyzer ───────────────────────────────────────────
# It finds learning moments and writes proposals. Approving one is the only thing
# that ever reaches memory or a skill, and that stays a person's click.

@router.get("/api/analyzer")
async def analyzer_status():
    from .. import analyzer

    return _ok(**analyzer.status())


@router.post("/api/analyzer")
async def analyzer_configure(request: Request):
    from .. import analyzer

    body = await request.json()
    on = body.get("enabled")
    interval = body.get("interval_s")
    return _ok(**analyzer.configure(
        None if on is None else bool(on),
        None if interval is None else int(interval or 0)))


@router.post("/api/analyzer/run")
async def analyzer_run(request: Request):
    """Sweep now instead of waiting for the timer."""
    from .. import analyzer

    try:
        body = await request.json()
    except Exception:
        body = {}
    force = bool((body or {}).get("force"))
    res = analyzer.run_once(force=force)
    # merged rather than double-splatted: both dicts carry counts, and a collision
    # here is a 500 rather than a wrong number.
    merged = {**res, **analyzer.status()}
    merged["proposals"] = res.get("proposals", 0)
    return _ok(**merged)


@router.get("/api/learning/proposals")
async def learning_proposals():
    """Only what is waiting on a decision, newest first, with its provenance."""
    from .. import learning

    rows = learning.list_artifacts("proposed")
    return _ok(proposals=rows, count=len(rows), stats=learning.stats())


@router.get("/api/gateways")
async def gateway_list():
    from .. import gateways

    return _ok(gateways=gateways.describe(), selected=gateways.selected())


@router.post("/api/gateways")
async def gateway_add(request: Request):
    from .. import gateways

    res = gateways.add(await request.json())
    return res if res.get("ok") else _fail(res.get("error") or "could not add it")


@router.delete("/api/gateways/{gateway_id}")
async def gateway_remove(gateway_id: str):
    from .. import gateways

    res = gateways.remove(gateway_id)
    return res if res.get("ok") else _fail(res.get("error") or "could not remove it")


@router.post("/api/gateways/{gateway_id}/select")
async def gateway_select(gateway_id: str):
    from .. import gateways

    res = gateways.select(gateway_id)
    if res.get("ok"):
        audit.record("gateway_mode_changed", backend=gateway_id, mode=gateway_id, status="ok")
    return res if res.get("ok") else _fail(res.get("error") or "could not select it")


@router.post("/api/gateways/{gateway_id}/run")
async def gateway_run(gateway_id: str, request: Request):
    import asyncio

    from .. import gateways

    body = await request.json()
    res = await asyncio.to_thread(
        gateways.run, gateway_id, body.get("task") or "", body.get("cwd") or None,
        body.get("timeout"), body.get("session") or "")
    return res if res.get("ok") else _fail(res.get("error") or "the gateway did not answer")


@router.get("/api/audit")
async def read_audit(limit: int = 200, session: str = "", event: str = ""):
    return _ok(entries=audit.recent(limit, session, event), total=audit.count())


@router.post("/api/audit/clear")
async def clear_audit():
    return audit.clear()

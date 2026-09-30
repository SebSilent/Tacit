"""HTTP surface for MCP servers, tools, activation and the audit log."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import mcp_registry, tokens

router = APIRouter()


def _ok(**kw):
    return {"ok": True, **kw}


def _fail(error: str):
    return JSONResponse({"ok": False, "error": error})


@router.get("/api/mcp/servers")
async def servers():
    return _ok(servers=mcp_registry.list_servers(), settings=mcp_registry.settings(),
               injection=mcp_registry.injection_report())


@router.post("/api/mcp/servers")
async def add_server(request: Request):
    res = mcp_registry.add_server(await request.json())
    return res if res.get("ok") else _fail(res.get("error") or "could not add server")


@router.patch("/api/mcp/servers/{sid}")
async def update_server(sid: str, request: Request):
    res = mcp_registry.update_server(sid, await request.json())
    return res if res.get("ok") else _fail(res.get("error") or "could not update server")


@router.delete("/api/mcp/servers/{sid}")
async def remove_server(sid: str):
    res = mcp_registry.remove_server(sid)
    return res if res.get("ok") else _fail(res.get("error") or "could not remove server")


@router.post("/api/mcp/servers/{sid}/{action}")
async def server_action(sid: str, action: str):
    if action == "start":
        res = mcp_registry.start(sid)
    elif action == "stop":
        res = mcp_registry.stop(sid)
    elif action == "restart":
        mcp_registry.stop(sid)
        res = mcp_registry.start(sid)
    elif action == "discover":
        found = mcp_registry.discover(sid)
        res = {"ok": True, "tools": found}
    else:
        return _fail(f"unknown action '{action}'")
    return res if res.get("ok") else _fail(res.get("error") or f"{action} failed")


@router.get("/api/mcp/tools")
async def tools_index(search: str = "", limit: int = 500):
    rows = mcp_registry.search(search, limit) if search else mcp_registry.all_tools()
    return _ok(tools=rows, injection=mcp_registry.injection_report())


@router.post("/api/mcp/tools/{sid}/{name}/pin")
async def pin_tool(sid: str, name: str, request: Request):
    body = await request.json()
    res = mcp_registry.set_pinned(sid, name, bool(body.get("pinned")))
    return res if res.get("ok") else _fail(res.get("error") or "could not pin tool")


@router.post("/api/mcp/activate")
async def activate(request: Request):
    body = await request.json()
    keys = body.get("keys") or body.get("tool_keys") or []
    res = mcp_registry.activate(list(keys), body.get("ttl_turns"))
    return _ok(**res, injection=mcp_registry.injection_report())


@router.post("/api/mcp/deactivate")
async def deactivate(request: Request):
    body = await request.json()
    res = mcp_registry.deactivate(list(body.get("keys") or []))
    return _ok(**res, injection=mcp_registry.injection_report())


@router.post("/api/mcp/call")
async def call_tool(request: Request):
    body = await request.json()
    res = mcp_registry.call(str(body.get("server_id") or ""), str(body.get("tool_name") or ""),
                            body.get("arguments") or {}, confirmed=bool(body.get("confirm")))
    if res.get("ok"):
        return _ok(result=res.get("result"), tokens=res.get("tokens"), ms=res.get("ms"))
    return _fail(res.get("error") or "call failed")


@router.get("/api/mcp/audit")
async def audit(limit: int = 100):
    return _ok(events=mcp_registry.recent_audit(limit))


@router.get("/api/mcp/settings")
async def get_settings():
    return _ok(settings=mcp_registry.settings(), injection=mcp_registry.injection_report())


@router.put("/api/mcp/settings")
async def put_settings(request: Request):
    return _ok(settings=mcp_registry.save_settings(await request.json()))


@router.get("/api/mcp/injection")
async def injection():
    report = mcp_registry.injection_report()
    report["discovered_display"] = tokens.label(report["discovered_tokens"])
    report["injected_display"] = tokens.label(report["injected_tokens"])
    report["saved_display"] = tokens.label(report["saved_tokens"])
    return _ok(**report)

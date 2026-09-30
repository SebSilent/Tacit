"""HTTP surface for the DSH Bridge.

The bridge itself lives in ``backend/plugins/dsh_bridge.py`` (bundled, disabled
by default as an *agent capability*). The panel needs to import and scaffold
regardless of whether its tools are exposed to the model, so this router calls
the module directly.

Nothing here installs packages or launches anything: importing registers a
**disabled** MCP server, and scaffolding only writes files under
``~/.tacit/adapters``.
"""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config, mcp_registry
from ..plugins import dsh_bridge

router = APIRouter()


def _ok(**kw):
    return {"ok": True, **kw}


def _fail(error: str):
    return JSONResponse({"ok": False, "error": error})


def _split(result) -> dict:
    """The bridge returns a plain string; turn 'ERROR: …' into ok=False."""
    text = str(result or "")
    if text.startswith("ERROR:"):
        return {"ok": False, "error": text[6:].strip()}
    return {"ok": True, "message": text}


def _node() -> dict:
    path = shutil.which("node")
    npx = shutil.which("npx")
    return {"node": bool(path), "npx": bool(npx), "node_path": path or "",
            "bridge": str(config.BRIDGES_DIR / "dsh-host.mjs"),
            "bridge_exists": (config.BRIDGES_DIR / "dsh-host.mjs").exists()}


@router.get("/api/dsh/status")
async def status():
    state = dsh_bridge._load_state()
    rows = []
    for entry in state.get("plugins") or []:
        verdict = dsh_bridge._classify(entry)
        rows.append({"name": entry.get("name") or entry.get("package"),
                     "package": entry.get("package") or "",
                     "command": entry.get("command") or "",
                     "server_id": entry.get("server_id") or "",
                     **verdict})
    servers = {s["id"] for s in mcp_registry.list_servers()}
    return _ok(plugins=rows, count=len(rows), toolchain=_node(),
               mcp_servers=sorted(servers),
               text=dsh_bridge.call("dsh_status", {}, {}) or "")


@router.post("/api/dsh/import")
async def import_plugin(request: Request):
    body = await request.json()
    package = str(body.get("package") or body.get("id") or "").strip()
    if not package:
        return _fail("a package name or command is required")
    res = _split(dsh_bridge.call("dsh_import", {
        "package": package,
        "command": body.get("command") or "",
        "name": body.get("name") or "",
    }, {}))
    if res.get("ok"):
        res["servers"] = mcp_registry.list_servers()
    return res


@router.post("/api/dsh/scaffold")
async def scaffold(request: Request):
    body = await request.json()
    plugin_id = str(body.get("plugin_id") or body.get("package") or "").strip()
    if not plugin_id:
        return _fail("a plugin id is required")
    res = _split(dsh_bridge.call("dsh_scaffold", {"plugin_id": plugin_id}, {}))
    if res.get("ok"):
        res["path"] = str(config.ADAPTERS_DIR / plugin_id)
    return res

"""HTTP surface for the plugin manager."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import plugin_manager

router = APIRouter()


def _ok(**kw):
    return {"ok": True, **kw}


def _fail(error: str):
    return JSONResponse({"ok": False, "error": error})


@router.get("/api/plugins")
async def list_plugins():
    return _ok(plugins=plugin_manager.list_plugins(), impact=plugin_manager.token_impact())


@router.post("/api/plugins/{plugin_id}/enable")
async def enable(plugin_id: str):
    res = plugin_manager.enable(plugin_id)
    return res if res.get("ok") else _fail(res.get("error") or "could not enable")


@router.post("/api/plugins/{plugin_id}/disable")
async def disable(plugin_id: str):
    res = plugin_manager.disable(plugin_id)
    return res if res.get("ok") else _fail(res.get("error") or "could not disable")


@router.get("/api/plugins/{plugin_id}/settings")
async def get_settings(plugin_id: str):
    res = plugin_manager.settings_of(plugin_id)
    return res if res.get("ok") else _fail(res.get("error") or "unknown plugin")


@router.put("/api/plugins/{plugin_id}/settings")
async def put_settings(plugin_id: str, request: Request):
    res = plugin_manager.save_settings(plugin_id, await request.json())
    return res if res.get("ok") else _fail(res.get("error") or "could not save settings")


@router.get("/api/plugins/{plugin_id}/logs")
async def logs(plugin_id: str):
    return _ok(id=plugin_id, logs=plugin_manager.logs(plugin_id))

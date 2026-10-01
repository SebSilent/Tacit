"""HTTP surface for capability profiles."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import memory_store, profiles, tokens

router = APIRouter()


def _ok(**kw):
    return {"ok": True, **kw}


def _fail(error: str):
    return JSONResponse({"ok": False, "error": error})


@router.get("/api/profiles")
async def list_profiles():
    rows = profiles.list_profiles()
    return _ok(profiles=rows, active=profiles.load()["active"], current=profiles.current(),
               memory_budget=memory_store.budget(), exact=tokens.exact())


@router.get("/api/profiles/current")
async def current():
    return _ok(current=profiles.current(), active=profiles.load()["active"])


@router.post("/api/profiles/{name}/apply")
async def apply(name: str):
    res = profiles.apply(name)
    return res if res.get("ok") else _fail(res.get("error") or "could not apply profile")


@router.post("/api/profiles")
async def capture(request: Request):
    body = await request.json()
    res = profiles.capture(body.get("name") or "", body.get("label") or "",
                           body.get("description") or "")
    return res if res.get("ok") else _fail(res.get("error") or "could not save profile")


@router.delete("/api/profiles/{name}")
async def delete(name: str):
    res = profiles.delete(name)
    return res if res.get("ok") else _fail(res.get("error") or "could not delete profile")

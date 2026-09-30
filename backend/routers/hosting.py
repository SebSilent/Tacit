import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, Request

from .. import config, hosting, vcs

router = APIRouter()


@router.get("/api/github/account")
async def account():
    a = await hosting.auth()
    if not a["token"]:
        return {"ok": False, "connected": False, "source": "none",
                "error": "no GitHub credentials found — paste a token below, or sign in "
                         "with the GitHub CLI (its session is picked up automatically)"}
    r = await hosting.api("/user", a["token"])
    if not r["ok"]:
        return {"ok": False, "connected": False, "source": a["source"],
                "error": hosting.error_text(r)}
    who = r["json"] or {}
    return {"ok": True, "connected": True, "source": a["source"],
            "login": who.get("login", ""), "name": who.get("name", ""),
            "avatar": who.get("avatar_url", ""), "html_url": who.get("html_url", "")}


@router.post("/api/github/account")
async def set_account(request: Request):
    body = await request.json()
    if body.get("clear"):
        hosting.save_config({})
        return {"ok": True, "cleared": True}
    token = str(body.get("token") or "").strip()
    if not token:
        return {"ok": False, "error": "token required"}
    r = await hosting.api("/user", token)
    if not r["ok"]:
        return {"ok": False, "error": hosting.error_text(r)}
    who = r["json"] or {}
    hosting.save_config({"token": token, "username": who.get("login", "")})
    return {"ok": True, "login": who.get("login", ""), "name": who.get("name", ""),
            "avatar": who.get("avatar_url", ""), "html_url": who.get("html_url", "")}


@router.get("/api/github/repos")
async def repos(q: str = ""):
    a = await hosting.auth()
    if not a["token"]:
        return {"ok": False, "error": "not connected — no GitHub credentials"}
    r = await hosting.api(
        "/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member",
        a["token"])
    if not r["ok"]:
        return {"ok": False, "error": hosting.error_text(r)}
    rows = []
    for x in (r["json"] if isinstance(r["json"], list) else []):
        rows.append({
            "full_name": x.get("full_name", ""), "name": x.get("name", ""),
            "private": bool(x.get("private")), "fork": bool(x.get("fork")),
            "clone_url": x.get("clone_url", ""),
            "default_branch": x.get("default_branch") or "main",
            "description": x.get("description") or "", "pushed_at": x.get("pushed_at") or "",
        })
    q = (q or "").lower()
    if q:
        rows = [x for x in rows if q in x["full_name"].lower()]
    return {"ok": True, "login": a["username"] or "", "count": len(rows), "repos": rows}


@router.get("/api/github/repo")
async def repo(repo: str = ""):
    parsed = hosting.parse_remote(repo) or hosting.parse_pair(repo)
    if not parsed:
        return {"ok": False, "error": "repo must look like owner/name"}
    a = await hosting.auth()
    r = await hosting.api(f"/repos/{parsed['owner']}/{parsed['name']}", a["token"])
    if not r["ok"]:
        return {"ok": True, "found": False, "status": r["status"],
                "error": "not found (or no access)" if r["status"] == 404
                         else hosting.error_text(r)}
    d = r["json"] or {}
    return {"ok": True, "found": True, "full_name": d.get("full_name", ""),
            "private": bool(d.get("private")),
            "visibility": d.get("visibility") or ("private" if d.get("private") else "public"),
            "fork": bool(d.get("fork")), "html_url": d.get("html_url", ""),
            "default_branch": d.get("default_branch") or "main",
            "description": d.get("description") or ""}


@router.post("/api/github/clone")
async def clone(request: Request):
    body = await request.json()
    parsed = hosting.parse_remote(body.get("repo")) or hosting.parse_pair(body.get("repo"))
    if not parsed:
        return {"ok": False, "error": "repo must look like owner/name"}
    root = str(body.get("dir") or "").strip() or vcs.resolve_workdir(None, None)
    if not Path(root).is_dir():
        return {"ok": False, "error": f"clone folder is not a directory: {root}"}
    target = str(Path(root) / parsed["name"])
    if Path(target).exists():
        return {"ok": False, "error": f"already exists: {target}", "target": target}
    url = f"https://github.com/{parsed['full_name']}.git"
    r = await asyncio.to_thread(vcs.run_argv, [vcs.BIN, "clone", url, target], root, None, 300)
    output = ((r.get("stdout") or "") + (r.get("stderr") or "")).strip()
    last = next((ln for ln in reversed(output.split("\n")) if ln.strip()), "")
    return {"ok": bool(r.get("ok")), "target": target, "dir": root,
            "repo": parsed["full_name"], "command": f"{vcs.BIN} clone {url} \"{target}\"",
            "output": output, "error": "" if r.get("ok") else (last or "clone failed")}


@router.post("/api/github/create")
async def create(request: Request):
    body = await request.json()
    a = await hosting.auth()
    if not a["token"]:
        return {"ok": False, "error": "not connected — no GitHub credentials"}
    name = re.sub(r"^-+|-+$", "", re.sub(r"[^\w.-]+", "-", str(body.get("name") or "").strip()))
    if not name:
        return {"ok": False, "error": "repository name required"}
    owner = str(body.get("owner") or "").strip()
    endpoint = f"/orgs/{owner}/repos" if owner else "/user/repos"
    if body.get("recreate"):
        login = a["username"]
        if not login:
            me = await hosting.api("/user", a["token"])
            login = (me.get("json") or {}).get("login", "")
        target = f"{owner}/{name}" if owner else (f"{login}/{name}" if login else "")
        if target:
            deleted = await hosting.delete_repo(target, a["token"])
            if not deleted["ok"]:
                return {"ok": False,
                        "error": f"could not delete existing {target}: {deleted.get('error')}"}
    payload = {"name": name, "private": bool(body.get("private")), "auto_init": False}
    if body.get("description"):
        payload["description"] = str(body["description"])[:350]
    r = await hosting.api(endpoint, a["token"], "POST", payload)
    if not r["ok"]:
        return {"ok": False, "error": hosting.error_text(r)}
    d = r["json"] or {}
    return {"ok": True, "full_name": d.get("full_name", ""), "clone_url": d.get("clone_url", ""),
            "ssh_url": d.get("ssh_url", ""), "private": bool(d.get("private")),
            "default_branch": d.get("default_branch") or "main",
            "html_url": d.get("html_url", "")}


@router.post("/api/github/delete")
async def delete(request: Request):
    body = await request.json()
    a = await hosting.auth()
    if not a["token"]:
        return {"ok": False, "error": "not connected — no GitHub credentials"}
    return await hosting.delete_repo(body.get("repo"), a["token"])

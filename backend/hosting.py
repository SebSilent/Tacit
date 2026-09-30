import asyncio
import re

import httpx

from . import config, vcs

API = "https://api.github.com"
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)
REMOTE_RE = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/\s]+@)?github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)
PAIR_RE = re.compile(r"^([\w.-]+)/([\w.-]+)$")


def load_config() -> dict:
    return config.read_json(config.GITHUB_FILE, {})


def save_config(value: dict) -> None:
    config.write_json(config.GITHUB_FILE, value)


def parse_remote(url: str):
    m = REMOTE_RE.match(str(url or "").strip())
    return {"owner": m.group(1), "name": m.group(2),
            "full_name": f"{m.group(1)}/{m.group(2)}"} if m else None


def parse_pair(value: str):
    m = PAIR_RE.match(str(value or "").strip())
    return {"owner": m.group(1), "name": m.group(2),
            "full_name": f"{m.group(1)}/{m.group(2)}"} if m else None


def error_text(r: dict) -> str:
    body = r.get("json") or {}
    return body.get("message") or body.get("error") or r.get("text") or \
        f"GitHub returned {r.get('status')}"


async def credential_fill(host: str = "github.com"):
    r = await asyncio.to_thread(
        vcs.run_argv, [vcs.BIN, "credential", "fill"], config.project_roots()[0],
        f"protocol=https\nhost={host}\n\n", 15)
    if not r.get("ok"):
        return None
    fields = {}
    for line in r["stdout"].split("\n"):
        i = line.find("=")
        if i > 0:
            fields[line[:i]] = line[i + 1:]
    if not fields.get("password"):
        return None
    return {"username": fields.get("username", ""), "token": fields["password"]}


async def cli_token() -> dict:
    root = config.project_roots()[0]
    r = await asyncio.to_thread(vcs.run_argv, [vcs.HOST_BIN, "auth", "token"], root, None, 15)
    token = (r.get("stdout") or "").strip() if r.get("ok") else ""
    if len(token) < 20 or len(token.split()) != 1:
        return {}
    username = ""
    u = await asyncio.to_thread(
        vcs.run_argv, [vcs.HOST_BIN, "api", "user", "--jq", ".login"], root, None, 15)
    if u.get("ok"):
        username = (u.get("stdout") or "").strip().strip('"')
    return {"token": token, "username": username}


async def auth() -> dict:
    cfg = load_config()
    if cfg.get("token"):
        return {"token": cfg["token"], "source": "config", "username": cfg.get("username", "")}
    cred = await credential_fill("github.com")
    if cred:
        return {"token": cred["token"], "source": "credential-manager",
                "username": cred["username"]}
    cli = await cli_token()
    if cli:
        return {"token": cli["token"], "source": "cli", "username": cli["username"]}
    return {"token": "", "source": "none", "username": ""}


async def api(pathname: str, token: str = "", method: str = "GET", body=None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Tacit",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.request(method, API + pathname, headers=headers, json=body)
    except httpx.HTTPError as e:
        return {"ok": False, "status": 0, "json": None, "text": str(e)}
    try:
        parsed = r.json()
    except Exception:
        parsed = None
    return {"ok": r.is_success, "status": r.status_code, "json": parsed, "text": r.text}


async def delete_repo(full: str, token: str) -> dict:
    pair = parse_pair(full)
    if not pair:
        return {"ok": False, "status": 0, "error": "repo must look like owner/name"}
    r = await api(f"/repos/{pair['owner']}/{pair['name']}", token, "DELETE")
    if r["ok"] or r["status"] == 404:
        return {"ok": True, "status": r["status"], "deleted": True}
    return {"ok": False, "status": r["status"], "error": error_text(r)}

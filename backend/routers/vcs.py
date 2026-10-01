from fastapi import APIRouter, Request

from .. import hosting, vcs

router = APIRouter()

IDENTITY_HELP = (
    "No commit identity is set, so this commit would be attributed to a placeholder. "
    "Connect GitHub in this panel and the account name is used, or fill in the "
    "Commit identity fields below.")


async def _commit_identity(cwd: str) -> tuple[dict | None, str]:
    """Who a commit should be attributed to, best available source first.

    An identity chosen in Tacit's settings wins, because selecting it here is
    the user saying so deliberately. After that: the repository's own config,
    then the environment, then the connected GitHub account. If none exists the
    commit is refused rather than misattributed to a placeholder.
    """
    for candidate in (vcs.saved_identity(), vcs.configured_identity(cwd),
                      vcs.env_identity()):
        if candidate:
            return candidate, ""
    try:
        acct = await hosting.account()
    except Exception:  # noqa: BLE001
        acct = {}
    github = vcs.github_identity(acct.get("login") or "", acct.get("name") or "")
    if github:
        return github, ""
    return None, IDENTITY_HELP


@router.get("/api/git/status")
async def status(workdir: str = "", sid: str = ""):
    cwd = vcs.resolve_workdir(workdir, sid)
    top = vcs.vc(cwd, ["rev-parse", "--show-toplevel"])
    if not top["ok"]:
        return {"ok": True, "workdir": cwd, "isRepo": False, "root": "",
                "status": None, "remotes": [], "error": top["stderr"].strip()}
    root = top["stdout"].strip()
    st = vcs.vc(root, ["status", "--porcelain=v1", "--branch"])
    return {"ok": True, "workdir": cwd, "isRepo": True, "root": root,
            "status": vcs.parse_status(st["stdout"]), "remotes": vcs.remotes(root),
            "command": st["command"]}


@router.get("/api/git/log")
async def log(workdir: str = "", sid: str = "", limit: int = 30):
    cwd = vcs.resolve_workdir(workdir, sid)
    count = min(200, max(1, int(limit or 30)))
    r = vcs.vc(cwd, ["log", f"--max-count={count}", "--date=iso",
                     "--pretty=format:%H\x1f%h\x1f%an\x1f%ad\x1f%s\x1f%d"])
    if not r["ok"]:
        return {"ok": False, "workdir": cwd, "commits": [],
                "error": r["stderr"].strip(), "command": r["command"]}
    commits = []
    for line in r["stdout"].split("\n"):
        if not line:
            continue
        parts = (line.split("\x1f") + ["", "", "", "", "", ""])[:6]
        commits.append({
            "hash": parts[0], "short": parts[1], "author": parts[2], "date": parts[3],
            "subject": parts[4],
            "refs": parts[5].replace("(", "").replace(")", "").lstrip(" ,").strip(),
        })
    return {"ok": True, "workdir": cwd, "commits": commits, "command": r["command"]}


@router.get("/api/git/branches")
async def branches(workdir: str = "", sid: str = ""):
    cwd = vcs.resolve_workdir(workdir, sid)
    cur = vcs.vc(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])
    local = vcs.vc(cwd, ["for-each-ref", "--format=%(refname:short)", "refs/heads"])
    remote = vcs.vc(cwd, ["for-each-ref", "--format=%(refname:short)", "refs/remotes"])

    def rows(r):
        return [s.strip() for s in r["stdout"].split("\n") if s.strip()] if r["ok"] else []

    return {"ok": local["ok"], "workdir": cwd,
            "current": cur["stdout"].strip() if cur["ok"] else "",
            "local": rows(local), "remote": rows(remote)}


@router.get("/api/git/diff")
async def diff(workdir: str = "", sid: str = "", staged: str = "", path: str = ""):
    cwd = vcs.resolve_workdir(workdir, sid)
    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    if path:
        args.extend(["--", path])
    r = vcs.vc(cwd, args)
    return {"ok": r["ok"], "workdir": cwd, "diff": r["stdout"],
            "error": r["stderr"].strip(), "command": r["command"]}


@router.get("/api/git/identity")
async def get_identity(workdir: str = "", sid: str = ""):
    """Every identity source, and which one a commit would actually use."""
    cwd = vcs.resolve_workdir(workdir, sid)
    repository = vcs.configured_identity(cwd)
    settings = vcs.saved_identity()
    environment = vcs.env_identity()
    try:
        acct = await hosting.account()
    except Exception:  # noqa: BLE001
        acct = {}
    github = vcs.github_identity(acct.get("login") or "", acct.get("name") or "")
    effective = settings or repository or environment or github
    return {"ok": True, "workdir": cwd, "effective": effective,
            "repository": repository, "settings": settings,
            "environment": environment, "github": github,
            "github_connected": bool(acct.get("login")),
            "github_username": acct.get("login") or "",
            "github_name": acct.get("name") or "",
            "needs_identity": effective is None, "help": IDENTITY_HELP}


@router.post("/api/git/identity")
async def set_identity(request: Request):
    body = await request.json()
    res = vcs.save_identity(body.get("name") or "", body.get("email") or "")
    return res if res.get("ok") else {"ok": False, "error": res.get("error")}


@router.post("/api/git/run")
async def run(request: Request):
    body = await request.json()
    cwd = vcs.resolve_workdir(body.get("workdir"), body.get("sid"))
    command = str(body.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "command required"}

    argv = vcs.split_command_line(command)
    binary = (argv[0] if argv else "").lower()
    if binary not in vcs.ALLOWED_BINS:
        return {"ok": False, "workdir": cwd, "command": command,
                "error": f"only git/gh commands are allowed here (got '{binary}')"}

    prefix = []
    if len(argv) > 1 and binary.startswith("git") and argv[1] == "commit":
        # A real identity is always used and a placeholder never is. When the
        # chosen one is the repository's own config there is nothing to inject.
        identity, why = await _commit_identity(cwd)
        if identity is None:
            return {"ok": False, "workdir": cwd, "command": command,
                    "error": why, "needs_identity": True}
        if identity.get("source") != "repository":
            prefix = ["-c", f"user.name={identity['name']}",
                      "-c", f"user.email={identity['email']}"]

    exe = vcs.BIN if binary.startswith("git") else vcs.HOST_BIN
    r = vcs.run_argv([exe, *prefix, *argv[1:]], cwd)
    out = r["stdout"] + (("\n" + r["stderr"]) if r["stderr"] else "")
    return {"ok": r["ok"], "workdir": cwd, "command": command, "output": out.strip()}

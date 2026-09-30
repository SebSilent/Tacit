import os
import re
import shlex
import subprocess
from pathlib import Path

from . import config

if os.name == "nt":
    BIN = "git.exe"
    HOST_BIN = "gh.exe"
else:
    BIN = "git"
    HOST_BIN = "gh"

ALLOWED_BINS = {"git", "git.exe", "gh", "gh.exe"}
TIMEOUT = int(os.environ.get("TACIT_VCS_TIMEOUT", "120"))


def mask_url(url: str) -> str:
    return re.sub(r"//[^/@\s]+@", "//***@", str(url))


def resolve_workdir(workdir: str | None = None, sid: str | None = None) -> str:
    from . import store
    candidate = str(workdir or "").strip()
    if not candidate and sid:
        rec = store.get(str(sid)) or {}
        candidate = str(rec.get("project") or "").strip()
    if not candidate:
        for row in store.list_sessions():
            p = str(row.get("project") or "").strip()
            if p and Path(p).is_dir():
                candidate = p
                break
    if not candidate:
        candidate = config.project_roots()[0]
    if Path(candidate).is_dir():
        return candidate
    return config.project_roots()[0]


def split_command_line(line: str) -> list[str]:
    try:
        return shlex.split(line, posix=False)
    except ValueError:
        return [line.strip()] if line.strip() else []


def run_argv(argv: list[str], cwd: str, stdin: str | None = None,
             timeout: int | None = None) -> dict:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env,
                           input=stdin, timeout=timeout or TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "stdout": "", "stderr": f"timed out after {timeout or TIMEOUT}s"}
    except FileNotFoundError:
        return {"ok": False, "code": -1, "stdout": "", "stderr": f"not found: {argv[0]}"}
    except Exception as e:
        return {"ok": False, "code": -1, "stdout": "", "stderr": str(e)}
    return {"ok": r.returncode == 0, "code": r.returncode,
            "stdout": r.stdout or "", "stderr": r.stderr or ""}


def vc(workdir: str, args: list[str]) -> dict:
    r = run_argv([BIN, *args], workdir)
    return {**r, "args": args, "command": " ".join([BIN, *args])}


def parse_status(out: str) -> dict:
    lines = str(out or "").split("\n")
    res = {"branch": "", "upstream": "", "ahead": 0, "behind": 0, "files": []}
    header = lines.pop(0) if lines else ""
    if header.startswith("## "):
        header = header[3:]
        detached = bool(re.match(r"^HEAD \(no branch\)", header))
        name = header
        bracket = header.find(" [")
        if bracket >= 0:
            meta = header[bracket + 2: header.rfind("]")]
            name = header[:bracket]
            a = re.search(r"ahead (\d+)", meta)
            b = re.search(r"behind (\d+)", meta)
            if a:
                res["ahead"] = int(a.group(1))
            if b:
                res["behind"] = int(b.group(1))
        noco = re.match(r"^No commits yet on (.+)$", name)
        if noco:
            res["branch"] = noco.group(1)
        elif detached:
            res["branch"] = "HEAD"
        else:
            dots = name.find("...")
            res["branch"] = name[:dots] if dots >= 0 else name
            res["upstream"] = name[dots + 3:] if dots >= 0 else ""
    for line in lines:
        if len(line) < 3 or not line.strip():
            continue
        x, y = line[0], line[1]
        p = line[3:]
        if p.startswith('"') and p.endswith('"'):
            p = p[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        res["files"].append({"path": p, "index": x, "worktree": y,
                             "staged": x not in (" ", "?"), "untracked": x == "?"})
    return res


def remotes(workdir: str) -> list[dict]:
    r = vc(workdir, ["remote", "-v"])
    if not r["ok"]:
        return []
    seen: dict[str, dict] = {}
    for line in r["stdout"].split("\n"):
        m = re.match(r"^(\S+)\s+(\S+)\s+\((fetch|push)\)$", line.strip())
        if m and (m.group(3) == "fetch" or m.group(1) not in seen):
            seen[m.group(1)] = {"name": m.group(1), "url": mask_url(m.group(2))}
    return list(seen.values())


def has_identity(workdir: str) -> bool:
    n = vc(workdir, ["config", "user.name"])
    e = vc(workdir, ["config", "user.email"])
    return bool(n["ok"] and n["stdout"].strip() and e["ok"] and e["stdout"].strip())

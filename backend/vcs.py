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

# Used when nothing else is chosen. Offered as the first option in the Git
# panel, and the only one that is never asked for: pick anything else once and
# it is remembered.
DEFAULT_IDENTITY = {"name": "Tacit", "email": "tacit@localhost", "source": "tacit"}


def default_identity() -> dict:
    return dict(DEFAULT_IDENTITY)

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


def configured_identity(workdir: str) -> dict | None:
    """The repository's own user.name / user.email, when both are set."""
    n = vc(workdir, ["config", "user.name"])
    e = vc(workdir, ["config", "user.email"])
    name = (n["stdout"] or "").strip() if n["ok"] else ""
    mail = (e["stdout"] or "").strip() if e["ok"] else ""
    if name and mail:
        return {"name": name, "email": mail, "source": "repository"}
    return None


def saved_identity() -> dict | None:
    """A name and email the user set in Tacit's own settings."""
    prefs = config.prefs()
    name = str(prefs.get("vcsName") or "").strip()
    mail = str(prefs.get("vcsEmail") or "").strip()
    if name and mail:
        return {"name": name, "email": mail, "source": "settings"}
    return None


def env_identity() -> dict | None:
    name = (os.environ.get("TACIT_VCS_NAME") or "").strip()
    mail = (os.environ.get("TACIT_VCS_EMAIL") or "").strip()
    if name and mail:
        return {"name": name, "email": mail, "source": "environment"}
    return None


def github_identity(username: str, display_name: str = "") -> dict | None:
    """Build a commit identity from the connected GitHub account.

    The login is what GitHub attributes a commit to, so it forms the noreply
    address. The display name, when the account has one, is what goes on the
    commit as the author's name.
    """
    login = str(username or "").strip()
    if not login:
        return None
    name = str(display_name or "").strip() or login
    return {"name": name, "email": f"{login}@users.noreply.github.com",
            "login": login, "source": "github"}


def save_identity(name: str, email: str) -> dict:
    name = str(name or "").strip()
    email = str(email or "").strip()
    if not name:
        return {"ok": False, "error": "a name is required"}
    if "@" not in email or " " in email:
        return {"ok": False, "error": "a valid email address is required"}
    config.save_prefs({"vcsName": name, "vcsEmail": email})
    return {"ok": True, "identity": {"name": name, "email": email, "source": "settings"}}

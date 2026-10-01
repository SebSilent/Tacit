"""Gateways: handing work to a program that is not the agent.

A gateway is a configured command that takes a task and answers. It is a hand-off,
not a sandbox and not a tool: Tacit gives it work, reads the answer, and records
what happened. Nothing about the gateway's internals is assumed.

One is built in, `none`, which is the default. Others are defined by the user as a
command, some arguments and a timeout, and are off until enabled. The exact command
is shown before it ever runs.

Tacit ships no gateway of its own and depends on no other harness. Anything here is
a command the user configured themselves.
"""

from __future__ import annotations

import re
import time

from . import audit, config, providers

# Built in and always present. Everything else is a command the user defines.
BUILTIN = ("none",)
MAX_OUTPUT = 20000
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$")


def _user_rows() -> list[dict]:
    data = providers.load().get("gateways") or {}
    rows = data.get("user") if isinstance(data, dict) else []
    return [dict(r) for r in rows] if isinstance(rows, list) else []


def _save_user(rows: list[dict]) -> None:
    providers.save({"gateways": {"user": rows}})


def describe() -> list[dict]:
    """Every gateway, built in and user defined, with what it may do."""
    rows = [
        {"id": "none", "name": "None", "kind": "gateway", "status": "built-in",
         "summary": "No external integrations.", "network": False, "trust": "local",
         "available": True, "implemented": True, "enabled": True, "command": ""},
    ]
    for row in _user_rows():
        gid = str(row.get("id") or "")
        rows.append({
            "id": gid, "name": row.get("name") or gid, "kind": "gateway", "status": "optional",
            "summary": row.get("description") or "a command you configured",
            "network": bool(row.get("network")), "trust": row.get("trust") or "unverified",
            "available": bool(gid) and bool(row.get("command")),
            "implemented": True, "enabled": bool(row.get("enabled", True)),
            "command": _command_for(row, "<task>"),
        })
    return rows


def get(gateway_id: str) -> dict | None:
    return next((r for r in describe() if r["id"] == gateway_id), None)


def _command_for(row: dict, task: str) -> str:
    """The exact argv, as it would be run. Shown before it runs."""
    argv = _argv(row, task)
    return " ".join(f'"{a}"' if " " in a else a for a in argv)


def _argv(row: dict, task: str) -> list[str]:
    command = str(row.get("command") or "").strip()
    args = [str(a) for a in (row.get("args") or [])]
    placed = False
    out = []
    for arg in args:
        if "{task}" in arg:
            out.append(arg.replace("{task}", task))
            placed = True
        else:
            out.append(arg)
    if not placed and task:
        out.append(task)
    return [command, *out]


def add(row: dict) -> dict:
    gid = str(row.get("id") or "").strip().lower()
    if not SAFE_ID.match(gid):
        return {"ok": False, "error": "an id of letters, digits, dot, dash or underscore"}
    if gid in BUILTIN:
        return {"ok": False, "error": f"'{gid}' is a built-in gateway"}
    if not str(row.get("command") or "").strip():
        return {"ok": False, "error": "a command is required"}
    rows = [r for r in _user_rows() if r.get("id") != gid]
    rows.append({
        "id": gid,
        "name": str(row.get("name") or gid).strip()[:60],
        "command": str(row["command"]).strip(),
        "args": [str(a) for a in (row.get("args") or [])][:40],
        "description": str(row.get("description") or "").strip()[:200],
        "timeout": max(1, min(int(row.get("timeout") or 300), 3600)),
        "network": bool(row.get("network")),
        "trust": str(row.get("trust") or "unverified"),
        "enabled": bool(row.get("enabled", True)),
    })
    _save_user(rows)
    audit.record("gateway_added", backend=gid, mode=gid, status="ok",
                 command=_command_for(rows[-1], "<task>"))
    return {"ok": True, "gateway": get(gid)}


def remove(gateway_id: str) -> dict:
    if gateway_id in BUILTIN:
        return {"ok": False, "error": f"'{gateway_id}' is built in and cannot be removed"}
    rows = _user_rows()
    kept = [r for r in rows if r.get("id") != gateway_id]
    if len(kept) == len(rows):
        return {"ok": False, "error": f"no gateway '{gateway_id}'"}
    _save_user(kept)
    audit.record("gateway_removed", backend=gateway_id, status="ok")
    return {"ok": True, "removed": gateway_id}


def selected() -> str:
    return str((providers.load().get("gateway") or {}).get("id") or "none")


def select(gateway_id: str) -> dict:
    if gateway_id == "none":
        providers.save({"gateway": {"id": "none"}})
        return {"ok": True, "gateway": "none"}
    row = get(gateway_id)
    if row is None:
        return {"ok": False, "error": f"no gateway '{gateway_id}'"}
    if not row["available"]:
        return {"ok": False, "error": f"'{gateway_id}' is not available: "
                                      f"{row.get('reason') or 'unavailable'}"}
    providers.save({"gateway": {"id": gateway_id}})
    return {"ok": True, "gateway": gateway_id}


def run(gateway_id: str, task: str, cwd: str | None = None, timeout: int | None = None,
        session: str = "") -> dict:
    text = str(task or "").strip()
    if not text:
        return {"ok": False, "error": "a task is required"}
    if gateway_id == "none":
        return {"ok": False, "error": "no gateway is selected"}

    row = next((r for r in _user_rows() if r.get("id") == gateway_id), None)
    if row is None:
        return {"ok": False, "error": f"no gateway '{gateway_id}'"}
    if not row.get("enabled", True):
        return {"ok": False, "error": f"gateway '{gateway_id}' is disabled"}
    if selected() != gateway_id:
        return {"ok": False, "error": f"'{gateway_id}' is not the selected gateway. "
                                      f"Select it in Settings > Capabilities first."}

    limit = max(1, min(int(timeout or row.get("timeout") or 300), 3600))
    argv = _argv(row, text)
    workdir = str(cwd or config.USER_HOME)
    started = time.time()
    import subprocess

    try:
        r = subprocess.run(argv, cwd=workdir, capture_output=True, text=True, timeout=limit)
        out = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
        code = r.returncode
    except subprocess.TimeoutExpired:
        audit.record("gateway_run", session=session, backend=gateway_id, tool="gateway",
                     status="timeout", timeout=limit, task=text[:200])
        return {"ok": False, "error": f"'{gateway_id}' timed out after {limit}s",
                "backend": gateway_id}
    except FileNotFoundError:
        return {"ok": False, "error": f"the command for '{gateway_id}' was not found"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    audit.record("gateway_run", session=session, backend=gateway_id, tool="gateway",
                 status="ok" if code == 0 else f"exit {code}", code=code, timeout=limit,
                 task=text[:200], duration_ms=int((time.time() - started) * 1000),
                 output_chars=len(out))
    return {"ok": code == 0, "backend": gateway_id, "code": code,
            "output": out[:MAX_OUTPUT], "command": _command_for(row, text),
            "duration_ms": int((time.time() - started) * 1000),
            "error": "" if code == 0 else out[:400]}

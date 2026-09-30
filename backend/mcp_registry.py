"""MCP server registry: configuration, lifecycle, discovery, policy, audit.

This is where *policy* lives. The client in ``mcp_client`` only speaks protocol;
here we decide which servers may run, which tools may be called, what gets
injected into the prompt, and what gets written to the audit log.

Context discipline is the whole point: a connected server's tools are
**discovered and stored, never auto-injected**. The agent sees only the compact
MCP helper tools until it searches for something and explicitly activates it
for a bounded number of turns. ``settings.direct_mode`` (off by default) is the
escape hatch for users who want the classic "dump every tool in" behaviour.

Config:  ~/.tacit/mcp.json
Audit:   ~/.tacit/mcp_audit.jsonl  (append-only, secrets redacted)
"""

from __future__ import annotations

import json
import re
import threading
import time

from . import config, tokens
from .mcp_client import (HttpMcpClient, McpError, StdioMcpClient, looks_secret, mask_url,
                         redact, redact_env)

_LOCK = threading.RLock()
HTTP_TRANSPORTS = {"http", "https", "sse", "streamable", "streamable-http", "streamable_http"}
_RUNTIME: dict[str, dict] = {}
_ACTIVE: dict[str, dict] = {}      # tool key -> {"expires_turn": int, "activated_at": float}
_TURN = {"n": 0}
AUDIT_LIMIT = 500

DANGER_WORDS = ("delete", "remove", "drop", "destroy", "kill", "exec", "shell",
                "write", "create", "push", "deploy", "publish", "rm ", "unlink",
                "truncate", "reset", "overwrite", "send", "post", "install")

DEFAULT_SETTINGS = {
    "direct_mode": False,      # inject every enabled tool — OFF by default
    "default_ttl_turns": 3,    # how long an activated tool stays visible
    "auto_start": False,       # do not launch servers until asked
    "max_restarts": 3,
    "search_limit": 8,
}


# ── config ─────────────────────────────────────────────────────────────────
def load() -> dict:
    data = config.read_json(config.MCP_FILE, {})
    if not isinstance(data, dict):
        data = {}
    servers = data.get("servers")
    if not isinstance(servers, dict):
        servers = {}
    settings = {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
    return {"settings": settings, "servers": servers}


def save(data: dict) -> None:
    config.write_json(config.MCP_FILE, data)


def settings() -> dict:
    return load()["settings"]


def save_settings(patch: dict) -> dict:
    data = load()
    merged = {**data["settings"]}
    for key, value in (patch or {}).items():
        if key in DEFAULT_SETTINGS:
            merged[key] = value
    data["settings"] = merged
    save(data)
    return merged


def _sanitize_id(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    return clean.lower() or "server"


def _blank_server(spec: dict) -> dict:
    sid = _sanitize_id(spec.get("id") or spec.get("name") or "server")
    return {
        "id": sid,
        "name": spec.get("name") or sid,
        "command": str(spec.get("command") or "").strip(),
        "args": list(spec.get("args") or []),
        "env": dict(spec.get("env") or {}),
        "cwd": str(spec.get("cwd") or "").strip(),
        "enabled": bool(spec.get("enabled", False)),
        "transport": (spec.get("transport") or "stdio"),
        "headers": {str(k): str(v) for k, v in (spec.get("headers") or {}).items()},
        "allow_tools": list(spec.get("allow_tools") or []),
        "deny_tools": list(spec.get("deny_tools") or []),
        "require_confirmation": bool(spec.get("require_confirmation", True)),
        "auto_activate": bool(spec.get("auto_activate", False)),
        "token_budget": int(spec.get("token_budget") or 0),
        "timeout": int(spec.get("timeout") or 30),
        "pinned": list(spec.get("pinned") or []),
        "tools": list(spec.get("tools") or []),   # discovered schemas, cached
        "plugin": spec.get("plugin") or "",
        "last_error": "",
        "added_at": spec.get("added_at") or round(time.time(), 3),
    }


# ── audit ──────────────────────────────────────────────────────────────────
def audit(event: str, **fields) -> None:
    row = {"ts": round(time.time(), 3), "event": event}
    for key, value in fields.items():
        if key in ("env", "arguments") and isinstance(value, dict):
            value = redact_env(value) if key == "env" else _summarize_args(value)
        row[key] = value
    try:
        with open(config.HOME / "mcp_audit.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _summarize_args(args: dict, limit: int = 160) -> str:
    try:
        text = json.dumps(args or {}, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        text = str(args)
    return text if len(text) <= limit else text[:limit] + "…"


def recent_audit(limit: int = 100) -> list[dict]:
    path = config.HOME / "mcp_audit.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for line in lines[-max(1, limit):]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


# ── lifecycle ──────────────────────────────────────────────────────────────
def _runtime(sid: str) -> dict:
    return _RUNTIME.setdefault(sid, {
        "client": None, "state": "stopped", "error": "", "restarts": 0,
        "next_retry": 0.0, "last_started": 0.0, "logs": [],
    })


def _log_line(sid: str, line: str) -> None:
    row = _runtime(sid)
    row["logs"].append({"ts": round(time.time(), 3), "line": line})
    del row["logs"][:-200]


def _spec(sid: str) -> dict | None:
    return load()["servers"].get(sid)


def _new_client(spec: dict, sid: str):
    """Pick the transport implementation. stdio spawns a subprocess; the HTTP
    transports POST to a single endpoint."""
    transport = str(spec.get("transport") or "stdio").lower()
    log = lambda line, s=sid: _log_line(s, line)
    if transport in HTTP_TRANSPORTS:
        return HttpMcpClient(spec, on_log=log)
    return StdioMcpClient(spec, on_log=log)


def status(sid: str) -> dict:
    row = _runtime(sid)
    client = row.get("client")
    if client is not None and not client.alive() and row["state"] == "running":
        row["state"] = "stopped"
        row["error"] = "server exited"
    return {"state": row["state"], "error": row["error"], "restarts": row["restarts"],
            "alive": bool(client and client.alive())}


def start(sid: str) -> dict:
    spec = _spec(sid)
    if not spec:
        return {"ok": False, "error": f"no MCP server '{sid}'"}
    if not spec.get("enabled"):
        return {"ok": False, "error": "server is disabled — enable it first"}
    if not spec.get("command"):
        return {"ok": False, "error": "server has no command"}
    transport = str(spec.get("transport") or "stdio").lower()
    if transport != "stdio" and transport not in HTTP_TRANSPORTS:
        return {"ok": False, "error": f"unsupported transport '{transport}'"}
    if transport in HTTP_TRANSPORTS and not str(spec.get("command") or "").startswith(("http://", "https://")):
        return {"ok": False, "error": "an HTTP server needs an http(s) endpoint URL as its command"}

    row = _runtime(sid)
    if row.get("client") is not None and row["client"].alive():
        return {"ok": True, "state": "running", "server_info": row["client"].server_info}

    client = _new_client(spec, sid)
    try:
        client.start()
        info = client.initialize(timeout=min(float(spec.get("timeout") or 30), 30))
        client.list_tools()
    except McpError as exc:
        client.stop()
        row.update({"client": None, "state": "error", "error": str(exc)})
        row["restarts"] += 1
        row["next_retry"] = time.time() + min(60, 2 ** min(row["restarts"], 6))
        audit("server_error", server=sid, error=str(exc),
              command=mask_url(spec.get("command")), env=spec.get("env"))
        return {"ok": False, "error": str(exc)}
    row.update({"client": client, "state": "running", "error": "",
                "last_started": time.time(), "next_retry": 0.0})
    audit("server_started", server=sid, command=mask_url(spec.get("command")),
          args=spec.get("args"), env=spec.get("env"),
          tools=len(client.tools), server_info=client.server_info)
    discover(sid)
    return {"ok": True, "state": "running", "server_info": client.server_info,
            "tools": len(client.tools)}


def stop(sid: str) -> dict:
    row = _runtime(sid)
    client = row.get("client")
    if client is not None:
        client.stop()
    row["client"] = None
    row["state"] = "stopped"
    audit("server_stopped", server=sid)
    return {"ok": True, "state": "stopped"}


def ensure_started(sid: str) -> dict:
    """Start if needed, honouring backoff so a broken server cannot spin."""
    spec = _spec(sid)
    if not spec or not spec.get("enabled"):
        return {"ok": False, "error": "not enabled"}
    row = _runtime(sid)
    if row.get("client") is not None and row["client"].alive():
        return {"ok": True, "state": "running"}
    if row.get("next_retry", 0) > time.time():
        return {"ok": False, "error": f"backing off until {int(row['next_retry'])}"}
    return start(sid)


def discover(sid: str) -> list[dict]:
    """Refresh the cached tool schemas for a server (does not inject them)."""
    row = _runtime(sid)
    client = row.get("client")
    if client is None or not client.alive():
        return []
    try:
        found = client.list_tools()
    except McpError as exc:
        row["error"] = str(exc)
        return []
    data = load()
    spec = data["servers"].get(sid)
    if spec is not None:
        spec["tools"] = found
        save(data)
    for tool in found:
        audit("tool_discovered", server=sid, tool=tool["name"],
              tokens=tokens.estimate_tool_schema_tokens(to_openai_schema(sid, tool)))
    return found


# ── tool surface ───────────────────────────────────────────────────────────
def _safe_fn_name(sid: str, tool: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_]+", "_", f"mcp__{sid}__{tool}")
    return clean[:64]


def to_openai_schema(sid: str, tool: dict) -> dict:
    return {"type": "function", "function": {
        "name": _safe_fn_name(sid, tool["name"]),
        "description": (tool.get("description") or f"{tool['name']} (via MCP server {sid})")[:1024],
        "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
    }}


def danger_level(tool: dict) -> str:
    hay = f"{tool.get('name','')} {tool.get('description','')}".lower()
    hits = sum(1 for word in DANGER_WORDS if word in hay)
    if hits >= 3:
        return "high"
    if hits >= 1:
        return "medium"
    return "low"


def tool_key(sid: str, name: str) -> str:
    return f"{sid}:{name}"


def _tool_entry(sid: str, spec: dict, tool: dict) -> dict:
    key = tool_key(sid, tool["name"])
    schema = to_openai_schema(sid, tool)
    return {
        "key": key,
        "server_id": sid,
        "server_name": spec.get("name") or sid,
        "name": tool["name"],
        "fn_name": schema["function"]["name"],
        "description": tool.get("description") or "",
        "tokens": tokens.estimate_tool_schema_tokens(schema),
        "danger": danger_level(tool),
        "allowed": _allowed(spec, tool["name"]),
        "pinned": tool["name"] in (spec.get("pinned") or []),
        "auto_activate": bool(spec.get("auto_activate")),
        "last_used": (spec.get("tool_meta") or {}).get(tool["name"], {}).get("last_used"),
        "use_count": (spec.get("tool_meta") or {}).get(tool["name"], {}).get("count", 0),
        "schema": schema,
    }


def _allowed(spec: dict, name: str) -> bool:
    allow = spec.get("allow_tools") or []
    deny = spec.get("deny_tools") or []
    if allow and name not in allow:
        return False
    return name not in deny


def all_tools() -> list[dict]:
    data = load()
    out = []
    for sid, spec in data["servers"].items():
        for tool in spec.get("tools") or []:
            out.append(_tool_entry(sid, spec, tool))
    return out


def _score(query: str, entry: dict) -> int:
    q = str(query or "").lower().strip()
    if not q:
        return 0
    score = 0
    name = entry["name"].lower()
    desc = entry["description"].lower()
    for term in re.split(r"[^a-z0-9_]+", q):
        if not term:
            continue
        if term == name:
            score += 10
        elif term in name:
            score += 6
        if term in desc:
            score += 2
        if term in entry["server_name"].lower():
            score += 1
    return score


def search(query: str, limit: int = 8) -> list[dict]:
    entries = [e for e in all_tools() if e["allowed"]]
    scored = [(e, _score(query, e)) for e in entries]
    hits = [e for e, s in sorted(scored, key=lambda p: -p[1]) if s > 0]
    return (hits or sorted(entries, key=lambda e: e["name"]))[:max(1, min(int(limit or 8), 50))]


# ── lazy activation ────────────────────────────────────────────────────────
def next_turn() -> int:
    with _LOCK:
        _TURN["n"] += 1
        return _TURN["n"]


def current_turn() -> int:
    return _TURN["n"]


def activate(keys: list[str], ttl_turns: int | None = None) -> dict:
    ttl = int(ttl_turns or settings().get("default_ttl_turns") or 3)
    known = {e["key"]: e for e in all_tools()}
    active, missing = [], []
    expire_at = current_turn() + max(1, ttl)
    for key in keys or []:
        entry = known.get(key) or known.get(str(key))
        if entry is None:
            # accept a bare tool name or fn_name too
            entry = next((e for e in known.values()
                          if e["name"] == key or e["fn_name"] == key), None)
        if entry is None:
            missing.append(key)
            continue
        _ACTIVE[entry["key"]] = {"expires_turn": expire_at, "activated_at": time.time()}
        active.append(entry["key"])
        audit("tool_activated", server=entry["server_id"], tool=entry["name"],
              ttl_turns=ttl, tokens=entry["tokens"])
    return {"ok": True, "activated": active, "unknown": missing, "expires_turn": expire_at}


def deactivate(keys: list[str]) -> dict:
    removed = []
    for key in keys or []:
        if _ACTIVE.pop(key, None) is not None:
            removed.append(key)
    return {"ok": True, "deactivated": removed}


def prune() -> None:
    now = current_turn()
    for key in [k for k, v in _ACTIVE.items() if v["expires_turn"] < now]:
        entry = next((e for e in all_tools() if e["key"] == key), None)
        _ACTIVE.pop(key, None)
        if entry:
            audit("tool_deactivated", server=entry["server_id"], tool=entry["name"],
                  reason="ttl expired")


def active_tools() -> list[dict]:
    """Schemas to inject right now: explicitly activated, plus pinned."""
    prune()
    out = []
    for entry in all_tools():
        if entry["key"] in _ACTIVE or entry["pinned"]:
            out.append(entry)
    return out


def injection_report() -> dict:
    """What lazy loading saved: every discovered schema minus what we inject."""
    data = load()
    discovered = 0
    injected = 0
    for sid, spec in data["servers"].items():
        for tool in spec.get("tools") or []:
            cost = tokens.estimate_tool_schema_tokens(to_openai_schema(sid, tool))
            discovered += cost
    for entry in active_tools():
        injected += entry["tokens"]
    direct = settings().get("direct_mode")
    enabled_total = 0
    for sid, spec in data["servers"].items():
        if spec.get("enabled"):
            for tool in spec.get("tools") or []:
                enabled_total += tokens.estimate_tool_schema_tokens(to_openai_schema(sid, tool))
    return {
        "discovered_tokens": discovered if not direct else enabled_total,
        "injected_tokens": enabled_total if direct else injected,
        "saved_tokens": 0 if direct else max(0, discovered - injected),
        "direct_mode": bool(direct),
        "active": [e["key"] for e in active_tools()],
        "exact": tokens.exact(),
    }


def schemas_for_prompt() -> list[dict]:
    """The MCP tool schemas to append to the request `tools` array."""
    if settings().get("direct_mode"):
        out = []
        data = load()
        for sid, spec in data["servers"].items():
            if not spec.get("enabled"):
                continue
            for tool in spec.get("tools") or []:
                if _allowed(spec, tool["name"]):
                    out.append(to_openai_schema(sid, tool))
        return out
    return [e["schema"] for e in active_tools()]


# ── calling ────────────────────────────────────────────────────────────────
def call(sid: str, tool_name: str, args: dict | None = None, *, confirmed: bool = False) -> dict:
    spec = _spec(sid)
    if not spec:
        return {"ok": False, "error": f"no MCP server '{sid}'"}
    if not _allowed(spec, tool_name):
        audit("tool_blocked", server=sid, tool=tool_name, reason="policy")
        return {"ok": False, "error": f"tool '{tool_name}' is blocked by this server's policy"}
    entry = next((e for e in all_tools() if e["server_id"] == sid and e["name"] == tool_name), None)
    if entry and entry["danger"] != "low" and spec.get("require_confirmation") and not confirmed:
        audit("tool_confirmation_required", server=sid, tool=tool_name, danger=entry["danger"])
        return {"ok": False, "needs_confirmation": True, "danger": entry["danger"],
                "error": (f"'{tool_name}' looks {entry['danger']}-risk; "
                          f"re-call with confirm=true to proceed")}
    started = ensure_started(sid)
    if not started.get("ok"):
        return {"ok": False, "error": started.get("error") or "server unavailable"}
    row = _runtime(sid)
    client = row["client"]
    began = time.time()
    try:
        text = client.call_tool(tool_name, args or {}, timeout=float(spec.get("timeout") or 30))
    except McpError as exc:
        audit("tool_error", server=sid, tool=tool_name, error=str(exc),
              ms=int((time.time() - began) * 1000))
        return {"ok": False, "error": str(exc)}
    # usage counters for the UI
    data = load()
    stored = data["servers"].get(sid)
    if stored is not None:
        meta = stored.setdefault("tool_meta", {})
        slot = meta.setdefault(tool_name, {"count": 0, "last_used": None})
        slot["count"] = int(slot.get("count") or 0) + 1
        slot["last_used"] = round(time.time(), 3)
        save(data)
    out_tokens = tokens.estimate_tokens(text)
    audit("tool_invoked", server=sid, tool=tool_name, arguments=args or {},
          status="ok", ms=int((time.time() - began) * 1000), result_tokens=out_tokens)
    return {"ok": True, "result": text, "tokens": out_tokens,
            "ms": int((time.time() - began) * 1000)}


# ── CRUD for the UI ────────────────────────────────────────────────────────
def list_servers() -> list[dict]:
    data = load()
    out = []
    for sid, spec in data["servers"].items():
        st = status(sid)
        tools = spec.get("tools") or []
        total = sum(tokens.estimate_tool_schema_tokens(to_openai_schema(sid, t)) for t in tools)
        out.append({
            "id": sid,
            "name": spec.get("name") or sid,
            "command": mask_url(spec.get("command")),
            "args": spec.get("args") or [],
            "env_keys": sorted((spec.get("env") or {}).keys()),
            "env": redact_env(spec.get("env") or {}),
            "header_keys": sorted((spec.get("headers") or {}).keys()),
            "headers": redact_env(spec.get("headers") or {}),
            "cwd": spec.get("cwd") or "",
            "enabled": bool(spec.get("enabled")),
            "transport": spec.get("transport") or "stdio",
            "allow_tools": spec.get("allow_tools") or [],
            "deny_tools": spec.get("deny_tools") or [],
            "require_confirmation": bool(spec.get("require_confirmation", True)),
            "auto_activate": bool(spec.get("auto_activate")),
            "token_budget": int(spec.get("token_budget") or 0),
            "timeout": int(spec.get("timeout") or 30),
            "plugin": spec.get("plugin") or "",
            "state": st["state"],
            "alive": st["alive"],
            "error": st["error"] or spec.get("last_error") or "",
            "restarts": st["restarts"],
            "tool_count": len(tools),
            "tools_tokens": total,
        })
    return out


def add_server(spec: dict) -> dict:
    if not str(spec.get("command") or "").strip():
        return {"ok": False, "error": "a command is required"}
    data = load()
    entry = _blank_server(spec)
    if entry["id"] in data["servers"] and not spec.get("force"):
        return {"ok": False, "error": f"server '{entry['id']}' already exists"}
    data["servers"][entry["id"]] = entry
    save(data)
    audit("server_added", server=entry["id"], command=entry["command"],
          args=entry["args"], env=entry["env"], enabled=entry["enabled"])
    return {"ok": True, "server": list_servers_for(entry["id"])}


def list_servers_for(sid: str) -> dict | None:
    return next((s for s in list_servers() if s["id"] == sid), None)


def update_server(sid: str, patch: dict) -> dict:
    data = load()
    spec = data["servers"].get(sid)
    if not spec:
        return {"ok": False, "error": f"no MCP server '{sid}'"}
    for key in ("name", "command", "cwd", "transport"):
        if patch.get(key) is not None:
            spec[key] = str(patch[key])
    for key in ("args", "allow_tools", "deny_tools", "pinned"):
        if patch.get(key) is not None:
            spec[key] = list(patch[key])
    if patch.get("env") is not None:
        spec["env"] = {str(k): str(v) for k, v in (patch["env"] or {}).items()}
    if patch.get("headers") is not None:
        spec["headers"] = {str(k): str(v) for k, v in (patch["headers"] or {}).items()}
    for key in ("enabled", "require_confirmation", "auto_activate"):
        if patch.get(key) is not None:
            spec[key] = bool(patch[key])
    for key in ("token_budget", "timeout"):
        if patch.get(key) is not None:
            spec[key] = int(patch[key])
    save(data)
    return {"ok": True, "server": list_servers_for(sid)}


def remove_server(sid: str) -> dict:
    data = load()
    if sid not in data["servers"]:
        return {"ok": False, "error": f"no MCP server '{sid}'"}
    stop(sid)
    del data["servers"][sid]
    save(data)
    audit("server_removed", server=sid)
    return {"ok": True}


def set_pinned(sid: str, tool_name: str, pinned: bool) -> dict:
    data = load()
    spec = data["servers"].get(sid)
    if not spec:
        return {"ok": False, "error": f"no MCP server '{sid}'"}
    current = [t for t in (spec.get("pinned") or []) if t != tool_name]
    if pinned:
        current.append(tool_name)
    spec["pinned"] = current
    save(data)
    entry = next((e for e in all_tools() if e["server_id"] == sid and e["name"] == tool_name), None)
    audit("tool_pinned" if pinned else "tool_unpinned", server=sid, tool=tool_name,
          tokens=(entry or {}).get("tokens"))
    return {"ok": True, "pinned": pinned, "tool": tool_name,
            "token_cost": (entry or {}).get("tokens", 0)}


def stop_all() -> None:
    for sid in list(_RUNTIME.keys()):
        try:
            stop(sid)
        except Exception:  # noqa: BLE001
            pass


def start_enabled() -> list[dict]:
    """Start servers that the user marked enabled (never auto-runs others)."""
    out = []
    for spec in load()["servers"].values():
        if spec.get("enabled"):
            out.append({"id": spec["id"], **start(spec["id"])})
    return out

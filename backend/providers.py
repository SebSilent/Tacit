"""The capability registry: what exists, what is available, what it may do.

This module declares and detects. It never acts. Nothing here runs a command,
opens a socket or touches a file beyond asking whether a binary exists. Every
capability in Tacit is described in one place, so the interface, the audit ledger
and the agent all agree about what is switched on.

An optional backend that is not installed is reported as unavailable with a
reason. It is never silently missing and never silently substituted.
"""

from __future__ import annotations

from . import config, tokens

KINDS = ("sandbox", "memory", "learning", "gateway", "tool")
STATUSES = ("built-in", "optional", "external")
DISK_LEVELS = ("none", "read", "write", "full")
TRUST_LEVELS = ("local", "verified", "untrusted")

# The safe default for every capability. Nothing optional is on.
DEFAULTS = {
    "profile": "default",
    "sandbox": {"backend": "none", "network": False, "timeout": 180,
                "memory_mb": 0, "cpu_seconds": 0, "readonly_project": False},
    "memory": {"mode": "off", "budget": 120, "ttl_days": 0, "reinforce": True},
    "learning": {"mode": "propose"},
    "analyzer": {"enabled": True, "interval_s": 120},
    # Per-turn guidance blocks. On by default: it changes how the agent behaves
    # without touching the standing prompt or adding a model call.
    "guidance": {"enabled": True},
    "gateway": {"id": "none", "timeout": 600},
    # user-defined gateways live alongside the selection, so they survive a save
    "gateways": {"user": []},
}


def _p(pid, name, kind, status, *, summary, network=False, disk="none", trust="local",
       tokens_cost=0, permissions=(), install="", available=True, reason="",
       implemented=True, blocked=""):
    return {
        "id": pid,
        "name": name,
        "kind": kind,
        "status": status,
        "summary": summary,
        "permissions": list(permissions),
        "network": bool(network),
        "disk": disk,
        "trust": trust,
        "tokens": int(tokens_cost),
        "install": install,
        "available": bool(available),
        "reason": reason,
        # available means the dependency is present; implemented means Tacit has
        # an adapter for it. Both are needed before a backend can be selected.
        "implemented": bool(implemented),
        # why there is no adapter, when there is not one
        "blocked": blocked,
    }


# ── what exists ────────────────────────────────────────────────────────────
# Declared statically so the interface can show a backend before it is built.
# `available` is filled in by detect().

_BUILTIN = [
    # sandbox -------------------------------------------------------------
    _p("none", "None", "sandbox", "built-in",
       summary="Commands run directly, with no isolation. Trusted local use only.",
       network=True, disk="full", trust="local"),
    _p("tacit-micro", "Tacit isolation", "sandbox", "built-in",
       summary="Tacit's own isolation. Uses the strongest primitive this platform offers: "
               "bubblewrap on Linux, sandbox-exec on macOS, and honest reporting where "
               "neither exists. Read-only project where possible, temporary overlay for "
               "writes, network off by default, limits and a timeout, and a report of "
               "what actually changed.",
       network=False, disk="write", trust="local", permissions=["filesystem", "subprocess"]),
    _p("container", "Container", "sandbox", "optional",
       summary="Docker or Podman. The only backend that enforces real isolation on "
               "Windows: a network namespace, a read-only project bind, memory and "
               "CPU ceilings, a PID limit, a private /tmp and no privilege "
               "escalation — all applied by the runtime, not approximated. The "
               "project is bind-mounted, so the change report and snapshots "
               "describe your real files. Nothing is downloaded for you.",
       network=False, disk="write", trust="verified", permissions=["subprocess"],
       install="install Docker or Podman and make sure it is on PATH"),

    # memory --------------------------------------------------------------
    _p("off", "Off", "memory", "built-in",
       summary="No persistent memory beyond this session."),
    _p("explicit", "Explicit", "memory", "built-in",
       summary="Only knowledge and skills you wrote yourself. Nothing is learned.",
       disk="write"),
    _p("jit", "Just in time", "memory", "built-in",
       summary="Retrieve the smallest relevant memory only when a turn needs it, "
               "within a token budget.", disk="write"),
    _p("full", "Everything Tacit has", "memory", "built-in",
       summary="Every memory feature Tacit implements: explicit notes plus "
               "just-in-time retrieval, still budgeted and auditable.",
       disk="write"),

    # learning ------------------------------------------------------------
    _p("learn-off", "Off", "learning", "built-in", summary="No learning at all."),
    _p("propose", "Propose", "learning", "built-in",
       summary="Tacit suggests skills, rules and corrections. Nothing applies without "
               "your approval.", disk="write"),
    _p("auto-low-risk", "Auto, low risk", "learning", "built-in",
       summary="Apply a proposal on its own only when it is a preference, it reads as "
               "low risk, and it is at least medium confidence.",
       disk="write"),
    _p("auto", "Auto", "learning", "built-in",
       summary="Apply every proposal on its own. Only choose this deliberately.",
       disk="write"),

    # gateway -------------------------------------------------------------
    _p("none", "None", "gateway", "built-in", summary="No external integrations."),
]

# What Tacit implements itself. There is no external harness in this list, and
# nothing here is detected or required.
STANDALONE = ("none", "tacit-micro", "off", "explicit", "jit", "full")


def _container_available() -> tuple[bool, str]:
    """Only asked when the user selects container mode. Never required.

    The reason names the group, so the interface can point at the exact install
    command from the dependency list rather than inventing its own wording.

    Asking the sandbox layer rather than repeating the probe keeps one source of
    truth: a runtime the registry calls available is a runtime the executor can
    actually find. A second ``shutil.which`` here as a fallback defeats that — it
    would report the backend available even when the executor's own probe said
    otherwise, which is how the registry came to promise a backend that then
    refused. If the sandbox layer cannot be imported, the honest answer is
    "unavailable", not a guess from PATH.
    """
    try:
        from . import sandbox
        if sandbox._container_runtime():
            return True, ""
    except Exception:  # noqa: BLE001
        pass
    return False, "needs the container-isolation dependency; see Optional dependencies"


def detect() -> dict[str, tuple[bool, str]]:
    """Availability of optional external backends.

    Deliberately narrow: the only things probed are a container runtime, and only
    because the user must opt into it. No other harness is looked for, and nothing
    is read from another tool's directories.
    """
    return {"container": _container_available()}


def registry() -> list[dict]:
    """Every provider, with availability resolved right now."""
    found = detect()
    out = []
    for row in _BUILTIN:
        row = dict(row)
        if row["id"] in found:
            ok, why = found[row["id"]]
            row["available"] = ok
            row["reason"] = why
        out.append(row)
    return out


def by_kind(kind: str) -> list[dict]:
    return [p for p in registry() if p["kind"] == kind]


def get(pid: str, kind: str | None = None) -> dict | None:
    for row in registry():
        if row["id"] == pid and (kind is None or row["kind"] == kind):
            return row
    return None


def available(pid: str, kind: str | None = None) -> tuple[bool, str]:
    row = get(pid, kind)
    if row is None:
        return False, f"no provider '{pid}'"
    return bool(row["available"]), row["reason"]


# ── current selection ──────────────────────────────────────────────────────
def load() -> dict:
    raw = config.read_json(config.CAPABILITIES_FILE, {})
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key].update(value)
            elif key in out:
                out[key] = value
    return out


def save(patch: dict) -> dict:
    """Merge a patch into the stored selection. Unknown keys are ignored."""
    current = load()
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key].update(value)
        elif key in current:
            current[key] = value
    config.write_json(config.CAPABILITIES_FILE, current)
    return current


def resolve(kind: str) -> dict:
    """The backend in force for a kind, with a clear reason if it is unusable.

    A selected backend that is unavailable, or that has no adapter yet, is
    reported as such. It is never quietly replaced by a weaker one.
    """
    cfg = load()
    if kind == "sandbox":
        chosen = str(cfg["sandbox"].get("backend") or "none")
    elif kind == "memory":
        chosen = str(cfg["memory"].get("mode") or "off")
    elif kind == "learning":
        chosen = str(cfg["learning"].get("mode") or "propose")
    elif kind == "gateway":
        chosen = str((cfg.get("gateway") or {}).get("id") or "none")
    else:
        chosen = "none"

    row = get(chosen, kind)
    if row is None:
        return {"kind": kind, "id": chosen, "ok": False, "available": False,
                "implemented": False, "reason": f"'{chosen}' is not a known {kind} backend",
                "provider": None}
    usable = bool(row["available"] and row["implemented"])
    reason = row["reason"] or ("" if row["implemented"] else
                                (row.get("blocked") or "Tacit has no adapter for this yet"))
    return {"kind": kind, "id": chosen, "ok": usable,
            "available": bool(row["available"]),
            "implemented": bool(row["implemented"]),
            "reason": reason, "provider": row}


def summary() -> dict:
    """One call for the interface: the selection, the registry, the cost."""
    cfg = load()
    return {
        "profile": cfg.get("profile") or "default",
        "config": cfg,
        "sandbox": resolve("sandbox"),
        "memory": resolve("memory"),
        "learning": resolve("learning"),
        "gateway": resolve("gateway"),
        "providers": registry(),
        "token_cost": tokens.estimate_tools_tokens([]),
        "exact": tokens.exact(),
    }

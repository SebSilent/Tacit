"""Profiles: named bundles of the optional capabilities.

Tacit's cost is made of parts you choose: which plugins are on, how much memory
is allowed into the prompt, and whether MCP tools are injected eagerly. A profile
is those choices captured under a name, so switching between "as small as
possible" and "everything on" is one action with a known price.

Stored in ``~/.tacit/profiles.json``. The token cost of every profile is computed
before you apply it, using the same estimator as the dashboard.
"""

from __future__ import annotations

from . import audit, config, mcp_registry, memory_store, plugin_manager, providers, tokens

# The seven tools a bare harness ships with: read, write, edit, shell, grep,
# list, glob. A profile can restrict the tool set to exactly these.
CORE_TOOLS = ("read_file", "write_file", "edit_file", "run_shell",
              "grep_files", "list_files", "glob_files")

BUILTIN = {
    "minimal": {
        "label": "Minimal",
        "description": "Seven core tools, nothing else. The smallest fixed prompt available.",
        "plugins": [],
        "memory_budget": 0,
        "mcp_direct": False,
        "tools": list(CORE_TOOLS),
        "capabilities": {"sandbox": "none", "memory": "off", "learning": "propose"},
    },
    "default": {
        "label": "Default",
        "description": "All tools, no memory, no sandbox, nothing learned. The default.",
        "plugins": [],
        "memory_budget": 0,
        "mcp_direct": False,
        "tools": None,
        "capabilities": {"sandbox": "none", "memory": "off", "learning": "propose"},
    },
    "safe": {
        "label": "Safe",
        "description": "Micro sandbox, memory limited to what you wrote or approved, "
                       "learning proposes only.",
        "plugins": ["memory_vault"],
        "memory_budget": memory_store.DEFAULT_BUDGET,
        "mcp_direct": False,
        "tools": None,
        "capabilities": {"sandbox": "tacit-micro", "memory": "explicit",
                         "learning": "propose"},
    },
    "power-isolation": {
        "label": "Power isolation",
        "description": "The strongest isolation Tacit can provide on this OS, plus "
                       "explicit memory. Nothing external is required.",
        "plugins": ["memory_vault"],
        "memory_budget": memory_store.DEFAULT_BUDGET,
        "mcp_direct": False,
        "tools": None,
        "capabilities": {"sandbox": "tacit-micro", "memory": "explicit",
                         "learning": "propose"},
    },
    "power-memory": {
        "label": "Power memory",
        "description": "Every memory feature Tacit implements: explicit notes plus "
                       "just-in-time retrieval. Sandbox left to you.",
        "plugins": ["memory_vault"],
        "memory_budget": memory_store.DEFAULT_BUDGET,
        "mcp_direct": False,
        "tools": None,
        "capabilities": {"sandbox": "none", "memory": "full", "learning": "propose"},
    },
    "full": {
        "label": "Full",
        "description": "Isolation, every memory feature, learning proposing only. "
                       "All of it implemented inside Tacit.",
        "plugins": ["memory_vault"],
        "memory_budget": memory_store.DEFAULT_BUDGET,
        "mcp_direct": False,
        "tools": None,
        "capabilities": {"sandbox": "tacit-micro", "memory": "full", "learning": "propose"},
    },
}

DEFAULT_PROFILE = "default"

# Names from earlier builds, so an existing profiles.json keeps working. "silent" was the name
# the default profile carried before it took its current name; "default" itself needs no entry
# because it is the canonical name now.
LEGACY_NAMES = {"lean": "default", "silent": "default", "assisted": "safe",
                "everything": "full", "dsh": "power-isolation",
                "hermes": "power-memory"}


def load() -> dict:
    data = config.read_json(config.PROFILES_FILE, {})
    if not isinstance(data, dict):
        data = {}
    custom = data.get("profiles")
    if not isinstance(custom, dict):
        custom = {}
    active = data.get("active") or DEFAULT_PROFILE
    active = LEGACY_NAMES.get(active, active)
    return {"active": active, "profiles": custom}


def save(data: dict) -> None:
    config.write_json(config.PROFILES_FILE, data)


def _all() -> dict:
    """Built-ins plus any the user saved. Custom names may shadow nothing."""
    data = load()
    out = {name: dict(cfg, builtin=True) for name, cfg in BUILTIN.items()}
    for name, cfg in data["profiles"].items():
        if name not in out:
            out[name] = dict(cfg, builtin=False)
    return out


def _plugin_tool_tokens(plugin_id: str) -> int:
    """The real schema cost of a plugin's tools, not its declared budget."""
    return tokens.estimate_tools_tokens(plugin_manager.tool_schemas(plugin_id))


def _tool_set(cfg: dict) -> list[dict]:
    """The tool schemas a profile leaves enabled."""
    from . import agent

    by_name = {t["function"]["name"]: t for t in agent.TOOLS}
    wanted = cfg.get("tools")
    if wanted is None:
        return list(by_name.values())
    return [by_name[n] for n in wanted if n in by_name]


def _tool_names() -> set[str]:
    from . import agent

    return {t["function"]["name"] for t in agent.TOOLS}


def cost_of(cfg: dict) -> dict:
    """Estimated fixed prompt cost of a profile, split by source.

    This is the whole standing cost: the base prompt, the tool schemas the
    profile leaves enabled, and any extras (plugins, memory, eager MCP).
    """
    try:
        from .ai import prompts

        prompt_tokens = tokens.estimate_tokens(prompts.system_prompt(None, False, chat=False))
    except Exception:  # noqa: BLE001
        prompt_tokens = 0

    chosen = _tool_set(cfg)
    tool_tokens = tokens.estimate_tools_tokens(chosen)

    enabled = list(cfg.get("plugins") or [])
    plugin_tokens = sum(_plugin_tool_tokens(pid) for pid in enabled)

    budget = int(cfg.get("memory_budget") or 0)
    memory_tokens = 0
    if "memory_vault" in enabled and budget > 0:
        available = sum(int(r.get("token_estimate") or 0)
                        for r in memory_store.list_memories(enabled=True, limit=2000))
        memory_tokens = min(budget, available)

    mcp_tokens = 0
    try:
        report = mcp_registry.injection_report()
        mcp_tokens = report["injected_tokens"] if cfg.get("mcp_direct") else 0
    except Exception:  # noqa: BLE001
        pass

    return {"prompt": prompt_tokens, "tools": tool_tokens, "tool_count": len(chosen),
            "plugins": plugin_tokens, "memory": memory_tokens, "mcp": mcp_tokens,
            "total": prompt_tokens + tool_tokens + plugin_tokens + memory_tokens + mcp_tokens,
            "exact": tokens.exact()}


def list_profiles() -> list[dict]:
    data = load()
    rows = []
    for name, cfg in _all().items():
        cost = cost_of(cfg)
        rows.append({
            "name": name,
            "label": cfg.get("label") or name.title(),
            "description": cfg.get("description") or "",
            "builtin": bool(cfg.get("builtin")),
            "active": name == data["active"],
            "plugins": list(cfg.get("plugins") or []),
            "memory_budget": int(cfg.get("memory_budget") or 0),
            "mcp_direct": bool(cfg.get("mcp_direct")),
            "tools": cfg.get("tools"),
            "tool_count": cost["tool_count"],
            "cost": cost,
            "cost_display": tokens.label(cost["total"]),
        })
    rows.sort(key=lambda r: (not r["builtin"], r["cost"]["total"], r["name"]))
    return rows


def current() -> dict:
    """The live state, expressed in the same shape as a profile."""
    enabled = [p["id"] for p in plugin_manager.list_plugins() if p["enabled"]]
    try:
        direct = bool(mcp_registry.settings().get("direct_mode"))
    except Exception:  # noqa: BLE001
        direct = False
    disabled = set(config.prefs().get("disabledTools") or [])
    live_tools = sorted(_tool_names() - disabled)
    tools = live_tools if set(live_tools) != _tool_names() else None
    return {"plugins": enabled, "memory_budget": memory_store.budget(),
            "mcp_direct": direct, "tools": tools, "tool_count": len(live_tools)}


def apply(name: str) -> dict:
    """Switch to a profile, and report honestly what it could not turn on.

    A profile is a bundle of choices. If one of them cannot be honoured, the rest
    still apply and the one that failed is named with its reason. A profile never
    silently gives you less than it claims.
    """
    name = LEGACY_NAMES.get(name, name)
    cfg = _all().get(name)
    if cfg is None:
        return {"ok": False, "error": f"no profile '{name}'"}

    applied, skipped = {}, []

    # capabilities first, so memory mode and budget are in place before the
    # plugins and the toolset are decided
    caps = dict(cfg.get("capabilities") or {})
    for kind in ("sandbox", "memory", "learning"):
        if kind not in caps:
            continue
        wanted = caps[kind]
        row = providers.get(wanted, kind)
        if row is None or not row["implemented"] or not row["available"]:
            skipped.append({"what": kind, "wanted": wanted,
                            "why": (row or {}).get("reason")
                                   or ("Tacit has no adapter for this yet"
                                       if row else "unknown backend")})
            continue
        if kind == "sandbox":
            providers.save({"sandbox": {"backend": wanted}})
        elif kind == "memory":
            providers.save({"memory": {"mode": wanted,
                                        "budget": int(cfg.get("memory_budget") or 0)}})
        else:
            providers.save({"learning": {"mode": wanted}})
        applied[kind] = wanted

    # the memory plugin follows the memory mode, so turning memory on gives the
    # agent its recall tools and turning it off takes them away
    wanted_plugins = list(cfg.get("plugins") or [])
    if applied.get("memory", "off") == "off" and "memory_vault" in wanted_plugins:
        wanted_plugins.remove("memory_vault")
    known = {p["id"] for p in plugin_manager.list_plugins()}
    for pid in known:
        if pid in wanted_plugins:
            plugin_manager.enable(pid)
        else:
            plugin_manager.disable(pid)
    memory_store.set_budget(int(cfg.get("memory_budget") or 0))
    try:
        mcp_registry.save_settings({"direct_mode": bool(cfg.get("mcp_direct"))})
    except Exception:  # noqa: BLE001
        pass

    want_tools = cfg.get("tools")
    disabled = [] if want_tools is None else sorted(_tool_names() - set(want_tools))
    config.save_prefs({"disabledTools": disabled})

    data = load()
    data["active"] = name
    save(data)
    audit.record("profile_applied", mode=name, status="ok" if not skipped else "partial",
                 applied=applied, skipped=[s["what"] for s in skipped])
    return {"ok": True, "active": name, "applied": applied, "skipped": skipped,
            "applied_now": current(), "cost": cost_of(cfg)}


def capture(name: str, label: str = "", description: str = "") -> dict:
    """Save the current state as a reusable profile."""
    clean = str(name or "").strip().lower().replace(" ", "-")
    if not clean:
        return {"ok": False, "error": "a profile name is required"}
    # A legacy name is an alias of a built-in, so captures under it would shadow the real one.
    clean = LEGACY_NAMES.get(clean, clean)
    if clean in BUILTIN:
        return {"ok": False, "error": f"'{clean}' is a built-in profile"}
    data = load()
    state = current()
    data["profiles"][clean] = {
        "label": label.strip() or clean.title(),
        "description": description.strip() or "Saved from the current configuration.",
        "plugins": state["plugins"],
        "memory_budget": state["memory_budget"],
        "mcp_direct": state["mcp_direct"],
    }
    save(data)
    return {"ok": True, "name": clean, "profiles": list_profiles()}


def delete(name: str) -> dict:
    # Legacy names resolve before anything else: "delete silent" targets the built-in now called
    # default, and must be refused for the same reason.
    name = LEGACY_NAMES.get(name, name)
    if name in BUILTIN:
        return {"ok": False, "error": "built-in profiles cannot be deleted"}
    data = load()
    if name not in data["profiles"]:
        return {"ok": False, "error": f"no profile '{name}'"}
    del data["profiles"][name]
    if data.get("active") == name:
        data["active"] = DEFAULT_PROFILE
    save(data)
    return {"ok": True, "removed": name}

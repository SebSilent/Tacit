"""Profiles: named bundles of the optional capabilities.

Tacit's cost is made of parts you choose: which plugins are on, how much memory
is allowed into the prompt, and whether MCP tools are injected eagerly. A profile
is those choices captured under a name, so switching between "as small as
possible" and "everything on" is one action with a known price.

Stored in ``~/.tacit/profiles.json``. The token cost of every profile is computed
before you apply it, using the same estimator as the dashboard.
"""

from __future__ import annotations

from . import config, mcp_registry, memory_store, plugin_manager, tokens

BUILTIN = {
    "lean": {
        "label": "Lean",
        "description": "No plugins, no memory, MCP tools loaded only when used.",
        "plugins": [],
        "memory_budget": 0,
        "mcp_direct": False,
    },
    "assisted": {
        "label": "Assisted",
        "description": "Memory on at the default budget. Everything else stays off.",
        "plugins": ["memory_vault"],
        "memory_budget": memory_store.DEFAULT_BUDGET,
        "mcp_direct": False,
    },
    "full": {
        "label": "Full",
        "description": "Every bundled plugin on, memory at the default budget.",
        "plugins": ["memory_vault", "dsh_bridge"],
        "memory_budget": memory_store.DEFAULT_BUDGET,
        "mcp_direct": False,
    },
    "everything": {
        "label": "Everything",
        "description": "Full, and MCP tools injected eagerly instead of on demand.",
        "plugins": ["memory_vault", "dsh_bridge"],
        "memory_budget": memory_store.DEFAULT_BUDGET,
        "mcp_direct": True,
    },
}

DEFAULT_PROFILE = "lean"


def load() -> dict:
    data = config.read_json(config.PROFILES_FILE, {})
    if not isinstance(data, dict):
        data = {}
    custom = data.get("profiles")
    if not isinstance(custom, dict):
        custom = {}
    active = data.get("active") or DEFAULT_PROFILE
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


def cost_of(cfg: dict) -> dict:
    """Estimated prompt cost of a profile, split by source."""
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

    return {"plugins": plugin_tokens, "memory": memory_tokens, "mcp": mcp_tokens,
            "total": plugin_tokens + memory_tokens + mcp_tokens,
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
    return {"plugins": enabled, "memory_budget": memory_store.budget(), "mcp_direct": direct}


def apply(name: str) -> dict:
    cfg = _all().get(name)
    if cfg is None:
        return {"ok": False, "error": f"no profile '{name}'"}
    known = {p["id"] for p in plugin_manager.list_plugins()}
    wanted = [pid for pid in (cfg.get("plugins") or []) if pid in known]
    for pid in known:
        if pid in wanted:
            plugin_manager.enable(pid)
        else:
            plugin_manager.disable(pid)
    memory_store.set_budget(int(cfg.get("memory_budget") or 0))
    try:
        mcp_registry.save_settings({"direct_mode": bool(cfg.get("mcp_direct"))})
    except Exception:  # noqa: BLE001
        pass
    data = load()
    data["active"] = name
    save(data)
    return {"ok": True, "active": name, "applied": current(), "cost": cost_of(cfg)}


def capture(name: str, label: str = "", description: str = "") -> dict:
    """Save the current state as a reusable profile."""
    clean = str(name or "").strip().lower().replace(" ", "-")
    if not clean:
        return {"ok": False, "error": "a profile name is required"}
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

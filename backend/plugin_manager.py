"""Lightweight plugin manager.

A plugin declares metadata and may contribute tools or MCP server definitions.
Three rules shape this module:

* **Disabled unless enabled.** Nothing a plugin provides reaches the agent
  until the user turns it on.
* **No silent execution.** A plugin's code is imported only when it is enabled.
  Bundled plugins (shipped in ``backend/plugins``) are ours and are read
  directly; a *user* plugin must ship a ``manifest.json`` so it can be listed
  without running it.
* **Visible cost.** Every plugin reports the tokens it adds to the startup
  prompt. A plugin that would inflate the always-on prompt must say so.

A plugin contributes by defining ``tools()`` (tool schemas), ``call(name, args,
ctx)`` to dispatch them, and optionally ``mcp_servers()`` to register external
tool servers. Those are the two extension points the harness consumes.

State lives in ``~/.tacit/plugins.json``.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import threading
import traceback
from pathlib import Path

from . import config, tokens

_LOCK = threading.Lock()
_MODULES: dict[str, object] = {}
_ERRORS: dict[str, str] = {}
_LOGS: dict[str, list[dict]] = {}
_LOG_LIMIT = 200

REQUIRED_FIELDS = ("id", "name", "description")


# ── state ──────────────────────────────────────────────────────────────────
def _state() -> dict:
    data = config.read_json(config.PLUGINS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("enabled", [])
    data.setdefault("settings", {})
    if not isinstance(data["enabled"], list):
        data["enabled"] = []
    if not isinstance(data["settings"], dict):
        data["settings"] = {}
    return data


def _save(data: dict) -> None:
    config.write_json(config.PLUGINS_FILE, data)


def is_enabled(plugin_id: str) -> bool:
    return plugin_id in _state()["enabled"]


# ── discovery ──────────────────────────────────────────────────────────────
def _candidates() -> list[tuple[str, Path, Path, dict]]:
    """(plugin_id, directory, entry_path, manifest) for everything on disk."""
    found: list[tuple[str, Path, Path, dict]] = []

    bundled = config.BUNDLED_PLUGINS_DIR
    if bundled.is_dir():
        for path in sorted(bundled.glob("*.py")):
            if path.name.startswith("_"):
                continue
            found.append((path.stem, bundled, path, {}))

    user = config.PLUGINS_USER_DIR
    if user.is_dir():
        for entry in sorted(user.iterdir()):
            if not entry.is_dir():
                continue
            manifest = config.read_json(entry / "manifest.json", {}) or {}
            code = entry / "plugin.py"
            if code.exists():
                found.append((manifest.get("id") or entry.name, entry, code, manifest))
    return found


def _load(plugin_id: str, entry: Path):
    """Import a plugin's code. Called only for enabled plugins."""
    with _LOCK:
        if plugin_id in _MODULES:
            return _MODULES[plugin_id]
        safe = re.sub(r"[^0-9A-Za-z_]", "_", plugin_id)
        bundled = entry.parent == config.BUNDLED_PLUGINS_DIR
        # Bundled plugins sit beside Tacit's own modules, so they get a real
        # package path and `from .. import x` works. User plugins are loaded
        # standalone and should use absolute imports (`from backend import x`).
        name = f"backend.plugins.{safe}" if bundled else f"tacit_user_plugin_{safe}"
        spec = importlib.util.spec_from_file_location(name, str(entry))
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _MODULES[plugin_id] = module
        return module


def _meta(plugin_id: str, entry: Path, manifest: dict) -> dict:
    """Metadata without executing anything for user plugins that ship a manifest."""
    meta = dict(manifest)
    if not meta and entry.suffix == ".py":
        # bundled plugin: reading its PLUGIN dict is safe, it is our own code
        try:
            module = _load(plugin_id, entry)
            declared = getattr(module, "PLUGIN", None)
            if isinstance(declared, dict):
                meta = dict(declared)
        except Exception as exc:  # noqa: BLE001
            _ERRORS[plugin_id] = f"{type(exc).__name__}: {exc}"
    meta.setdefault("id", plugin_id)
    meta.setdefault("name", plugin_id.replace("_", " ").title())
    meta.setdefault("description", "")
    meta.setdefault("version", "0.0")
    meta.setdefault("permissions", [])
    meta.setdefault("token_budget", 0)
    meta.setdefault("settings", [])
    return meta


def log(plugin_id: str, message: str, level: str = "info") -> None:
    rows = _LOGS.setdefault(plugin_id, [])
    rows.append({"ts": round(__import__("time").time(), 3), "level": level, "message": message})
    del rows[:-_LOG_LIMIT]


# ── public API ─────────────────────────────────────────────────────────────
def describe(plugin_id: str, entry: Path, manifest: dict) -> dict:
    meta = _meta(plugin_id, entry, manifest)
    enabled = is_enabled(plugin_id)
    hooks = _hook_names(plugin_id, enabled)
    return {
        "id": meta["id"],
        "name": meta["name"],
        "description": meta.get("description", ""),
        "version": meta.get("version", "0.0"),
        "permissions": meta.get("permissions", []),
        "settings": meta.get("settings", []),
        "token_budget": int(meta.get("token_budget") or 0),
        "enabled": enabled,
        "provides": hooks,
        "error": _ERRORS.get(meta["id"]),
        "source": "bundled" if entry.parent == config.BUNDLED_PLUGINS_DIR else "user",
        "path": str(entry),
    }


def _hook_names(plugin_id: str, enabled: bool) -> list[str]:
    """Capabilities a plugin actually contributes.

    Only hooks the harness consumes are reported, so the interface never
    advertises something that would do nothing when switched on.
    """
    if not enabled:
        return []
    with _LOCK:
        module = _MODULES.get(plugin_id)
    if module is None:
        return []
    names = []
    for hook, label in (("tools", "tools"), ("mcp_servers", "mcp")):
        if callable(getattr(module, hook, None)):
            names.append(label)
    return names


def list_plugins() -> list[dict]:
    return [describe(pid, entry, manifest) for pid, _dir, entry, manifest in _candidates()]


def get(plugin_id: str) -> dict | None:
    for pid, _dir, entry, manifest in _candidates():
        if pid == plugin_id:
            return describe(pid, entry, manifest)
    return None


def enable(plugin_id: str) -> dict:
    found = next(((pid, entry) for pid, _d, entry, _m in _candidates() if pid == plugin_id), None)
    if not found:
        return {"ok": False, "error": f"no plugin '{plugin_id}'"}
    pid, entry = found
    try:
        _load(pid, entry)
        _ERRORS.pop(pid, None)
        log(pid, "enabled")
    except Exception as exc:  # noqa: BLE001
        _ERRORS[pid] = f"{type(exc).__name__}: {exc}"
        log(pid, f"failed to load: {exc}", "error")
        return {"ok": False, "error": _ERRORS[pid]}
    state = _state()
    if pid not in state["enabled"]:
        state["enabled"].append(pid)
    _save(state)
    return {"ok": True, "id": pid, "enabled": True}


def disable(plugin_id: str) -> dict:
    state = _state()
    state["enabled"] = [p for p in state["enabled"] if p != plugin_id]
    _save(state)
    log(plugin_id, "disabled")
    return {"ok": True, "id": plugin_id, "enabled": False}


def settings_of(plugin_id: str) -> dict:
    meta = get(plugin_id)
    if not meta:
        return {"ok": False, "error": f"no plugin '{plugin_id}'"}
    declared = {s.get("key"): s for s in (meta.get("settings") or []) if s.get("key")}
    current = dict(_state()["settings"].get(plugin_id) or {})
    resolved = {}
    for key, spec in declared.items():
        resolved[key] = current.get(key, spec.get("default"))
    return {"ok": True, "id": plugin_id, "schema": meta.get("settings") or [], "values": resolved}


def save_settings(plugin_id: str, values: dict) -> dict:
    if not get(plugin_id):
        return {"ok": False, "error": f"no plugin '{plugin_id}'"}
    state = _state()
    merged = dict(state["settings"].get(plugin_id) or {})
    for key, value in (values or {}).items():
        merged[str(key)] = value
    state["settings"][plugin_id] = merged
    _save(state)
    return {"ok": True, "id": plugin_id, "values": merged}


def logs(plugin_id: str) -> list[dict]:
    return list(_LOGS.get(plugin_id, []))


# ── contributions ──────────────────────────────────────────────────────────
def _enabled_modules():
    for pid, _dir, entry, _manifest in _candidates():
        if not is_enabled(pid):
            continue
        try:
            yield pid, _load(pid, entry)
        except Exception as exc:  # noqa: BLE001
            _ERRORS[pid] = f"{type(exc).__name__}: {exc}"
            continue


def collect_tools() -> list[dict]:
    """Tool schemas contributed by enabled plugins."""
    rows = []
    for pid, module in _enabled_modules():
        fn = getattr(module, "tools", None)
        if callable(fn):
            try:
                rows.extend(fn() or [])
            except Exception as exc:  # noqa: BLE001
                _ERRORS[pid] = str(exc)
    return rows


def tool_schemas(plugin_id: str) -> list[dict]:
    """The tools a plugin contributes, whether or not it is enabled.

    Used to price a capability before switching it on.
    """
    for pid, _dir, entry, _manifest in _candidates():
        if pid != plugin_id:
            continue
        try:
            module = _load(pid, entry)
            fn = getattr(module, "tools", None)
            return list(fn() or []) if callable(fn) else []
        except Exception:  # noqa: BLE001
            return []
    return []


def collect_mcp_servers() -> list[dict]:
    rows = []
    for pid, module in _enabled_modules():
        fn = getattr(module, "mcp_servers", None)
        if callable(fn):
            try:
                for spec in (fn() or []):
                    spec = dict(spec)
                    spec.setdefault("plugin", pid)
                    rows.append(spec)
            except Exception as exc:  # noqa: BLE001
                _ERRORS[pid] = str(exc)
    return rows


def call_tool(name: str, args: dict, ctx: dict):
    """Dispatch a plugin tool call. Returns ``None`` if no plugin owns ``name``."""
    for pid, module in _enabled_modules():
        fn = getattr(module, "call", None)
        if callable(fn):
            try:
                result = fn(name, args or {}, ctx or {})
            except Exception as exc:  # noqa: BLE001
                log(pid, f"{name} failed: {exc}", "error")
                return f"ERROR: plugin '{pid}' tool '{name}' failed: {exc}"
            if result is not None:
                return result
    return None


def token_impact() -> dict:
    """Startup-prompt cost of the enabled plugins, for the dashboard."""
    rows = []
    total = 0
    for meta in list_plugins():
        cost = int(meta.get("token_budget") or 0) if meta.get("enabled") else 0
        total += cost
        rows.append({"id": meta["id"], "name": meta["name"],
                     "enabled": meta["enabled"], "tokens": cost})
    return {"rows": rows, "total": total,
            "tools_tokens": tokens.estimate_tools_tokens(collect_tools()),
            "exact": tokens.exact()}

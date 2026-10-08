"""Per-model settings: optional overrides for temperature, max_tokens and
context_budget, keyed by model reference.

Renamed from ``benchmarks.py`` in stage 5 (plan item 10): the name described
who used to write these rows — the benchmark harness — not what they are. The
``benchmark`` agent tool that managed them is deleted; per-model settings are
configuration, not an agent capability, so they are managed from the interface
(Providers / General) instead. The mechanism itself is load-bearing:
``agent.context_budget()`` reads ``profile_for(ref)`` for the window cap, and
``run_turn`` reads ``temperature``/``max_tokens`` from it, so deleting the
store would change model behaviour.

The file stays ``~/.tacit/benchmarks.json`` so an existing install keeps its
rows without a migration.
"""

from __future__ import annotations

import json

from . import config


def all_profiles() -> dict:
    rows = config.read_json(config.MODEL_SETTINGS_FILE, {})
    return rows if isinstance(rows, dict) else {}


def profile_for(ref: str | None) -> dict:
    if not ref:
        return {}
    rows = all_profiles()
    exact = rows.get(ref)
    if isinstance(exact, dict):
        return exact
    provider = str(ref).split("/", 1)[0]
    fallback = rows.get(provider)
    return fallback if isinstance(fallback, dict) else {}


def set_profile(ref: str, temperature=None, max_tokens=None, context_budget=None) -> str:
    name = str(ref or "").strip()
    if not name:
        return "ERROR: model reference required"
    rows = all_profiles()
    cur = rows.get(name) if isinstance(rows.get(name), dict) else {}
    for key, value in (("temperature", temperature), ("max_tokens", max_tokens),
                       ("context_budget", context_budget)):
        if value is not None:
            try:
                cur[key] = float(value) if key == "temperature" else int(value)
            except (TypeError, ValueError):
                return f"ERROR: {key} must be a number"
            if value is None or cur[key] < 0:
                cur.pop(key, None)
    if cur:
        rows[name] = cur
    else:
        rows.pop(name, None)
    config.write_json(config.MODEL_SETTINGS_FILE, rows)
    return f"{name}: {json.dumps(cur) if cur else '(cleared)'}"


def listing() -> str:
    rows = all_profiles()
    if not rows:
        return (f"no per-model settings yet - stored in {config.MODEL_SETTINGS_FILE}\n"
                "set them from the interface (Providers), or in the file directly")
    return "\n".join(f"{k}: {json.dumps(v)}" for k, v in sorted(rows.items()))
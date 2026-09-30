"""Per-session token metrics — local only, never transmitted anywhere.

The point of these counters is to make the *cost of context discipline* legible:
what was actually injected, and what was avoided by lazy loading, budgeting,
compaction and sub-agent delegation. They live in the session record so the
dashboard can read them straight back.
"""

from __future__ import annotations

FIELDS = (
    "prompt_tokens",        # input tokens billed
    "completion_tokens",    # output tokens billed
    "tool_schema_tokens",   # tool schemas injected into requests
    "memory_tokens",        # memory block injected at startup
    "mcp_tool_tokens",      # MCP schemas injected (activated or pinned)
    "saved_lazy_tools",     # MCP schemas NOT injected thanks to lazy activation
    "saved_memory_budget",  # memory tokens NOT injected thanks to the budget
    "saved_compaction",     # tokens reclaimed by compaction
    "saved_subagent",       # tokens kept out of the main window by delegation
    "mcp_calls",            # MCP tool invocations
)


def blank() -> dict:
    return {k: 0 for k in FIELDS}


def get(rec: dict) -> dict:
    """The session's metrics, filled in with zeros for anything missing."""
    stored = (rec or {}).get("metrics") or {}
    return {**blank(), **{k: int(stored.get(k) or 0) for k in FIELDS}}


def bump(rec: dict, **deltas) -> dict:
    """Add to the session's counters. Unknown keys are ignored."""
    if rec is None:
        return blank()
    current = get(rec)
    for key, value in deltas.items():
        if key in FIELDS:
            try:
                current[key] += int(value)
            except (TypeError, ValueError):
                continue
    rec["metrics"] = current
    return current


def dashboard(rec: dict, *, base_prompt_tokens: int = 0, tools: int = 0,
              mcp_discovered: int = 0, memory_total: int = 0,
              mcp_injected: int = 0) -> dict:
    """The numbers behind the token dashboard.

    ``full_context_baseline`` is what the prompt would cost if every discovered
    MCP schema and every enabled memory were injected directly. ``actual`` is
    what Tacit really injects. The difference is the saving from context
    discipline — no competitor numbers are guessed, only these two are compared.
    """
    m = get(rec)
    baseline = base_prompt_tokens + tools + mcp_discovered + memory_total
    actual = base_prompt_tokens + tools + mcp_injected + m["memory_tokens"]
    accounted = (m["saved_lazy_tools"] + m["saved_memory_budget"]
                 + m["saved_compaction"] + m["saved_subagent"])
    return {
        "prompt_tokens": m["prompt_tokens"],
        "completion_tokens": m["completion_tokens"],
        "total_tokens": m["prompt_tokens"] + m["completion_tokens"],
        "base_prompt_tokens": base_prompt_tokens,
        "tool_schema_tokens": tools,
        "mcp_discovered_tokens": mcp_discovered,
        "mcp_injected_tokens": mcp_injected,
        "memory_tokens": m["memory_tokens"],
        "memory_budget_tokens": memory_total,
        "mcp_calls": m["mcp_calls"],
        "saved_lazy_tools": m["saved_lazy_tools"],
        "saved_memory_budget": m["saved_memory_budget"],
        "saved_compaction": m["saved_compaction"],
        "saved_subagent": m["saved_subagent"],
        "saved_total": accounted,
        "full_context_baseline": baseline,
        "actual_startup": actual,
        "saved_by_discipline": max(0, baseline - actual),
    }

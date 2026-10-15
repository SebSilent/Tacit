"""Per-session token metrics — local only, never transmitted anywhere.

The point of these counters is to make the *cost of context discipline* legible:
what was actually injected, and what was avoided by lazy loading, budgeting,
compaction and sub-agent delegation. They live in the session record so the
dashboard can read them straight back.
"""

from __future__ import annotations

FIELDS = (
    "prompt_tokens",        # input tokens billed, cached reads included: what
                            # occupied the window, which is what the cache rate
                            # is a fraction *of*
    "completion_tokens",    # output tokens billed
    "cached_tokens",        # input tokens the provider served from its own cache
    "instruction_tokens",   # the project's own instruction files, injected at startup
    "task_tokens",          # the session task list, injected only when non-empty
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


def _int(value) -> int:
    """A token count from a provider, which sometimes sends a string or nothing."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_usage(usage: dict | None) -> dict:
    """One model call's usage, reduced to the six numbers every consumer agrees on.

    Two paths accumulate usage and they must never disagree about what a token is:
    the server's live meter (``session_runtime._usage_event``), which drives the
    context percentage and the compaction trigger, and the headless benchmark
    runner (``headless._accumulate``), which writes the number a harness comparison
    is judged on. Providers are inconsistent — ``total`` is often absent, cache
    figures arrive only on some backends, and a count can be a string — so the
    parsing is done once, here.

    ``fresh`` is the one that matters for cost: cached reads are re-reads of bytes
    already paid for, so what a call actually *costed* is its uncached input plus
    its output. That is OpenBench's stated token basis, and the reason it is
    computed here rather than at either call site.
    """
    u = usage or {}
    inp = _int(u.get("input"))
    out = _int(u.get("output"))
    cache_read = _int(u.get("cache_read"))
    return {
        "input": inp,
        "output": out,
        "total": _int(u.get("total")) or (inp + out),
        "cache_read": cache_read,
        "cache_write": _int(u.get("cache_write")),
        "fresh": max(0, inp, cache_read) + out,
    }


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
        # A turn re-sends its whole transcript on every step, so what the provider
        # served from cache is most of the bill on a long session. Reporting it is
        # the only way to tell a cheap long session from an expensive one.
        "cached_tokens": m["cached_tokens"],
        "cache_hit_rate": (round(m["cached_tokens"] / m["prompt_tokens"] * 100, 1)
                           if m["prompt_tokens"] else 0.0),
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
        "saved_by_discipline": max(0, baseline, actual),
    }

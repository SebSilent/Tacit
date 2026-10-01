"""Memory modes.

Memory either earns its place in the prompt or it does not go in. This module
decides what the agent may see, states why for every item it injects, and keeps
the whole thing inside a token budget.

Modes:
  off        nothing persistent is injected, and nothing is retrieved
  explicit   only what you wrote or approved. Nothing learned ever appears.
  jit        the smallest useful set: always-relevant items go in, everything
             else is fetched per turn by relevance rather than carried around

The rule is the same in all three: an item that is injected can be pointed at,
and explained, and measured.
"""

from __future__ import annotations

from . import memory_store, providers, tokens

MODES = ("off", "explicit", "jit")
HEADER = "Memory (durable notes you chose to keep):"
JIT_SHARE = 0.5          # jit keeps half the budget for mid-turn retrieval


def mode() -> str:
    """The memory mode in force. Anything unset or unknown means off."""
    chosen = str((providers.load().get("memory") or {}).get("mode") or "off")
    return chosen if chosen in MODES else "off"


def budget() -> int:
    try:
        return max(0, int((providers.load().get("memory") or {}).get("budget") or 0))
    except (TypeError, ValueError):
        return 0


def is_on() -> bool:
    return mode() != "off" and budget() > 0


def _entry(row: dict, why: str) -> dict:
    """One injected item, with everything needed to explain it later."""
    return {
        "id": row.get("id"),
        "content": row.get("content") or "",
        "type": row.get("type") or "other",
        "scope": row.get("scope") or "global",
        "scope_key": row.get("scope_key") or "",
        "confidence": row.get("confidence") or "medium",
        "source": row.get("source") or "user",
        "source_session": row.get("source_session") or "",
        "created_at": row.get("created_at") or 0,
        "last_used_at": row.get("last_used_at") or 0,
        "use_count": int(row.get("use_count") or 0),
        "tokens": int(row.get("token_estimate") or 0),
        "why": why,
    }


def _render(items: list[dict]) -> str:
    if not items:
        return ""
    return HEADER + "\n" + "\n".join(f"- [{i['type']}] {i['content']}" for i in items)


def _candidates(project: str, cap_mode: str) -> list[tuple[dict, str]]:
    """(row, why it is a candidate) before the budget is applied."""
    pool = memory_store.list_memories(enabled=True, project=project, limit=2000)
    if cap_mode == "explicit":
        # Everything here is enabled, and enabled means you approved it. A
        # proposal you have not accepted stays disabled and never appears.
        return [(r, "you wrote this" if r.get("source") == "user" else "you approved this")
                for r in pool]
    out = []
    for row in pool:
        if row.get("pinned"):
            out.append((row, "pinned"))
        elif row.get("confidence") == "high":
            out.append((row, "high confidence"))
    return out


def startup(project: str = "") -> dict:
    """What is injected before a turn, with provenance and an honest cost."""
    current = mode()
    cap = budget()
    empty = {"mode": current, "items": [], "text": "", "tokens": 0, "budget": cap,
             "count": 0, "held_back": 0, "retrieval_budget": 0}
    if current == "off" or cap <= 0:
        return empty

    limit = int(cap * JIT_SHARE) if current == "jit" else cap
    rank = {"high": 0, "medium": 1, "low": 2}
    rows = _candidates(project, current)
    rows.sort(key=lambda pair: (0 if pair[0].get("pinned") else 1,
                                rank.get(pair[0].get("confidence"), 1),
                                -int(pair[0].get("use_count") or 0),
                                -float(pair[0].get("created_at") or 0)))

    chosen: list[dict] = []
    held_back = 0
    for row, why in rows[:200]:
        trial = chosen + [_entry(row, why)]
        # measure the rendered block, so the header can never push past the cap
        if tokens.estimate_tokens(_render(trial)) <= limit:
            chosen = trial
        else:
            held_back += 1

    text = _render(chosen)
    return {"mode": current, "items": chosen, "text": text,
            "tokens": tokens.estimate_tokens(text), "budget": cap,
            "count": len(chosen), "held_back": held_back,
            "retrieval_budget": max(0, cap - limit) if current == "jit" else 0}


def recall_for(query: str, project: str = "", limit: int = 5,
               session: str = "") -> dict:
    """Mid-turn retrieval. Only jit uses this; explicit and off return nothing."""
    if mode() != "jit" or not str(query or "").strip():
        return {"mode": mode(), "items": [], "tokens": 0, "query": query}
    hits = memory_store.recall(query, limit=limit, project=project)
    items = [_entry(h, f"matched {query!r}") for h in hits]
    for item in items:
        if item["id"]:
            memory_store.reinforce(item["id"], ttl_days=0)
    return {"mode": "jit", "items": items, "query": query,
            "tokens": sum(i["tokens"] for i in items)}


def report(project: str = "") -> dict:
    """Everything the interface needs to explain memory for this turn."""
    started = startup(project)
    stats = memory_store.stats(project)
    return {
        "mode": started["mode"],
        "budget": started["budget"],
        "used": started["tokens"],
        "remaining": max(0, started["budget"] - started["tokens"]),
        "retrieval_budget": started["retrieval_budget"],
        "injected": started["items"],
        "held_back": started["held_back"],
        "available": stats.get("count", 0),
        "enabled": stats.get("enabled", 0),
        "display": tokens.label(started["tokens"]),
        "exact": tokens.exact(),
    }

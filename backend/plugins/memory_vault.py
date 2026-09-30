"""Memory Vault plugin — optional, transparent, token-budgeted memory.

Disabled by default. When enabled it contributes a handful of small tools; the
startup block it injects is capped by the memory budget (default 120 tokens) and
is reported by the token dashboard, so "persistent learning" never becomes a
silent tax on every request.
"""

from __future__ import annotations

from .. import memory_store
from .. import tokens as token_mod

PLUGIN = {
    "id": "memory_vault",
    "name": "Memory Vault",
    "description": "Durable, inspectable notes with a hard token budget. Off by default.",
    "version": "0.1",
    "permissions": ["storage"],
    "token_budget": 0,  # the tools are tiny; the injected block is budgeted separately
    "settings": [
        {"key": "auto_approve", "label": "Auto-approve memories the agent proposes",
         "type": "bool", "default": False},
        {"key": "budget", "label": "Startup memory budget (tokens)",
         "type": "int", "default": memory_store.DEFAULT_BUDGET},
    ],
}

_TYPES = list(memory_store.TYPES)
_SCOPES = list(memory_store.SCOPES)
_CONF = list(memory_store.CONFIDENCE)

_S = {"type": "string"}
_I = {"type": "integer"}


def _setting(key: str, default):
    try:
        from .. import plugin_manager

        values = (plugin_manager.settings_of("memory_vault") or {}).get("values") or {}
        return values.get(key, default)
    except Exception:  # noqa: BLE001
        return default


def tools() -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "memory_recall",
            "description": "Search durable memory for things the user chose to keep. "
                           "Use it before asking the user to repeat themselves.",
            "parameters": {"type": "object",
                           "properties": {"query": _S, "limit": _I,
                                          "scope": {"type": "string", "enum": _SCOPES}},
                           "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "memory_add",
            "description": "Propose a durable memory for later sessions. Unless the user "
                           "enabled auto-approval it lands disabled, pending review.",
            "parameters": {"type": "object",
                           "properties": {"content": _S,
                                          "type": {"type": "string", "enum": _TYPES},
                                          "scope": {"type": "string", "enum": _SCOPES},
                                          "confidence": {"type": "string", "enum": _CONF}},
                           "required": ["content"]}}},
        {"type": "function", "function": {
            "name": "memory_update",
            "description": "Edit a stored memory by id.",
            "parameters": {"type": "object",
                           "properties": {"id": _I, "content": _S},
                           "required": ["id", "content"]}}},
        {"type": "function", "function": {
            "name": "memory_delete",
            "description": "Delete a stored memory by id.",
            "parameters": {"type": "object", "properties": {"id": _I}, "required": ["id"]}}},
        {"type": "function", "function": {
            "name": "memory_list_summary",
            "description": "Compact summary of what memory holds and what it costs at startup.",
            "parameters": {"type": "object", "properties": {"limit": _I}, "required": []}}},
    ]


def call(name: str, args: dict, ctx: dict):
    args = args or {}
    project = (ctx or {}).get("project") or ""
    if name == "memory_recall":
        hits = memory_store.recall(args.get("query") or "", limit=args.get("limit") or 5,
                                   scope=args.get("scope") or "", project=project)
        if not hits:
            return "No matching memories."
        return "\n".join(f"[{h['id']}] ({h['type']}, {h['confidence']}) {h['content']}"
                         for h in hits)
    if name == "memory_add":
        approve = bool(_setting("auto_approve", False))
        res = memory_store.add(args.get("content") or "", type=args.get("type") or "other",
                               scope=args.get("scope") or "global",
                               confidence=args.get("confidence") or "medium",
                               project=project, source="agent_suggestion",
                               enabled=approve)
        if not res.get("ok"):
            return f"ERROR: {res.get('error')}"
        m = res["memory"]
        state = "stored and enabled" if approve else "stored as a pending suggestion"
        st = memory_store.stats(project)
        return (f"{state}: memory #{m['id']} (~{m['token_estimate']} tokens). Startup block "
                f"uses {token_mod.label(st['startup_tokens'])} of its {st['budget']}-token budget.")
    if name == "memory_update":
        res = memory_store.update(int(args.get("id") or 0), content=args.get("content") or "")
        return (f"updated memory #{args.get('id')}" if res.get("ok")
                else f"ERROR: {res.get('error')}")
    if name == "memory_delete":
        res = memory_store.delete(int(args.get("id") or 0))
        return (f"deleted memory #{args.get('id')}" if res.get("ok")
                else "ERROR: no such memory")
    if name == "memory_list_summary":
        st = memory_store.stats(project)
        rows = memory_store.list_memories(project=project, limit=args.get("limit") or 10)
        head = (f"{st['count']} memories ({st['enabled']} enabled, {st['pinned']} pinned). "
                f"Startup: {token_mod.label(st['startup_tokens'])} of {st['budget']} tokens, "
                f"{st['excluded_by_budget']} held back by the budget.")
        body = "\n".join(f"[{r['id']}] ({r['type']}) {r['content'][:120]}" for r in rows)
        return head + ("\n" + body if body else "")
    return None

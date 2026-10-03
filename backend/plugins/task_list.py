"""Task List plugin — the turn's remaining work, kept outside the context window.

A long turn holds its task list in the transcript, which means two things go wrong
at once: every step re-pays for it, and a compaction can summarise it into prose the
model then has to re-derive. Claude Code keeps its todo list as data for exactly
this reason.

Kept here as a plugin rather than a built-in tool because it enlarges the starting
prompt, and the rule this project sets for such a feature is that it must be
optional and must show its cost. It is off by default; **Settings > Plugins**
reports the schema tokens before you enable it, and the block it injects appears
only once there is something in it — an unused task list costs nothing at all.

The list lives on the session, not in the messages, so it survives compaction by
construction rather than by being summarised well.
"""

from __future__ import annotations

import json
import time

from .. import config, tokens

PLUGIN = {
    "id": "task_list",
    "name": "Task List",
    "description": "The turn's remaining work, kept outside the context window and "
                   "surviving compaction. Off by default.",
    "version": "0.1",
    "permissions": ["storage"],
    "token_budget": 0,      # the schema is small; the injected block is bounded below
    "settings": [
        {"key": "budget", "label": "Injected task-list budget (tokens)",
         "type": "int", "default": 400},
    ],
}

MAX_TASKS = 40
DEFAULT_BUDGET = 400

_S = {"type": "string"}
_I = {"type": "integer"}


def _setting(key: str, default):
    try:
        from .. import plugin_manager
        values = (plugin_manager.settings_of("task_list") or {}).get("values") or {}
        return values.get(key, default)
    except Exception:  # noqa: BLE001
        return default


def _path(session: str):
    return config.HOME / "tasks" / f"{str(session or 'none')[:64]}.json"


def load(session: str) -> list[dict]:
    try:
        rows = json.loads(_path(session).read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save(session: str, rows: list[dict]) -> None:
    path = _path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=1), encoding="utf-8")


def add(session: str, text: str) -> dict:
    text = " ".join(str(text or "").split())
    if not text:
        return {"ok": False, "error": "a task description is required"}
    rows = load(session)
    if any(r["text"].lower() == text.lower() and not r["done"] for r in rows):
        return {"ok": False, "error": "that task is already open"}
    if len(rows) >= MAX_TASKS:
        return {"ok": False, "error": f"task list is full ({MAX_TASKS}); finish or clear some"}
    rows.append({"text": text[:400], "done": False, "ts": round(time.time(), 3)})
    _save(session, rows)
    return {"ok": True, "tasks": rows}


def set_done(session: str, index: int, done: bool = True) -> dict:
    rows = load(session)
    if not (1 <= int(index or 0) <= len(rows)):
        return {"ok": False, "error": f"no task numbered {index}; call tasks(action='list')"}
    rows[int(index) - 1]["done"] = bool(done)
    _save(session, rows)
    return {"ok": True, "tasks": rows}


def clear(session: str, finished_only: bool = True) -> dict:
    rows = load(session)
    kept = [r for r in rows if not (r.get("done") and finished_only)] if finished_only else []
    _save(session, kept)
    return {"ok": True, "tasks": kept, "removed": len(rows) - len(kept)}


def tools() -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "tasks",
            "description": "The remaining work in this session, kept outside your context so it "
                           "survives compaction. action=add (text), done (index), reopen (index), "
                           "list, or clear. Add the steps of a multi-part task before starting, "
                           "and mark each done as you finish it.",
            "parameters": {"type": "object",
                           "properties": {
                               "action": {"type": "string",
                                          "enum": ["add", "done", "reopen", "list", "clear"]},
                               "text": _S, "index": _I},
                           "required": ["action"]}}},
    ]


def render(session: str) -> str:
    """The list as a model reads it back: numbered, open items first."""
    rows = load(session)
    if not rows:
        return ""
    lines = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. [{'x' if r.get('done') else ' '}] {r['text']}")
    open_n = sum(1 for r in rows if not r.get("done"))
    head = (f"Task list for this session — {open_n} open of {len(rows)}. "
            "This is your own record; mark items done as you finish them.")
    return head + "\n" + "\n".join(lines)


def block(session: str) -> dict:
    """What is injected at startup, bounded by the plugin's own budget."""
    text = render(session)
    if not text:
        return {"text": "", "tokens": 0, "count": 0, "open": 0}
    budget = int(_setting("budget", DEFAULT_BUDGET) or DEFAULT_BUDGET)
    rows = load(session)
    if tokens.estimate_tokens(text) > budget:
        # Open items are the point of the list; finished ones are history the
        # transcript already carries. Drop the finished ones first.
        keep = [r for r in rows if not r.get("done")] or rows
        text = render_from(keep)
    return {"text": text, "tokens": tokens.estimate_tokens(text),
            "count": len(rows), "open": sum(1 for r in rows if not r.get("done"))}


def render_from(rows: list[dict]) -> str:
    lines = [f"- [{'x' if r.get('done') else ' '}] {r['text']}" for r in rows]
    open_n = sum(1 for r in rows if not r.get("done"))
    return (f"Task list for this session — {open_n} open of {len(rows)} (finished items "
            "trimmed to stay inside the budget).\n" + "\n".join(lines))


def call(name: str, args: dict, ctx: dict):
    if name != "tasks":
        return None
    args = args or {}
    session = (ctx or {}).get("session") or ""
    action = str(args.get("action") or "list").strip().lower()
    if action == "add":
        res = add(session, args.get("text") or "")
    elif action == "done":
        res = set_done(session, args.get("index") or 0, True)
    elif action == "reopen":
        res = set_done(session, args.get("index") or 0, False)
    elif action == "clear":
        res = clear(session)
    else:
        res = {"ok": True, "tasks": load(session)}
    if not res.get("ok"):
        return f"ERROR: {res.get('error')}"
    return render(session) or "(task list is empty)"

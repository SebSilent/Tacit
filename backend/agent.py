import fnmatch
import inspect
import json
import os
import re
import subprocess
from pathlib import Path

from . import benchmarks, config, extras, mcp_registry, metrics, plugin_manager, skills
from . import tokens as token_mod
from .ai import engine, prompts

DENY_NAMES = {".env", "models.json"}
DENY_PARTS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
BLOCKED_BINARIES = {"git", "gh", "gitk", "tig", "hub"}
MAX_LIST = 300


class ToolError(RuntimeError):
    pass


def _root(project: str | None) -> Path:
    if project:
        p = Path(project).expanduser()
        if p.is_dir():
            return p.resolve()
    return Path(config.USER_HOME)


def _safe(path: str, project: str | None, write: bool = False) -> Path:
    raw = Path(str(path or ".")).expanduser()
    base = _root(project)
    p = (raw if raw.is_absolute() else (base / raw)).resolve()
    if p.name in DENY_NAMES:
        raise ToolError(f"{p.name} is protected and cannot be read or written")
    parts = set(p.parts)
    if parts & DENY_PARTS and not p.is_dir():
        raise ToolError("that path is protected")
    if write and p.is_dir():
        raise ToolError("that path is a directory")
    return p


def _rel(p: Path, project: str | None) -> str:
    try:
        return str(p.relative_to(_root(project))).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _clip(text: str, limit: int | None = None) -> str:
    limit = limit or config.TOOL_OUTPUT_LIMIT
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(truncated, {len(text)} chars total)"


def t_list_files(path: str = ".", project: str | None = None) -> str:
    base = _safe(path, project)
    if base.is_file():
        return f"{_rel(base, project)} is a file ({base.stat().st_size} bytes)"
    if not base.exists():
        return f"ERROR: {path} does not exist"
    rows = []
    for c in sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if c.name in DENY_NAMES or c.name in DENY_PARTS:
            continue
        rows.append(("DIR  " if c.is_dir() else "FILE ") + _rel(c, project))
        if len(rows) >= MAX_LIST:
            rows.append(f"…(more than {MAX_LIST} entries)")
            break
    return "\n".join(rows) or "(empty)"


def t_read_file(path: str, offset: int = 0, limit: int = 2000, project: str | None = None) -> str:
    p = _safe(path, project)
    if not p.exists():
        return f"ERROR: {path} does not exist"
    if p.is_dir():
        return t_list_files(path, project)
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return f"ERROR: {e}"
    start = max(0, int(offset or 0))
    chunk = lines[start:start + max(1, int(limit or 2000))]
    body = "\n".join(f"{start + i + 1}\t{ln}" for i, ln in enumerate(chunk))
    if start + len(chunk) < len(lines):
        body += f"\n…({len(lines)} lines total)"
    return _clip(body)


def t_write_file(path: str, content: str, project: str | None = None) -> str:
    p = _safe(path, project, write=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {_rel(p, project)}"


def t_edit_file(path: str, find: str, replace: str, project: str | None = None) -> str:
    p = _safe(path, project, write=True)
    if not p.exists():
        return f"ERROR: {path} does not exist"
    text = p.read_text(encoding="utf-8", errors="replace")
    n = text.count(find)
    if n == 0:
        return f"ERROR: search text not found in {path}"
    if n > 1:
        return f"ERROR: search text appears {n} times in {path}; make it unique"
    p.write_text(text.replace(find, replace), encoding="utf-8")
    return f"edited {_rel(p, project)} (1 replacement)"


def t_glob_files(pattern: str, project: str | None = None) -> str:
    base = _root(project)
    pat = str(pattern or "").strip()
    if not pat:
        return "ERROR: pattern required"
    try:
        paths = base.glob(pat) if ("/" in pat or "\\" in pat) else base.rglob(pat)
    except (ValueError, OSError) as e:
        return f"ERROR: bad pattern: {e}"
    hits = []
    try:
        for p in paths:
            if not p.is_file():
                continue
            if any(part in DENY_PARTS or part == "node_modules" for part in p.parts):
                continue
            hits.append(_rel(p, project))
            if len(hits) >= MAX_LIST:
                break
    except OSError:
        pass
    if len(hits) >= MAX_LIST:
        return "\n".join(sorted(hits)) + f"\n…(more than {MAX_LIST} matches)"
    return "\n".join(sorted(hits)) or "(no matches)"


def t_grep_files(pattern: str, path: str = ".", project: str | None = None) -> str:
    base = _safe(path, project)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: bad pattern: {e}"
    roots = [base] if base.is_dir() else [base.parent]
    rows = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in DENY_PARTS and not d.startswith(".")]
            for name in filenames:
                f = Path(dirpath) / name
                try:
                    if f.stat().st_size > 2_000_000:
                        continue
                    for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        if rx.search(line):
                            rows.append(f"{_rel(f, project)}:{i}: {line.strip()[:200]}")
                            if len(rows) >= 200:
                                return "\n".join(rows) + "\n…(more matches)"
                except Exception:
                    continue
    return "\n".join(rows) or "(no matches)"


_WRAPPERS = {"env", "sudo", "command", "nohup", "time", "nice", "xargs", "doas"}


def _blocked_shell(command: str) -> bool:
    """True if any command in the line invokes a version-control binary.

    Checks every command position (the start of the line and of each `&&`,
    `||`, `;`, `|` segment, after wrappers like sudo/env), so `<vcs>`, `x &&
    <vcs>`, pipes, subshells and absolute paths are all caught — while ordinary
    arguments that merely mention the word (e.g. `grep foo .`) are not.
    A guardrail that keeps the model out of version control, not a jail.
    """
    for segment in re.split(r"[&|;\n]+", str(command or "")):
        words = [w.strip().strip("'\"") for w in re.split(r"[\s()<>`]+", segment)]
        words = [w for w in words if w]
        i = 0
        while i < len(words) and (words[i].lower() in _WRAPPERS or re.match(r"^\w+=", words[i])):
            i += 1
        if i >= len(words):
            continue
        base = os.path.basename(words[i]).lower()
        if base in BLOCKED_BINARIES or base.rsplit(".", 1)[0] in BLOCKED_BINARIES:
            return True
    return False


def t_run_shell(command: str, project: str | None = None, timeout: int | None = None) -> str:
    cmd = str(command or "").strip()
    if not cmd:
        return "ERROR: empty command"
    if not config.allow_vcs() and _blocked_shell(cmd):
        return ("version control is turned off for the agent in this workspace. Use the Git panel "
                "for status, commits, branches, push and pull, or turn the agent's access on in "
                "Settings > Tools.")
    cwd = _root(project)
    try:
        r = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True,
                           timeout=max(1, min(int(timeout or config.SHELL_TIMEOUT), 600)))
    except subprocess.TimeoutExpired:
        return f"ERROR: timed out after {timeout or config.SHELL_TIMEOUT}s"
    except Exception as e:
        return f"ERROR: {e}"
    out = r.stdout or ""
    if r.stderr:
        out += ("\n[stderr]\n" if out else "") + r.stderr
    return _clip(f"exit code {r.returncode}\n{out.strip()}")


def t_bg_start(command: str, project: str | None = None, cwd: str | None = None) -> str:
    return extras.bg_start(command, project=project, cwd=cwd)


def t_bg_output(id: str, tail: int = 4000) -> str:
    return extras.bg_output(id, tail)


def t_bg_stop(id: str) -> str:
    return extras.bg_stop(id)


def t_fetch(url: str, max_chars: int | None = None) -> str:
    return extras.fetch(url, max_chars)


def t_snapshot(label: str = "", project: str | None = None) -> str:
    return extras.snapshot(project or str(config.USER_HOME), label)


def t_list_snapshots(project: str | None = None) -> str:
    return extras.list_snapshots()


def t_restore(name: str, project: str | None = None) -> str:
    return extras.restore(name, project or str(config.USER_HOME))


def t_browser(action: str, url: str = "", selector: str = "", text: str = "",
              path: str = "", full: bool = False, project: str | None = None) -> str:
    from . import browser as B
    act = str(action or "").strip().lower()
    if act == "open":
        return B.open_page(url)
    if act == "text":
        return B.page_text(selector)
    if act == "links":
        return B.page_links()
    if act == "click":
        return B.click(selector)
    if act == "type":
        return B.type_into(selector, text)
    if act == "screenshot":
        return B.screenshot(path, full)
    if act == "close":
        return B.close()
    return "ERROR: action must be open|text|links|click|type|screenshot|close"


def t_evidence(action: str, source: str = "", note: str = "", snippet: str = "",
               id: str = "", project: str | None = None) -> str:
    from . import evidence as E
    act = str(action or "").strip().lower()
    if act == "add":
        return E.add(source, note, snippet)
    if act == "get":
        return E.get(id)
    if act in ("list", ""):
        return E.listing()
    return "ERROR: action must be add|get|list"


def t_benchmark(action: str, model: str = "", temperature=None, max_tokens=None,
                context_budget=None, project: str | None = None) -> str:
    from . import benchmarks as B
    act = str(action or "").strip().lower()
    if act == "list":
        return B.listing()
    if act == "set":
        return B.set_profile(model, temperature, max_tokens, context_budget)
    return "ERROR: action must be list|set"


def t_research(question: str, project: str | None = None) -> str:
    from . import research as R
    return R.research(question, project=project)


def t_skill(name: str, project: str | None = None) -> str:
    return skills.read(name)


def run_terminal(command: str, project: str | None = None, timeout: int | None = None) -> dict:
    cmd = str(command or "").strip()
    cwd = _root(project)
    if not cmd:
        return {"ok": False, "error": "empty command", "cwd": str(cwd), "shell": config.SHELL}
    limit = max(1, min(int(timeout or config.SHELL_TIMEOUT), 600))
    try:
        r = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {limit}s", "cwd": str(cwd), "shell": config.SHELL}
    except Exception as e:
        return {"ok": False, "error": str(e), "cwd": str(cwd), "shell": config.SHELL}
    return {"ok": True, "code": r.returncode, "stdout": r.stdout or "", "stderr": r.stderr or "",
            "cwd": str(cwd), "shell": config.SHELL}


# ── MCP helper tools ──────────────────────────────────────────────────────
# These four are always available but tiny. The real MCP tool schemas are only
# injected when the agent activates them (or the user pins them), which is what
# keeps a busy MCP server from inflating the always-on prompt.

def t_mcp_list_servers() -> str:
    rows = mcp_registry.list_servers()
    if not rows:
        return ("No MCP servers configured. Add one in Settings → MCP. Its tools are "
                "discovered but stay out of your prompt until activated.")
    lines = []
    for s in rows:
        state = s["state"] + ("" if s["enabled"] else ", disabled")
        cost = token_mod.label(s["tools_tokens"])
        lines.append(f"- {s['id']}: {s['name']} [{state}] {s['tool_count']} tool(s), "
                     f"{cost} tokens if all injected")
        if s.get("error"):
            lines.append(f"    error: {s['error']}")
    return "\n".join(lines)


def t_mcp_search_tools(query: str = "", limit: int = 8) -> str:
    hits = mcp_registry.search(query, limit)
    if not hits:
        return "No MCP tools match that. Call mcp_list_servers to see what is connected."
    lines = []
    for e in hits:
        lines.append(f"- {e['name']} [{e['server_id']}] {token_mod.label(e['tokens'])} tokens, "
                     f"{e['danger']} risk\n  key: {e['key']}\n  {(e['description'] or '')[:200]}")
    lines.append("\nCall mcp_call(server_id, tool_name, arguments) to use one, or "
                 "mcp_activate_tools([key]) to make its schema callable for a few turns.")
    return "\n".join(lines)


def t_mcp_activate_tools(tool_keys=None, ttl_turns: int = 3) -> str:
    if isinstance(tool_keys, str):
        tool_keys = [tool_keys]
    res = mcp_registry.activate(list(tool_keys or []), ttl_turns)
    parts = [f"activated {len(res['activated'])} tool(s) for {ttl_turns} turn(s): "
             f"{', '.join(res['activated']) or 'none'}"]
    if res.get("unknown"):
        parts.append(f"unknown keys: {', '.join(map(str, res['unknown']))}")
    report = mcp_registry.injection_report()
    parts.append(f"MCP schemas now cost {token_mod.label(report['injected_tokens'])} tokens "
                 f"instead of {token_mod.label(report['discovered_tokens'])}.")
    return "\n".join(parts)


def t_mcp_call(server_id: str = "", tool_name: str = "", arguments=None,
               confirm: bool = False) -> str:
    if not server_id or not tool_name:
        return "ERROR: server_id and tool_name are required"
    res = mcp_registry.call(server_id, tool_name, arguments or {}, confirmed=bool(confirm))
    if not res.get("ok"):
        return f"ERROR: {res.get('error')}"
    return _clip(res.get("result") or "", config.TOOL_OUTPUT_LIMIT)


MCP_HELPER_NAMES = ("mcp_list_servers", "mcp_search_tools", "mcp_activate_tools", "mcp_call")


EXECS = {
    "list_files": t_list_files,
    "read_file": t_read_file,
    "write_file": t_write_file,
    "edit_file": t_edit_file,
    "glob_files": t_glob_files,
    "grep_files": t_grep_files,
    "run_shell": t_run_shell,
    "bg_start": t_bg_start,
    "bg_output": t_bg_output,
    "bg_stop": t_bg_stop,
    "fetch": t_fetch,
    "snapshot": t_snapshot,
    "list_snapshots": t_list_snapshots,
    "restore": t_restore,
    "browser": t_browser,
    "evidence": t_evidence,
    "benchmark": t_benchmark,
    "research": t_research,
    "skill": t_skill,
    "mcp_list_servers": t_mcp_list_servers,
    "mcp_search_tools": t_mcp_search_tools,
    "mcp_activate_tools": t_mcp_activate_tools,
    "mcp_call": t_mcp_call,
}

READONLY_BLOCKED = {"write_file", "edit_file", "run_shell", "bg_start", "bg_stop", "restore"}


def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_S = {"type": "string"}
_I = {"type": "integer"}

TOOLS = [
    _fn("list_files", "List files and folders. Relative paths resolve against the working project.",
        {"path": _S}, []),
    _fn("read_file", "Read a file with line numbers.",
        {"path": _S, "offset": _I, "limit": _I}, ["path"]),
    _fn("write_file", "Create or overwrite a file.", {"path": _S, "content": _S}, ["path", "content"]),
    _fn("edit_file", "Replace a snippet that appears exactly once in a file.",
        {"path": _S, "find": _S, "replace": _S}, ["path", "find", "replace"]),
    _fn("glob_files", "Find files by name pattern, e.g. '*.py'.", {"pattern": _S}, ["pattern"]),
    _fn("grep_files", "Search file contents with a regular expression.",
        {"pattern": _S, "path": _S}, ["pattern"]),
    _fn("run_shell", "Run a shell command from the working project directory. Returns exit code, "
                     "stdout and stderr.", {"command": _S, "timeout": _I}, ["command"]),
    _fn("skill", "Read a skill's full instructions. Skills are listed by name and description in "
                 "your system prompt; call this before following one.", {"name": _S}, ["name"]),
    _fn("bg_start", "Start a long-running command in the background (dev server, watcher, slow "
                    "build). Returns an id you can poll.", {"command": _S, "cwd": _S}, ["command"]),
    _fn("bg_output", "Read a background job's output and whether it is still running.",
        {"id": _S, "tail": _I}, ["id"]),
    _fn("bg_stop", "Stop a background job.", {"id": _S}, ["id"]),
    _fn("fetch", "Fetch a URL and return its text (HTML is reduced to readable text). Use it to "
                 "read documentation instead of guessing.", {"url": _S, "max_chars": _I}, ["url"]),
    _fn("snapshot", "Save a copy of the working project so changes can be undone. Take one "
                    "before risky edits.", {"label": _S}, []),
    _fn("list_snapshots", "List saved snapshots, newest first.", {}, []),
    _fn("restore", "Restore the project from a snapshot, overwriting files with the saved "
                   "copies.", {"name": _S}, ["name"]),
    _fn("browser", "Drive a real browser. action=open|text|links|click|type|screenshot|close. "
                   "open needs url; click/type need selector; type needs text. Use it for pages "
                   "that need JavaScript - plain pages are faster with fetch.",
        {"action": _S, "url": _S, "selector": _S, "text": _S, "path": _S, "full": {"type": "boolean"}},
        ["action"]),
    _fn("research", "Investigate a question across several sources and return a cited report "
                    "with a section on what could not be confirmed. Slower than a plain fetch.",
        {"question": _S}, ["question"]),
    _fn("evidence", "Record a citable fact (action=add with source/note/snippet), read one back "
                    "(get, by id) or list what is recorded.",
        {"action": _S, "source": _S, "note": _S, "snippet": _S, "id": _S}, ["action"]),
    _fn("benchmark", "Per-model settings. action=list, or action=set with model plus "
                     "temperature / max_tokens / context_budget.",
        {"action": _S, "model": _S, "temperature": {"type": "number"},
         "max_tokens": _I, "context_budget": _I}, ["action"]),
    _fn("task", "Delegate a focused investigation to a sub-agent with its own fresh context. "
                "Use it to map or search a codebase without filling your own context — only "
                "its report comes back.", {"prompt": _S}, ["prompt"]),
    _fn("mcp_list_servers", "List configured MCP servers: state, tool count and what their "
        "tools would cost if injected. Nothing is injected by default.", {}, []),
    _fn("mcp_search_tools", "Search the tool catalogue of every connected MCP server by "
        "intent, e.g. 'open a github issue'. Returns matches with an activation key.",
        {"query": _S, "limit": _I}, ["query"]),
    _fn("mcp_activate_tools", "Make MCP tools callable for a few turns by injecting their "
        "schemas. Pass keys from mcp_search_tools. They deactivate on their own.",
        {"tool_keys": {"type": "array", "items": _S}, "ttl_turns": _I}, ["tool_keys"]),
    _fn("mcp_call", "Call an MCP tool. Prefer this when you already know what you need — it "
        "costs no schema tokens. Destructive tools require confirm=true.",
        {"server_id": _S, "tool_name": _S,
         "arguments": {"type": "object"}, "confirm": {"type": "boolean"}},
        ["server_id", "tool_name"]),
]

SUBTASK = "task"


def tools_for(readonly: bool = False, depth: int = 0) -> list[dict]:
    rows = list(TOOLS)
    disabled = set(config.prefs().get("disabledTools") or [])
    if disabled:
        rows = [t for t in rows if t["function"]["name"] not in disabled]
    if readonly:
        rows = [t for t in rows if t["function"]["name"] not in READONLY_BLOCKED]
    if depth >= config.SUBAGENT_MAX_DEPTH:
        rows = [t for t in rows if t["function"]["name"] != SUBTASK]

    # Extra capabilities are *appended*, never baked into the base list:
    # plugin tools only from enabled plugins, MCP schemas only when activated or
    # pinned (unless the user turned on direct mode). Failures here must never
    # break an ordinary turn.
    try:
        rows.extend(plugin_manager.collect_tools())
    except Exception:  # noqa: BLE001
        pass
    try:
        rows.extend(mcp_registry.schemas_for_prompt())
    except Exception:  # noqa: BLE001
        pass
    return rows


def _size(msg: dict) -> int:
    try:
        return len(json.dumps(msg, ensure_ascii=False))
    except Exception:
        return len(str(msg))


def windowed(messages: list[dict], budget: int) -> list[dict]:
    if sum(_size(m) for m in messages) <= budget:
        return messages
    head = messages[0]
    rest = messages[1:]
    kept, total = [], 0
    for m in reversed(rest):
        sz = _size(m)
        if kept and total + sz > budget:
            break
        kept.append(m)
        total += sz
    kept.reverse()
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)
    note = {"role": "system", "content":
            "[Earlier steps were elided to conserve context. Continue from the recent context "
            "using your tools — do NOT restart the task.]"}
    return [head, note] + kept


def parse_args(raw: str) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


def _chars(msgs: list[dict]) -> int:
    return sum(len(m.get("content") or "") for m in msgs)


def should_compact(rec: dict) -> bool:
    msgs = rec.get("messages") or []
    if len(msgs) <= config.COMPACT_KEEP_TAIL + 1:
        return False
    ctx = (rec.get("usage") or {}).get("context") or {}
    window, tokens = ctx.get("window") or 0, ctx.get("tokens") or 0
    if window and tokens:
        return tokens > window * config.COMPACT_AT
    return _chars(msgs) > int(config.AGENT_CONTEXT_BUDGET * config.COMPACT_AT)


def compact_history(rec: dict, ref: str | None = None, force: bool = False) -> dict | None:
    msgs = rec.get("messages") or []
    if not force and not should_compact(rec):
        return None
    keep = config.COMPACT_KEEP_TAIL
    if len(msgs) <= keep + 1:
        return None

    old, tail = msgs[:-keep], msgs[-keep:]
    transcript = "\n\n".join(
        f"{m.get('role', 'user')}: {(m.get('content') or '')[:2000]}" for m in old)
    try:
        res = engine.chat([
            {"role": "system", "content": prompts.COMPACT},
            {"role": "user", "content": transcript},
        ], ref=ref)
    except engine.EngineError as e:
        return {"compacted": 0, "error": str(e)}

    summary = (res.get("content") or "").strip()
    if not summary:
        return None

    before = _chars(msgs)
    rec["messages"] = [{"role": "summary", "content": summary}] + tail
    return {
        "compacted": len(old), "kept": len(tail),
        "chars_before": before, "chars_after": _chars(rec["messages"]),
    }


def run_subagent(task: str, ctx: dict, out: dict):
    depth = int(ctx.get("depth") or 0)
    if depth >= config.SUBAGENT_MAX_DEPTH:
        out["result"] = "ERROR: sub-agents cannot delegate further"
        return
    if not (task or "").strip():
        out["result"] = "ERROR: prompt required"
        return

    messages = [
        {"role": "system", "content": prompts.system_prompt(ctx.get("project"), False, subagent=True)},
        {"role": "user", "content": task},
    ]
    report: list[str] = []
    for ev in run_turn(messages, project=ctx.get("project"), ref=ctx.get("ref"),
                       max_steps=config.SUBAGENT_MAX_STEPS, depth=depth + 1,
                       readonly=True, nested=True):
        kind = ev.get("type")
        if kind == "text":
            report.append(ev["delta"])
        elif kind in ("tool_start", "tool_end", "notify"):
            yield {**ev, "subagent": True}

    text = "".join(report).strip() or "(the sub-agent returned no report)"
    out["result"] = _clip(text, config.SUBAGENT_RESULT_LIMIT)


def call_tool(name: str, args: dict, ctx: dict, out: dict):
    if name == SUBTASK:
        yield from run_subagent(args.get("prompt", ""), ctx, out)
        return
    if name.startswith("mcp__"):
        entry = next((e for e in mcp_registry.all_tools() if e["fn_name"] == name), None)
        if entry is None:
            out["result"] = f"ERROR: unknown MCP tool '{name}' — it may have deactivated"
            return
        res = mcp_registry.call(entry["server_id"], entry["name"], args or {})
        out["result"] = res.get("result") if res.get("ok") else f"ERROR: {res.get('error')}"
        return
    fn = EXECS.get(name)
    if not fn:
        plugin_result = plugin_manager.call_tool(name, args, ctx)
        if plugin_result is not None:
            out["result"] = str(plugin_result)
            return
        out["result"] = f"ERROR: unknown tool '{name}'"
        return
    if "project" not in args and "project" in inspect.signature(fn).parameters:
        args = {**args, "project": ctx.get("project")}
    try:
        out["result"] = str(fn(**args))
    except ToolError as e:
        out["result"] = f"ERROR: {e}"
    except TypeError as e:
        out["result"] = f"ERROR: bad arguments for {name}: {e}"
    except Exception as e:
        out["result"] = f"ERROR: {e}"


def run_turn(messages: list[dict], *, project: str | None, readonly: bool = False,
             max_steps: int | None = None, ref: str | None = None, temperature=None,
             depth: int = 0, nested: bool = False, chat: bool = False,
             stop=None, steer=None, reasoning=None):
    steps = max_steps or config.AGENT_MAX_STEPS
    tools = [] if chat else tools_for(readonly, depth)
    ctx = {"project": project, "ref": ref, "depth": depth}
    prof = benchmarks.profile_for(ref)
    if temperature is None and prof.get("temperature") is not None:
        temperature = prof["temperature"]
    prof_tokens = prof.get("max_tokens")

    for _ in range(steps):
        if stop is not None and stop.is_set():
            yield {"type": "notify", "message": "stopped", "level": "info"}
            yield {"type": "done"}
            return
        if steer:
            while steer:
                extra = steer.pop(0)
                if (extra or "").strip():
                    messages.append({"role": "user", "content": extra})
                    yield {"type": "notify", "message": "steering applied", "level": "info"}
        calls = []
        model_used = None
        cancelled = False
        try:
            stream = engine.stream_chat(windowed(messages, config.AGENT_CONTEXT_BUDGET),
                                        ref=ref, tools=tools, temperature=temperature,
                                        max_tokens=prof_tokens, reasoning_effort=reasoning)
            for ev in stream:
                if stop is not None and stop.is_set():
                    cancelled = True
                    break
                if ev["type"] == "text":
                    yield {"type": "text", "delta": ev["delta"]}
                elif ev["type"] == "reason":
                    yield {"type": "reason", "delta": ev["delta"]}
                elif ev["type"] == "tool_calls":
                    calls = ev["calls"]
                elif ev["type"] == "usage":
                    yield {"type": "usage", "usage": ev["usage"]}
                elif ev["type"] == "done":
                    model_used = ev.get("model")
        except engine.EngineError as e:
            yield {"type": "error", "message": str(e)}
            yield {"type": "done"}
            return

        if cancelled:
            yield {"type": "notify", "message": "stopped", "level": "info"}
            yield {"type": "done"}
            return

        if model_used and not nested:
            yield {"type": "state_delta", "model": model_used["ref"]}

        if not calls:
            yield {"type": "done"}
            return

        assistant = {"role": "assistant", "content": "", "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
            for c in calls]}
        messages.append(assistant)

        for c in calls:
            if stop is not None and stop.is_set():
                yield {"type": "notify", "message": "stopped", "level": "info"}
                yield {"type": "done"}
                return
            args = parse_args(c["arguments"])
            yield {"type": "tool_start", "name": c["name"], "args": args, "id": c["id"]}
            out: dict = {}
            for p in call_tool(c["name"], args, ctx, out):
                yield p
            result = out.get("result", "ERROR: the tool produced no result")
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
            yield {"type": "tool_end", "name": c["name"], "result": result, "id": c["id"]}

    yield {"type": "notify", "message": f"stopped after {steps} steps", "level": "warn"}
    yield {"type": "done"}

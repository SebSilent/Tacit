import collections
import fnmatch
import inspect
import json
import os
import queue
import re
import subprocess
import time
import threading
from pathlib import Path

from . import (audit, config, extras, guidance, mcp_registry, metrics, model_settings,
               plugin_manager, sandbox, skills)
from . import tokens as token_mod
from .ai import engine, prompts

DENY_NAMES = {".env", "models.json"}
DENY_PARTS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
BLOCKED_BINARIES = {"git", "gh", "gitk", "tig", "hub"}
MAX_LIST = 300
MAX_GREP = 200


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


# Commands whose only job is to print a file. Piping or grepping disqualifies a
# command from the hint, because there the cut is in a derived result, not in a source.
_DUMPERS = ("cat", "head", "tail", "less", "more", "sed")
_FILE_TOKEN = re.compile(r"[A-Za-z0-9_./\\-]+\.[A-Za-z]{1,6}")


def _dumped_file(cmd: str) -> str:
    """The file a shell command is plainly printing, or '' if it is not just printing."""
    parts = str(cmd or "").split()
    if not parts:
        return ""
    # Anything piped or redirected is a derived result, and the cut in it is not the
    # file's length. Pointing at read_file there would send the agent away from the
    # search it was actually running.
    if any(tok in ("|", ">", ">>", "&&", ";", "2>") for tok in parts):
        return ""
    verb = parts[0].rsplit("/", 1)[-1].lower()
    if verb not in _DUMPERS:
        return ""
    if verb == "sed" and not any(p.startswith("-n") for p in parts[1:3]):
        return ""
    for tok in parts[1:]:
        if tok.startswith("-"):
            continue
        if _FILE_TOKEN.fullmatch(tok):
            return tok
    return ""


def _dump_hint(cmd: str, clipped: str) -> str:
    """Point a clipped file dump at the tool that can deliver the file whole.

    The transcript used to imply that a 17,578-character source file was 6,000 characters
    long, and the measured response was to keep re-probing it with other commands instead
    of ever reading it. `read_file` now returns files whole, so the hint names it.
    """
    name = _dumped_file(cmd)
    if not name or "truncated," not in clipped:
        return ""
    return (f"\n{name} is longer than this result, so it is cut off here. Call read_file "
            f"with path='{name}' to get the whole file (up to "
            f"{config.READ_OUTPUT_LIMIT:,} characters) instead of paging it through the shell.")


def _last_line_number(text: str, fallback: int) -> int:
    """The highest `N\t` prefix in a read_file body, so a clipped read can say
    which line it actually reached rather than which line was asked for."""
    for line in reversed((text or "").splitlines()):
        head, sep, _ = line.partition("\t")
        if sep and head.isdigit():
            return int(head)
    return fallback


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
    if lines and start >= len(lines):
        return (f"ERROR: offset {start} is past the end of {_rel(p, project)} "
                f"({len(lines)} lines)")
    chunk = lines[start:start + max(1, int(limit or 2000))]
    body = "\n".join(f"{start + i + 1}\t{ln}" for i, ln in enumerate(chunk))
    clipped = _clip(body, max(200, config.READ_OUTPUT_LIMIT - 96))
    last = _last_line_number(clipped, start + len(chunk))
    remaining = len(lines) - last
    if remaining > 0:
        tail = (f"\n…(lines {start + 1}-{last} of {len(lines)} shown; "
                f"pass offset={last} for the next {remaining})")
    else:
        tail = f"\n…(lines {start + 1}-{last} of {len(lines)} shown; end of file)"
    return clipped + tail


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


def _files_under(base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in DENY_PARTS and not d.startswith(".")]
        for name in filenames:
            yield Path(dirpath) / name


def t_grep_files(pattern: str, path: str = ".", project: str | None = None) -> str:
    base = _safe(path, project)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: bad pattern: {e}"
    if not base.exists():
        return f"ERROR: {path} does not exist"
    # A file path means that file. The old fallback searched its parent instead,
    # so grepping one module returned matches from its neighbours and a mistyped
    # path silently searched somewhere else entirely. Both read as real answers.
    files = [base] if base.is_file() else _files_under(base)
    rows, full = [], False
    for f in files:
        try:
            if f.stat().st_size > 2_000_000:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                rows.append(f"{_rel(f, project)}:{i}: {line.strip()[:200]}")
                if len(rows) >= MAX_GREP:
                    full = True
                    break
        if full:
            break
    body = "\n".join(rows) or "(no matches)"
    if full:
        body += ("\n…(more matches; the list is capped — narrow the pattern or pass a "
                 "subdirectory)")
    return _clip(body)


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


def t_run_shell(command: str, project: str | None = None, timeout: int | None = None,
                session: str = "") -> str:
    cmd = str(command or "").strip()
    if not cmd:
        return "ERROR: empty command"
    if not config.allow_vcs() and _blocked_shell(cmd):
        return ("version control is turned off for the agent in this workspace. Use the Git panel "
                "for status, commits, branches, push and pull, or turn the agent's access on in "
                "Settings > Tools.")
    # Everything runs through the sandbox layer, so which backend ran it, and what
    # it changed, is recorded whether or not isolation was in force.
    res = sandbox.run(cmd, project=project, timeout=timeout, session=session)
    if not res.get("ok"):
        err = (res.get("stderr") or "ERROR: the command did not run").strip()
        if "timed out" in err:
            # A killed command may already have done something, and the obvious
            # next move — run it again — is the one that hangs again. Say so here,
            # where the model is reading it, rather than only in the step guidance.
            err += ("\nIt was killed, not finished: it may have partial side effects, and "
                    "re-running the same command will usually hang the same way. On Windows a "
                    "piped find/findstr/more can block on a full pipe buffer — use a different "
                    "command shape, or pass a longer timeout if the work is genuinely slow.")
        return err
    out = res.get("stdout") or ""
    if res.get("stderr"):
        out += ("\n[stderr]\n" if out else "") + res["stderr"]
    body = f"exit code {res.get('code', 0)}\n{out.strip()}"
    if res.get("backend") not in ("", "none"):
        changed = res.get("changed") or {}
        if changed.get("count"):
            names = (changed.get("added", []) + changed.get("modified", [])
                     + changed.get("removed", []))[:12]
            body += f"\n\n[{res['backend']}] {changed['count']} file(s) changed: " + \
                    ", ".join(names)
        for note in (res.get("notes") or [])[:2]:
            body += f"\n[{res['backend']}] {note}"
    clipped = _clip(body)
    hint = _dump_hint(cmd, clipped)
    return clipped + hint if hint else clipped


def t_bg_start(command: str, project: str | None = None, cwd: str | None = None,
               session: str = "") -> str:
    return extras.bg_start(command, project=project, cwd=cwd, session=session)


def t_bg_output(id: str, tail: int = 4000) -> str:
    return extras.bg_output(id, tail)


def t_bg_stop(id: str) -> str:
    return extras.bg_stop(id)


def t_fetch(url: str, max_chars: int | None = None) -> str:
    return extras.fetch(url, max_chars)


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


def run_research(args: dict, ctx: dict, out: dict, parent_call_id: str = ""):
    """The research tool, driven like a sub-agent: events stream, result lands.

    The old version ran the whole investigation and returned one string, which
    meant the interface saw nothing until the report was finished — minutes of
    tool calls with no tool_start, no notify, nothing. This yields the aspects'
    events as they happen, tagged `research: True`, and puts the report in
    ``out["result"]`` at the end, exactly as `run_subagent` does for `task`.
    """
    from . import research as R
    report: list = []
    for ev in R.research(str(args.get("question") or ""), project=ctx.get("project"),
                         ref=ctx.get("ref")):
        if isinstance(ev, dict):
            yield {**ev, "research": True, "parent_call_id": parent_call_id}
        else:
            report.append(str(ev))
    out["result"] = "".join(report) or "ERROR: research produced nothing"


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
    "browser": t_browser,
    "evidence": t_evidence,
    "research": None,         # dispatched through run_research (see call_tool)
    "skill": t_skill,
    "mcp_list_servers": t_mcp_list_servers,
    "mcp_search_tools": t_mcp_search_tools,
    "mcp_activate_tools": t_mcp_activate_tools,
    "mcp_call": t_mcp_call,
}

READONLY_BLOCKED = {"write_file", "edit_file", "run_shell", "bg_start", "bg_stop"}

# Read-only callers also lose these. Filtering the schema by name is not the whole
# job: `mcp_call` reaches any external tool including destructive ones, and it can
# be given confirm=true by the model itself, with nobody to ask. `task` opens an
# unbounded nested delegation from a panel that is advertised as a cheap side
# conversation. (Snapshots are not here because they are not agent tools at all:
# the agent's undo is automatic, and restore belongs to the person.)
READONLY_BLOCKED |= {"task", "mcp_call", "mcp_activate_tools"}

# Tools that read in one action and write in another. The guard below refuses the
# writing action rather than dropping the tool, so `evidence get` still works for
# a read-only caller.
READONLY_BLOCKED_ACTIONS = {"evidence": {"add"}}


def readonly_guard(name: str, args: dict) -> str:
    """An error string if a read-only caller may not make this call, else ''.

    Hiding a schema does not stop a call: a model that remembers the name can
    still emit it, and `call_tool` will run it. The interface offers a switch
    labelled "read-only tools" and the Assistant's own prompt tells the model it
    cannot change anything, so the refusal has to happen at the call as well.
    """
    if name in READONLY_BLOCKED:
        return f"ERROR: '{name}' is not available to a read-only caller"
    blocked = READONLY_BLOCKED_ACTIONS.get(name)
    if blocked and str((args or {}).get("action") or "").strip().lower() in blocked:
        return (f"ERROR: {name} action='{(args or {}).get('action')}' writes settings, "
                "which a read-only caller may not do")
    return ""


def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_S = {"type": "string"}
_I = {"type": "integer"}

# Said only where it is true. On cmd.exe a multi-line quoted body runs, exits 0 and does
# nothing, which is the worst failure an agent can meet: the tool reports success.
_SHELL_NOTE = ("A quoted command cannot span lines here, so write a script file and run it."
               if config.OS == "Windows" else "")

TOOLS = [
    _fn("list_files", "List files and folders. Relative paths resolve against the working project.",
        {"path": _S}, []),
    _fn("read_file", "Read a file as numbered lines. offset is a 0-based line index; the result "
                     "reports the range it showed and the offset to continue from.",
        {"path": _S, "offset": _I, "limit": _I}, ["path"]),
    _fn("write_file", "Create or overwrite a file.", {"path": _S, "content": _S}, ["path", "content"]),
    _fn("edit_file", "Replace a snippet that appears exactly once in a file.",
        {"path": _S, "find": _S, "replace": _S}, ["path", "find", "replace"]),
    _fn("glob_files", "Find files by name pattern, e.g. '*.py'.", {"pattern": _S}, ["pattern"]),
    _fn("grep_files", "Search file contents with a regular expression. path may be a directory "
                      "or a single file.",
        {"pattern": _S, "path": _S}, ["pattern"]),
    _fn("run_shell", f"Run a shell command from the working project directory, in "
                     f"{config.SHELL_KIND} on {config.OS}. Returns exit code, stdout and "
                     f"stderr. {_SHELL_NOTE}", {"command": _S, "timeout": _I}, ["command"]),
    _fn("skill", "Read a skill's full instructions. Skills are listed by name and description in "
                 "your system prompt; call this before following one.", {"name": _S}, ["name"]),
    _fn("bg_start", "Start a long-running command in the background (dev server, watcher, slow "
                    "build). Returns an id you can poll.", {"command": _S, "cwd": _S}, ["command"]),
    _fn("bg_output", "Read a background job's output and whether it is still running.",
        {"id": _S, "tail": _I}, ["id"]),
    _fn("bg_stop", "Stop a background job.", {"id": _S}, ["id"]),
    _fn("fetch", "Fetch a URL and return its text (HTML is reduced to readable text). Use it to "
                 "read documentation instead of guessing.", {"url": _S, "max_chars": _I}, ["url"]),
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
    _fn("task", "Delegate a focused investigation to a sub-agent with its own fresh context. "
                "Use it to map or search a codebase without filling your own context — only "
                "its report comes back, so ask one narrow question per delegation.",
        {"prompt": _S}, ["prompt"]),
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

# Tools whose first use in a turn triggers an automatic snapshot. `run_shell` is
# included: a shell command can rewrite the tree, and a snapshot of this project
# costs about half a second, once per turn. The sandbox layer still reports every
# file a command changed.
MUTATING = {"write_file", "edit_file", "run_shell"}

# Tools the repeat guard never caches: a repeated write may be a recovery, and
# silently skipping a mutation that was asked for would make the harness lie
# about what it did to the project. `run_shell` is deliberately NOT exempt —
# the guard exists because a cell spent 26 of 29 shell calls on refused
# repeats, so an identical command is a loop, not a recovery. Kept separate
# from MUTATING because "triggers a snapshot" is not "may loop freely".
REPEAT_EXEMPT = {"write_file", "edit_file"}

# How many times the same call with the same arguments may actually run in one turn before it is
# answered from cache instead. Two is "you are checking twice"; four is the feal loop, where 59
# shell calls had 10 distinct argument sets and the turn ended with nothing written.
REPEAT_BLOCK_AFTER = 3
REPEAT_ECHO = 1500
# Blocked repeats in a row before the turn stops trying. The cell that started this one spent 26 of
# its 29 shell calls on refused repeats and never reached a file: once the model has been told four
# times that a call is cached, the remaining steps cannot buy new information, so the turn ends
# early and spends its last round on the deliverable instead of counting down to the cap.
REPEAT_STOP_AFTER = 4
# The deliverable phase gets a few rounds, not one: writing the file is step one, and on a task like
# the Scheme evaluator the model then needs to run it and fix what the run shows. Bounded, because
# an unbounded "one more chance" is how a turn becomes two turns.
SALVAGE_ROUNDS = 3
SALVAGE_TOOLS = ("write_file", "edit_file", "run_shell")
_REPEAT_REPLY = (
    "BLOCKED: {tool} has already run with exactly these arguments {n} times this turn, and it "
    "will not run again. Its result was:\n\n{prev}\n\nThat answer has not changed. Do something "
    "different: change the arguments, or write down what you have and use it.{missing}")

# This reminder rides on the blocked result rather than only on the guidance block, because the
# guidance block fires once and is then suppressed as a duplicate three steps before the cap.
# The cell that started this one was told once and spent 26 more calls not writing the file.
_MISSING_IN_REPLY = ("\n\n{names} is what this task asks you to produce, and it is not written "
                     "yet. Write it now with write_file; describing it in your final answer "
                     "scores nothing.")

# A file name in the task text that is not on disk yet is the thing being asked for. This is the
# cheapest possible completion signal, and it is the one a 30-step turn can act on: a cell that
# derives the right answer and never writes it down scores zero, because the checker reads files.
_ARTIFACT = re.compile(
    r"(?<!\w)[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh|js|ts|tsx|jsx|c|h|cpp|go|rs|java|scm|txt"
    r"|json|jsonl|md|yaml|yml|toml|html|css|sql)\b")


def deliverables(text: str, project: str | None) -> list:
    """File names the task text names that do not exist in the project yet.

    Inputs are excluded by the same rule that makes this useful: a named file already on disk is
    something to read or edit, not something still to produce.
    """
    if not text:
        return []
    out, seen = [], set()
    for name in _ARTIFACT.findall(text):
        base = name.rsplit("/", 1)[-1]
        if base in seen:
            continue
        seen.add(base)
        if project and (os.path.exists(os.path.join(project, name))
                        or os.path.exists(os.path.join(project, base))):
            continue
        out.append(name)
    return out[:4]


def _auto_snapshot(project: str, tool: str, session: str = "") -> str:
    """Snapshot the project once, before the first edit of a turn.

    The README promises the agent takes a snapshot before risky edits. Asking it
    to in the tool description is not the same thing: across two real sessions the
    model never once called `snapshot`, so the undo timeline was empty at exactly
    the moment it would have been needed. Once per turn keeps it cheap, and a
    failure here must never cost the edit.
    """
    try:
        out = extras.snapshot(project, f"auto: before {tool}", session=session)
    except Exception as exc:  # noqa: BLE001
        return f"could not take an automatic snapshot ({exc}); edits are not undoable"
    audit.record("auto_snapshot", session=session, tool=tool, backend="snapshot",
                 status="ok" if not str(out).startswith("ERROR") else "error",
                 detail=str(out)[:200])
    return f"snapshot taken before the first edit: {str(out)[:120]}"


def tools_for(readonly: bool = False, depth: int = 0) -> list[dict]:
    rows = list(TOOLS)
    disabled = set(config.prefs().get("disabledTools") or [])
    if disabled:
        rows = [t for t in rows if t["function"]["name"] not in disabled]
    if readonly:
        rows = [t for t in rows if t["function"]["name"] not in READONLY_BLOCKED]
    if depth >= config.SUBAGENT_MAX_DEPTH:
        rows = [t for t in rows if t["function"]["name"] != SUBTASK]

    # The MCP helper tools only earn their schemas when an MCP server is
    # configured: with none in mcp.json they are four dead ends that cost
    # tokens and invite calls that can only fail. Presence is the gate — a
    # configured-but-stopped server still gets them, because listing and
    # searching are how the model learns what it could start.
    try:
        if not mcp_registry.load()["servers"]:
            rows = [t for t in rows
                    if t["function"]["name"] not in MCP_HELPER_NAMES]
    except Exception:  # noqa: BLE001
        pass

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


def _pinned(messages: list[dict]) -> list[dict]:
    """The leading prefix that must survive elision: standing prompt, then task.

    Pinning only ``messages[0]`` is what lost a sub-agent its instructions. For a
    sub-agent [0] is the system prompt and [1] is the task, so elision kept the
    persona and dropped the question — the report that came back said in terms
    that it no longer knew what it had been asked. A compaction summary lands in
    the same prefix and is kept for the same reason: it is the only surviving
    record of the work the elision just removed.
    """
    out, seen_user = [], False
    for m in messages:
        role = m.get("role")
        if role == "system" and not seen_user:
            out.append(m)
        elif role == "user" and not seen_user:
            out.append(m)
            seen_user = True
        else:
            break
    return out


def windowed(messages: list[dict], budget: int) -> list[dict]:
    if not messages:
        return messages
    if sum(_size(m) for m in messages) <= budget:
        return messages
    pin = _pinned(messages)
    rest = messages[len(pin):]
    room = budget - sum(_size(m) for m in pin)
    kept, total = [], 0
    for m in reversed(rest):
        sz = _size(m)
        if kept and total + sz > room:
            break
        kept.append(m)
        total += sz
    kept.reverse()
    # A tool result whose call was elided has nothing to answer, and most
    # providers reject the request outright.
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)
    if len(pin) + len(kept) >= len(messages):
        return messages
    return pin + [{"role": "system", "content": prompts.ELIDED}] + kept


def context_budget(ref: str | None = None, rec: dict | None = None) -> int:
    """Characters of transcript to send, derived from the model's real window.

    One flat number is wrong in both directions: 120k characters is about 3% of a
    1M-token window and four times an 8k one. Where the window is known the cap
    follows it, so a large model stops re-reading files the cap had thrown away
    and a small one is never sent a transcript it cannot hold. A per-model
    ``context_budget`` set for the model overrides both.
    """
    try:
        override = int((model_settings.profile_for(ref) or {}).get("context_budget") or 0)
    except Exception:  # noqa: BLE001
        override = 0
    if override > 0:
        return override
    window = 0
    if rec:
        window = int(((rec.get("usage") or {}).get("context") or {}).get("window") or 0)
    if not window and ref:
        try:
            window = int((config.resolve_model(ref) or {}).get("contextWindow") or 0)
        except Exception:  # noqa: BLE001
            window = 0
    if window <= 0:
        return config.AGENT_CONTEXT_BUDGET
    # Derived from the same trigger as compaction, so the cap and the summariser
    # agree about how much of the window is ours to fill.
    chars = config.context_fill_target(window) * token_mod.CHARS_PER_TOKEN
    return max(config.CONTEXT_BUDGET_MIN, min(chars, config.CONTEXT_BUDGET_MAX))


def parse_args(raw: str) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


# A call whose arguments never finished streaming. The usual victim is a large write_file:
# the round reaches the output cap in the middle of the document, and parse_args turns the
# half-JSON into {} - so the harness runs a write with no path and no content, the transcript
# says it ran, and the model goes on believing the file exists. The cells ended as
# "attack.py does not exist" with 45,000 output tokens behind them. Say plainly that nothing
# happened, and make the note start with ERROR: so the repeat guard never treats it as a
# result worth caching.
SEVERED = ("ERROR: this call was cut off by the output limit while its arguments were still "
           "streaming, so it did NOT run and nothing was written. Break the write into pieces "
           "that fit one round: write_file the first part, then extend the file with edit_file.")


def _drain(stream, got: list, why: list, state: dict):
    """Consume one model round: yield what the interface needs, record what the loop needs.

    `state` collects the tool calls, the finish reason and the round's tokens, so a caller can
    decide whether the round was usable without the event stream leaking back out.
    """
    for ev in stream:
        kind = ev["type"]
        if kind == "text":
            got.append(ev["delta"])
            yield {"type": "text", "delta": ev["delta"]}
        elif kind == "reason":
            why.append(ev["delta"])
            yield {"type": "reason", "delta": ev["delta"]}
        elif kind == "tool_calls":
            state["calls"] = ev["calls"]
        elif kind == "usage":
            u = ev.get("usage") or {}
            state["tokens"] = int(state.get("tokens") or 0) + int(u.get("input") or 0) \
                + int(u.get("output") or 0)
            yield {"type": "usage", "usage": u}
        elif kind == "done":
            state["finish"] = ev.get("finish") or state.get("finish") or ""


def _code_block(text: str) -> str:
    """The first fenced block in a reply, or "" when there is none."""
    m = re.search(r"```[ \t]*[\w+#.-]*\n(.*?)```", text or "", re.S)
    return (m.group(1).strip() if m else "")


def _severed(calls: list) -> set:
    """ids of calls whose argument JSON cannot be parsed - they never arrived complete."""
    bad = set()
    for c in calls or []:
        raw = str(c.get("arguments") or "").strip()
        if not raw or raw == "{}":
            continue
        try:
            json.loads(raw)
        except Exception:
            bad.add(c.get("id"))
    return bad


def _chars(msgs: list[dict]) -> int:
    """What a stored transcript costs once it is expanded for the model.

    A stored row carries its tool results and its reasoning alongside the text,
    and both are replayed next turn. Counting only ``content`` measured about a
    twentieth of the real size — on one session, 13,784 characters counted
    against 307,088 actually sent — so compaction never fired and the window cap
    silently elided the work instead of summarising it.
    """
    total = 0
    for m in msgs or []:
        if not isinstance(m, dict):
            total += len(str(m))
            continue
        total += len(m.get("content") or "") + len(m.get("reason") or "")
        for t in m.get("tools") or []:
            if not isinstance(t, dict):
                continue
            total += len(str(t.get("result") or "")) + _size(t.get("args") or {})
    return total


def should_compact(rec: dict) -> bool:
    msgs = rec.get("messages") or []
    if len(msgs) <= config.COMPACT_TAIL_MIN_MESSAGES + 1:
        return False
    ctx = (rec.get("usage") or {}).get("context") or {}
    window, tokens = ctx.get("window") or 0, ctx.get("tokens") or 0
    if window and not tokens:
        # The provider reported a window but no usage for the last call. Measure
        # the transcript locally rather than falling through to the character
        # heuristic, which knows nothing about this model.
        tokens = token_mod.estimate_messages_tokens(
            transcript_messages(msgs, reasoning=False))
    if window and tokens:
        return tokens > config.context_fill_target(window)
    # No window known: compact once the transcript exceeds what we are willing to
    # send, which is exactly the point where elision would otherwise start
    # throwing information away instead of summarising it.
    return _chars(msgs) > context_budget(rec.get("model"), rec)


def _msg_tokens(m: dict) -> int:
    """Token cost of one message, in either the stored or the wire shape."""
    if not isinstance(m, dict):
        return token_mod.estimate_tokens(str(m))
    total = token_mod.estimate_tokens(m.get("content") or "")
    total += token_mod.estimate_tokens(m.get("reason") or "")
    for t in m.get("tools") or []:
        if isinstance(t, dict):
            total += token_mod.estimate_tokens(str(t.get("result") or ""))
    for c in m.get("tool_calls") or []:
        fn = (c or {}).get("function") or {}
        total += token_mod.estimate_tokens(str(fn.get("arguments") or ""))
    return total


def _tail_budget(trigger_tokens: int) -> int:
    """Tokens of recent transcript to keep verbatim through a compaction.

    Bounded by an absolute figure and by a share of whatever triggered the
    compaction, whichever is smaller.
    """
    if trigger_tokens <= 0:
        return config.COMPACT_TAIL_TOKENS
    return max(500, min(config.COMPACT_TAIL_TOKENS,
                        int(trigger_tokens * config.COMPACT_TAIL_SHARE)))


def _tail_split(rest: list[dict], budget: int | None = None) -> tuple[list[dict], list[dict]]:
    """Split into (to_summarise, to_keep) by token budget, not message count.

    A fixed count is six huge tool results on one turn and six one-line answers on
    the next: the first overflows the summariser's input and the second discards
    recent work that was cheap to keep. A token budget treats both alike, with a
    message floor so a tail of one enormous result still keeps its neighbours and
    a ceiling so a tail of hundreds of tiny ones cannot eat the summary.
    """
    if not rest:
        return [], []
    floor = max(1, min(config.COMPACT_TAIL_MIN_MESSAGES, len(rest)))
    ceiling = max(floor, min(config.COMPACT_TAIL_MAX_MESSAGES, len(rest)))
    budget = config.COMPACT_TAIL_TOKENS if budget is None else max(1, int(budget))
    keep, total = 0, 0
    for i in range(len(rest) - 1, -1, -1):
        cost = _msg_tokens(rest[i])
        if keep >= floor and total + cost > budget:
            break
        keep += 1
        total += cost
        if keep >= ceiling:
            break
    keep = max(keep, floor)
    split = len(rest) - keep
    # A tail may not open on an orphaned tool result: it has no call to answer.
    # Step back to include the assistant message that made the call rather than
    # dropping results — on a tool-heavy transcript dropping them walks the split
    # to the end and empties the tail, which loses more than it saves.
    while 0 < split < len(rest) and rest[split].get("role") == "tool":
        split -= 1
    if split < len(rest) and rest[split].get("role") == "tool":
        return [], list(rest)      # no owning call anywhere: nothing safe to fold
    return rest[:split], rest[split:]


_PRUNED = "[older output cleared to save context; the summary below records what it showed]"


def _prune_old_output(old: list[dict]) -> int:
    """Clear bulky tool output from rows that are about to be summarised anyway.

    A cheap pre-pass, and often the whole job: if dropping the bodies of old
    results brings the transcript back under budget there is no need to pay a
    model to summarise them. What is kept is the head of each result, which is
    where the answer usually is, plus the fact that there was more.
    """
    reclaimed = 0
    keep_chars = config.COMPACT_PRUNE_KEEP_CHARS
    for m in old:
        if not isinstance(m, dict):
            continue
        # Wire shape: the result is its own message.
        if m.get("role") == "tool":
            body = str(m.get("content") or "")
            if len(body) > config.COMPACT_PRUNE_MIN_CHARS:
                m["content"] = body[:keep_chars] + _PRUNED
                reclaimed += len(body) - keep_chars
            continue
        # Stored shape: results hang off the assistant row that made the calls.
        for t in m.get("tools") or []:
            if not isinstance(t, dict):
                continue
            body = str(t.get("result") or "")
            if len(body) > config.COMPACT_PRUNE_MIN_CHARS:
                t["result"] = body[:keep_chars] + _PRUNED
                reclaimed += len(body) - keep_chars
    return reclaimed


_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\.{0,2}/)[^\s`'\")\]}<>:,]+")


def _fallback_summary(digest: str, error: str = "") -> str:
    """A deterministic handover note for when the summariser is unavailable.

    Returning nothing here is what made a failed summary call silent and total:
    the transcript stayed full, the window cap then elided it, and the agent lost
    the work with no record of why. Paths, commands and failures are facts that
    can be extracted without a model, and they are the part worth keeping.
    """
    paths = sorted(set(_PATH_RE.findall(digest or "")))[:40]
    lines = ["[automatic digest — the summariser was unavailable"
             + (f": {error[:120]}" if error else "") + "]"]
    if paths:
        lines.append("Files and paths touched: " + ", ".join(paths))
    failed = [ln.strip() for ln in (digest or "").splitlines() if "FAILED" in ln][:20]
    if failed:
        lines.append("Calls that failed:")
        lines.extend("  " + f[:200] for f in failed)
    tools = collections.Counter(
        m.group(1) for m in re.finditer(r"tool (\w+)", digest or ""))
    if tools:
        lines.append("Tool calls: " + ", ".join(f"{k} x{v}" for k, v in
                                                tools.most_common(12)))
    tail = (digest or "")[-config.COMPACT_PRUNE_MIN_CHARS:]
    lines.append("Most recent activity:\n" + tail)
    return "\n".join(lines)[:8000]


def _summarise(transcript: str, ref: str | None = None) -> tuple[str, str]:
    """Ask a model for the handover note. Returns (summary, error).

    A nominated cheap model does this job when one is configured: summarising is
    mechanical, and spending the working model — often the expensive one — on it
    is waste. The transcript is capped so the summariser call cannot itself
    overflow the window it is trying to relieve.
    """
    capped = transcript[:config.SUMMARY_INPUT_MAX_CHARS]
    try:
        res = engine.chat([
            {"role": "system", "content": prompts.COMPACT},
            {"role": "user", "content": capped},
        ], ref=config.COMPACT_MODEL or ref)
    except engine.EngineError as e:
        return _fallback_summary(capped, str(e)), str(e)
    except Exception as e:  # noqa: BLE001
        return _fallback_summary(capped, str(e)), str(e)
    summary = (res.get("content") or "").strip()
    if not summary:
        return _fallback_summary(capped, "the summariser returned nothing"), "empty summary"
    return summary, ""


def _digest_row(m: dict) -> str:
    """One transcript row as the summariser should see it.

    Text alone is not the work. The paths read, the commands run and whether they
    failed are the part a handover note has to carry, and they live on the row's
    tool list rather than in its content.
    """
    role = m.get("role", "user")
    body = (m.get("content") or "").strip()
    lines = [f"{role}: {body[:2000]}"] if body else []
    for t in m.get("tools") or []:
        if not isinstance(t, dict):
            continue
        try:
            argtext = json.dumps(t.get("args") or {}, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            argtext = str(t.get("args") or "")
        flag = " FAILED" if t.get("is_error") else ""
        lines.append(f"  tool {t.get('name')}{flag} {argtext[:200]}")
    return "\n".join(lines) or f"{role}: (nothing recorded)"


def _is_summary(m: dict) -> bool:
    return prompts.SUMMARY_MARK in str(m.get("content") or "")


def _stored_pin(msgs: list[dict]) -> int:
    """How many leading stored rows must survive verbatim.

    The task itself, always. A summary written by an earlier compaction is not
    pinned: it falls into the region being summarised, so the next note refines it
    instead of stacking a second summary on top of the first.
    """
    n = 0
    for m in msgs:
        if m.get("role") == "user":
            n += 1
        else:
            break
    return n


def compact_history(rec: dict, ref: str | None = None, force: bool = False) -> dict | None:
    msgs = rec.get("messages") or []
    if not force and not should_compact(rec):
        return None
    pin = _stored_pin(msgs)
    rest = msgs[pin:]
    if len(rest) <= config.COMPACT_TAIL_MIN_MESSAGES:
        return None
    ctx = (rec.get("usage") or {}).get("context") or {}
    trigger = config.context_fill_target(ctx.get("window") or 0) or (_chars(msgs) // 4)
    old, tail = _tail_split(rest, _tail_budget(trigger))
    if not old:
        return None
    before = _chars(msgs)
    # A summary written by an earlier compaction sits at the head of `old`, so it
    # is digested along with everything else and the new note refines it rather
    # than stacking a second summary on top of the first.
    reclaimed = _prune_old_output(old)
    summary, err = _summarise("\n\n".join(_digest_row(m) for m in old), ref)
    rec["messages"] = (list(msgs[:pin])
                       + [{"role": "summary", "content": summary}] + tail)
    return {"compacted": len(old), "kept": len(tail), "chars_before": before,
            "chars_after": _chars(rec["messages"]), "pruned_chars": reclaimed,
            "error": err}


def _digest_wire(m: dict) -> str:
    """One wire message as the summariser should see it.

    The wire shape differs from the stored one: an assistant row carries
    ``tool_calls`` and each result arrives as its own ``tool`` row. Summarising
    those as bare content would throw away the only record of what was done.
    """
    role = m.get("role", "user")
    body = (m.get("content") or "").strip()
    if isinstance(body, list):          # multimodal content parts
        body = " ".join(p.get("text") or "" for p in body if isinstance(p, dict)).strip()
    lines = [f"{role}: {body[:1500]}"] if body else []
    for call in m.get("tool_calls") or []:
        fn = (call or {}).get("function") or {}
        lines.append(f"  call {fn.get('name')} {str(fn.get('arguments') or '')[:200]}")
    if role == "tool":
        lines.append(f"  result: {body[:400]}" if body else "  result: (empty)")
    return "\n".join(lines) or f"{role}: (nothing recorded)"


def wire_chars(messages: list[dict]) -> int:
    """Size of a live wire transcript, measured the same way it will be sent."""
    return sum(_size(m) for m in messages or [])


def compact_messages(messages: list[dict], ref: str | None = None,
                     budget_chars: int | None = None) -> dict | None:
    """Compact a live transcript in place, keeping the pinned prefix and the tail.

    :func:`compact_history` does this between turns, on the stored record. Nothing
    did it *during* one, so a long turn grew until the window cap started silently
    eliding instead — losing information a summary would have kept. This is the
    same discipline applied mid-turn, and it mutates ``messages`` in place because
    the caller's loop holds that list.
    """
    pin = _pinned(messages)
    rest = messages[len(pin):]
    if len(rest) <= config.COMPACT_TAIL_MIN_MESSAGES:
        return None
    trigger = (int(budget_chars) // token_mod.CHARS_PER_TOKEN
               if budget_chars else config.COMPACT_TAIL_TOKENS)
    old, tail = _tail_split(rest, _tail_budget(trigger))
    if not old:
        return None
    before = wire_chars(messages)
    reclaimed = _prune_old_output(old)
    summary, err = _summarise("\n\n".join(_digest_wire(m) for m in old), ref)
    messages[:] = (pin
                   + [{"role": "system",
                       "content": f"{prompts.SUMMARISED}\n{summary}"}]
                   + tail)
    return {"compacted": len(old), "kept": len(tail), "chars_before": before,
            "chars_after": wire_chars(messages), "pruned_chars": reclaimed,
            "error": err}


def _clip_report(text: str, limit: int) -> str:
    """Trim a sub-agent report without losing its ending.

    A hard cut at the front-only limit threw away the conclusion, which is the
    part the parent acts on — the parent then re-read the files itself and paid
    for the work twice. Keeping head and tail preserves the opening findings and
    the summary, and says plainly how much went missing in between.
    """
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head - 120
    dropped = len(text) - head - max(0, tail)
    return (text[:head]
            + f"\n\n…[{dropped} characters of this report were dropped to stay inside the "
              f"{limit}-character delegation budget. Ask for a narrower question if the "
              "missing part matters.]\n\n"
            + (text[-tail:] if tail > 0 else ""))


def run_subagent(task: str, ctx: dict, out: dict, parent_call_id: str = ""):
    depth = int(ctx.get("depth") or 0)
    if depth >= config.SUBAGENT_MAX_DEPTH:
        out["result"] = "ERROR: sub-agents cannot delegate further"
        return
    if not (task or "").strip():
        out["result"] = "ERROR: prompt required"
        return

    # Delegated work runs on the delegation model when one is set, else the
    # session's. The parent's ref is the fallback, not the rule: a session model
    # at thinking=max is a poor default for fetch-and-grep work, and the choice
    # has to be reachable from the interface.
    ref = str(ctx.get("ref") or "").strip() or config.delegate_model() or None
    messages = [
        {"role": "system", "content": prompts.system_prompt(ctx.get("project"), False, subagent=True)},
        {"role": "user", "content": task},
    ]
    report: list[str] = []
    spent = 0
    for ev in run_turn(messages, project=ctx.get("project"), ref=ref,
                       max_steps=config.SUBAGENT_MAX_STEPS, depth=depth + 1,
                       readonly=True, nested=True, session=ctx.get("session") or "",
                       stop=ctx.get("stop")):
        kind = ev.get("type")
        if kind == "text":
            report.append(ev["delta"])
            yield {**ev, "subagent": True, "parent_call_id": parent_call_id}
        elif kind == "reason":
            yield {**ev, "subagent": True, "parent_call_id": parent_call_id}
        elif kind == "usage":
            # The sub-agent's calls are billed too. Dropping these events made
            # the session's token total read low, and left the dashboard's
            # "saved by delegation" row permanently zero.
            u = ev.get("usage") or {}
            spent += int(u.get("input") or 0) + int(u.get("output") or 0)
            yield {**ev, "subagent": True, "delegated": spent, "parent_call_id": parent_call_id}
        elif kind in ("tool_start", "tool_end", "notify"):
            yield {**ev, "subagent": True, "parent_call_id": parent_call_id}

    text = "".join(report).strip() or "(the sub-agent returned no report)"
    result = _clip_report(text, config.SUBAGENT_RESULT_LIMIT)
    out["result"] = result
    out["delegated_tokens"] = spent
    # What the main window avoided: everything the sub-agent spent, less the
    # report that came back into it.
    out["saved_tokens"] = max(0, spent - token_mod.estimate_tokens(result))
    yield {"type": "notify", "level": "info", "subagent": True, "parent_call_id": parent_call_id,
           "message": "sub-agent complete"}


def call_tool(name: str, args: dict, ctx: dict, out: dict, call: dict | None = None):
    if ctx.get("readonly"):
        refusal = readonly_guard(name, args)
        if refusal:
            out["result"] = refusal
            return
    parent_call_id = call.get("id") if call else None
    if name == SUBTASK:
        yield from run_subagent(args.get("prompt", ""), ctx, out, parent_call_id=parent_call_id)
        return
    if name == "research":
        yield from run_research(args, ctx, out, parent_call_id=parent_call_id)
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
    if fn is None and name != "research":
        plugin_result = plugin_manager.call_tool(name, args, ctx)
        if plugin_result is not None:
            out["result"] = str(plugin_result)
            return
        out["result"] = f"ERROR: unknown tool '{name}'"
        return
    if "project" not in args and "project" in inspect.signature(fn).parameters:
        args = {**args, "project": ctx.get("project")}
    if "session" not in args and "session" in inspect.signature(fn).parameters:
        args = {**args, "session": ctx.get("session") or ""}
    try:
        out["result"] = str(fn(**args))
    except ToolError as e:
        out["result"] = f"ERROR: {e}"
    except TypeError as e:
        out["result"] = f"ERROR: bad arguments for {name}: {e}"
    except Exception as e:
        out["result"] = f"ERROR: {e}"


def _run_parallel(specs: list[tuple], ctx: dict, results: dict):
    """Run independent delegations concurrently, merging their event streams.

    ``specs`` is a list of ``(call_id, name, args)``; each result lands in
    ``results`` keyed by call id, so the caller can still report them in the order
    the model asked for them.

    Sub-agents are read-only and independent, so there is nothing to serialise
    beyond the order results are reported in. Running four of them one after
    another is four times the wall clock for the same tokens, and mapping a
    codebase is exactly the case where a model asks for several at once.
    """
    q: queue.Queue = queue.Queue()
    outs: dict = {}

    def worker(cid: str, name: str, args: dict):
        out: dict = {}
        outs[cid] = out
        try:
            # Create a mock call object with the ID so sub-agent events get parent_call_id
            mock_call = {"id": cid}
            for p in call_tool(name, args, ctx, out, mock_call):
                q.put(("ev", p))
        except Exception as exc:  # noqa: BLE001
            out["result"] = f"ERROR: {exc}"
        finally:
            q.put(("end", cid))

    threads = [threading.Thread(target=worker, args=spec, daemon=True) for spec in specs]
    for t in threads:
        t.start()
    remaining = len(threads)
    while remaining:
        kind, payload = q.get()
        if kind == "ev":
            yield payload
        else:
            remaining -= 1
    results.update(outs)


def _audit_tool(name: str, args: dict, result, failed: bool, ctx: dict) -> None:
    """One ledger row per tool call, as the README says the ledger holds.

    Arguments go in as a short digest and the result as a length, not as content:
    the ledger is for checking what happened, not for storing the transcript a
    second time. `run_shell` is skipped because the sandbox layer already writes
    a richer row for the same call.
    """
    if name == "run_shell":
        return
    # Passed as a dict, not a JSON string, so audit's own masking still sees the
    # keys inside it: a `url` or an `env` block in the arguments is redacted on
    # the way in exactly as it would be anywhere else in the ledger.
    digest = {}
    for k, v in list((args or {}).items())[:12]:
        try:
            txt = (json.dumps(v, ensure_ascii=False)
                   if isinstance(v, (dict, list)) else str(v))
            key = str(k)[:40]
            if len(txt) > 200:
                # The preview is only a preview, and saying so matters: without the real length a
                # 400-line write reads back as a 200-character file, and I have already mistaken my
                # own truncation for the agent's and announced a root cause from it.
                digest[key] = txt[:200]
                digest[key + "_chars"] = len(txt)
            else:
                digest[key] = txt
        except Exception:  # noqa: BLE001
            digest[str(k)[:40]] = "<unserialisable>"
    audit.record("tool_call", session=ctx.get("session") or "", tool=name,
                 status="error" if failed else "ok",
                 depth=int(ctx.get("depth") or 0), args=digest,
                 result_chars=len(str(result or "")))


def model_reasons(ref: str | None = None) -> bool:
    """Whether the chosen model is known to think, so its reasoning can be replayed."""
    try:
        return bool((config.resolve_model(ref) or {}).get("reasoning"))
    except Exception:
        return False


def transcript_messages(rows: list[dict], reasoning: bool = False) -> list[dict]:
    """Expand stored transcript rows into the messages a model should see.

    A stored assistant row carries the calls it made and what came back. Sent as
    plain text those would be invisible to the model, which is how an agent forgot
    its own work between turns and started the same exploration again.

    `reasoning` replays each turn's chain of thought back to a thinking model. It
    is off unless the chosen model is known to reason, because a provider that does
    not know the key may reject the request outright.
    """
    out = []
    for m in rows or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        body = m.get("content") or ""
        calls = [t for t in (m.get("tools") or []) if isinstance(t, dict) and t.get("id")]
        if not body.strip() and not calls:
            continue
        if role == "summary":
            out.append({"role": "system", "content": f"{prompts.SUMMARISED}\n{body}"})
        elif role == "user":
            out.append({"role": "user", "content": body})
        elif role == "assistant":
            row = {"role": "assistant", "content": body}
            if reasoning and (m.get("reason") or "").strip():
                row["reasoning_content"] = m["reason"]
            if calls:
                row["tool_calls"] = [
                    {"id": t["id"], "type": "function",
                     "function": {"name": t.get("name") or "",
                                  "arguments": json.dumps(t.get("args") or {})}}
                    for t in calls]
            out.append(row)
            for t in calls:
                out.append({"role": "tool", "tool_call_id": t["id"],
                            "content": str(t.get("result") or "(no result recorded)")})
    return out


def run_turn(messages: list[dict], *, project: str | None, readonly: bool = False,
             max_steps: int | None = None, ref: str | None = None, temperature=None,
             depth: int = 0, nested: bool = False, chat: bool = False,
             stop=None, steer=None, reasoning=None, trace: list | None = None,
             session: str = "", has_instructions: bool = False, deadline: float | None = None):
    steps = max_steps or config.AGENT_MAX_STEPS
    tools = [] if chat else tools_for(readonly, depth)
    ctx = {"project": project, "ref": ref, "depth": depth, "readonly": readonly,
           "session": session, "stop": stop}
    # One budget for the whole turn, derived from the model's real window rather
    # than a flat constant that suits neither a 1M-token model nor an 8k one.
    budget = config.AGENT_CONTEXT_BUDGET if chat else context_budget(ref)
    try:
        window = int((config.resolve_model(ref) or {}).get("contextWindow") or 0)
    except Exception:  # noqa: BLE001
        window = 0
    # Live cost accounting. Tacit's claim is that it shows you what it costs, and
    # a figure you can only read after the turn is over is half that promise: one
    # session billed 1.9M tokens without the interface saying a word until the end.
    turn_tokens = 0
    warned: set = set()
    compactions = 0
    prof = model_settings.profile_for(ref)
    if temperature is None and prof.get("temperature") is not None:
        temperature = prof["temperature"]
    prof_tokens = prof.get("max_tokens")

    # Guidance is turn-scoped: what failed, what is being repeated, what was
    # already said to the model on a previous step.
    last_errors: list = []
    last_block = ""
    # Counts, not a set: "have I seen this exact call" is only useful if the answer can grow.
    # The feal cell that issued 59 shell calls with 10 distinct arguments was not exploring, it
    # was re-running one probe and paying full price for the same bytes each time.
    seen_calls: dict = {}
    call_cache: dict = {}
    repeats: list = []
    # Consecutive identical calls refused this turn. Reset by any call that actually runs, because
    # "stuck" means nothing changed, not that the same tool was used twice.
    blocked_run = 0
    # Names the task asks for that are not on disk yet, and the names this turn has written.
    wanted = deliverables(next((str(m.get("content") or "") for m in messages
                                if m.get("role") == "user"), ""), project)
    guarantee = config.deliver_guarantee()
    written: set = set()
    phase_ran = [False]
    touched = [False]
    ran_after_write = [False]
    demanded = [0]
    forced = 0
    snapped = False

    def _salvage(names, run_check=False):
        """One round whose only tools are the ones that can write the deliverable.

        The closing call deliberately offers no tools, which is right for a report and wrong for a
        task whose score comes from a file: the checker reads the disk, not the answer. This is the
        one exception, and it is still one round, so it cannot become a second step budget.
        """
        nonlocal turn_tokens
        schemas = [t for t in tools if t["function"]["name"] in SALVAGE_TOOLS]
        if not schemas or chat:
            return
        ask = windowed(messages + [{"role": "user",
                                    "content": (guidance.verify(names) if run_check
                                                else guidance.salvage(names))}], budget)
        got: list[str] = []
        why: list[str] = []
        calls: list = []
        # Two attempts, and the second one is the interesting case. A salvage round exists to
        # produce one large file, which is exactly what an output cap severs: the round comes back
        # with finish_reason=length and half a write_file in it. That is our ceiling failing, not
        # the model, so the retry sends the same request with no max_tokens at all and lets the
        # provider finish the document. Only ever one extra attempt, and only when the round was
        # actually cut off.
        for attempt, cap in enumerate((prof_tokens, 0)):
            got, why, calls = [], [], []
            state: dict = {}
            try:
                yield from _drain(engine.stream_chat(ask, ref=ref, tools=schemas,
                                                     temperature=temperature, max_tokens=cap,
                                                     reasoning_effort=reasoning), got, why, state)
            except engine.EngineError as exc:
                yield {"type": "error", "message": "no salvage round: " + str(exc)[:160]}
                return
            turn_tokens += int(state.get("tokens") or 0)
            calls = list(state.get("calls") or [])
            # Only 0 means "send no max_tokens". None does not: the payload falls back to the
            # model card, so None is a real ceiling worth escalating away from.
            if cap == 0 or state.get("finish") != "length" or not _severed(calls):
                break
            yield {"type": "notify", "level": "info",
                   "message": "salvage round hit the output cap mid-write; retrying without one"}
        if not calls:
            # The phase exists because a cell that ends with nothing on disk scores zero however
            # well it argued. feal trial 2 is exactly that: 16 turns, 38,500 tokens, no write_file
            # ever attempted. If the reply to "write it now" is prose with the code in it, that
            # code is the deliverable and the only thing between 0 and a chance is who puts it on
            # disk. Extraction is not authorship - the content is the model's, unedited.
            prose = "".join(got)
            block = _code_block(prose)
            if block and names:
                target = str(names[0])
                out: dict = {}
                try:
                    for _p in call_tool("write_file", {"path": target,
                                                      "content": block}, ctx, out, None):
                        pass
                except Exception:
                    return
                result = str(out.get("result") or "")
                if not result or result.startswith("ERROR:"):
                    return
                written.add(target.rsplit("/", 1)[-1])
                messages.append({"role": "assistant", "content": prose})
                messages.append({"role": "tool", "tool_call_id": "salvage-extract",
                                 "content": result})
                yield {"type": "notify", "level": "info",
                       "message": f"no tool call in that reply: wrote {target} from the code in it"}
            return
        messages.append({"role": "assistant", "content": "".join(got), "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
            for c in calls]})
        step_tools: list[dict] = []
        for c in calls:
            args = parse_args(c["arguments"])
            yield {"type": "tool_start", "name": c["name"], "args": args, "id": c["id"]}
            if c.get("id") in _severed(calls):
                out = {"result": SEVERED}
                yield {"type": "notify", "level": "info",
                       "message": f"discarded a {c['name']} call cut off mid-arguments"}
            else:
                out = {}
                for p in call_tool(c["name"], args, ctx, out, c):
                    yield p
            result = out.get("result", "ERROR: the tool produced no result")
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
            failed = str(result).startswith("ERROR:")
            if not failed and c["name"] == "run_shell":
                # Only a real execution counts, and only the shell can be one. A successful
                # write_file must not satisfy this, and neither may a read_file - or "the
                # deliverable was run" collapses back into "something happened after it".
                phase_ran[0] = True
            _audit_tool(c["name"], args, result, failed, ctx)
            path = str(args.get("path") or args.get("file") or "")
            if path:
                written.add(path.replace("\\", "/").rsplit("/", 1)[-1])
            step_tools.append({"id": c["id"], "name": c["name"], "args": args,
                               "result": result, "is_error": failed})
            yield {"type": "tool_end", "name": c["name"], "result": result,
                   "id": c["id"], "is_error": failed}
        if trace is not None:
            trace.append({"text": "".join(got), "reason": "".join(why).strip(),
                          "tools": step_tools})

    def _report(missing=(), closing=True, verify=()):
        """One closing call with no tools offered, so it can only say where things
        stand. Used whenever the turn would otherwise end with no answer. `closing=False`
        keeps the deliverable phase but skips the summary, for a turn that already said
        something and simply left the named file unwritten."""
        nonlocal turn_tokens
        if chat or not tools:
            return
        # The other half of the same failure. schemelike shipped `Missing closing parenthesis`
        # three trials running because the deliverable EXISTED: the salvage loop below only fires
        # for files that are absent, so a written-but-never-run file ended the turn untouched. This
        # asks for the run, on the files the task named, without offering the authoring tools a
        # second time around - nothing here writes a new file from scratch.
        if verify and not missing:
            for _ in range(SALVAGE_ROUNDS):
                yield from _salvage(list(verify), run_check=True)
                if ran_after_write[0] or phase_ran[0]:
                    break
            if not closing:
                return
        if missing and config.deliver_guarantee():
            names = list(missing)
            # Landing the file does not end the phase. It ends when the file has landed AND
            # something has been run inside it - otherwise a syntactically broken deliverable
            # counts as done and the cell dies at the checker instead of at a failing run.
            for _ in range(SALVAGE_ROUNDS):
                yield from _salvage(names if names else list(missing),
                                    run_check=not names)
                names = [n for n in missing if n.rsplit("/", 1)[-1] not in written]
                if not names and phase_ran[0]:
                    break
            if not closing:
                return
        ask = windowed(messages + [{"role": "user", "content": guidance.closing()}],
                       budget)
        got: list[str] = []
        why: list[str] = []
        try:
            for ev in engine.stream_chat(ask, ref=ref, tools=None, temperature=temperature,
                                         max_tokens=prof_tokens, reasoning_effort=reasoning):
                if ev["type"] == "text":
                    got.append(ev["delta"])
                    yield {"type": "text", "delta": ev["delta"]}
                elif ev["type"] == "reason":
                    why.append(ev["delta"])
                    yield {"type": "reason", "delta": ev["delta"]}
                elif ev["type"] == "usage":
                    u = ev.get("usage") or {}
                    turn_tokens += int(u.get("input") or 0) + int(u.get("output") or 0)
                    yield {"type": "usage", "usage": u}
        except engine.EngineError as exc:
            # Swallowing this is how a turn came back empty with no explanation.
            yield {"type": "error", "message": "no closing report: " + str(exc)}
            return
        body = "".join(got).strip()
        if not body and trace is not None:
            # The closing call can come back empty, which would leave the user with
            # a transcript of tool results and no answer again. A count of what
            # actually ran is a fact, so it is better than silence.
            names = [t["name"] for s in (trace or []) for t in (s.get("tools") or [])]
            tally: dict = {}
            for nm in names:
                tally[nm] = tally.get(nm, 0) + 1
            order = ", ".join(f"{k} x{v}" for k, v in
                              sorted(tally.items(), key=lambda x: -x[1]))
            body = ("No report came back from the model. This turn ran "
                    f"{len(names)} tool calls ({order or 'none'}) and concluded nothing "
                    "else. Ask again with a narrower task.")
            yield {"type": "text", "delta": body}
        if trace is not None:
            trace.append({"text": body, "reason": "".join(why).strip(), "tools": []})
    # The project context is read once at the start of a task, not on every
    # turn. Repeating it spends the window on advice the model already followed, so
    # discovery is only offered while the transcript holds no assistant work.
    has_prior_work = any(m.get("role") == "assistant" for m in messages)

    for i in range(steps):
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
        # Collected per step, not per turn. A turn is many model calls, and text
        # from different steps must not be glued into one blob: that is what made
        # a transcript read like "...let me do it now.Not yet I only listed...".
        step_text: list[str] = []
        step_reason: list[str] = []
        step_errors: list = []
        try:
            ctx_msgs = windowed(messages, budget)
            # Only in the last few steps: a task that names an optional file should not be
            # nagged about it for the whole turn, and near the cap it is the only thing worth
            # spending a step on, because the checker reads files rather than answers.
            missing = ()
            if wanted and steps - i <= 3:
                missing = tuple(n for n in wanted
                                if n.rsplit("/", 1)[-1] not in written)
            block = guidance.for_step(step=i, steps=steps, errors=last_errors,
                                      repeated=repeats, missing=missing,
                                      first=(i == 0 and not has_prior_work),
                                      project=bool(project) and not has_instructions, previous=last_block)
            if block:
                # Appended at the point of use instead of folded into the standing
                # prompt, so the cached prefix stays intact and the block is only
                # paid for on the step that needs it.
                ctx_msgs = ctx_msgs + [{"role": "system", "content": block}]
                last_block = block
            # The wall clock is part of the task. Until now the loop could not see it: headless
            # checked the deadline between events and simply abandoned the turn, so a cell with
            # twelve turns of real work in it ended with nothing on disk and no credit at all.
            left = (deadline - time.time()) if deadline else None
            if guarantee and left is not None and left <= guidance.LATE_S:
                ctx_msgs = ctx_msgs + [{"role": "system",
                                       "content": guidance.time_left(left)}]
            # Late enough that another probe is not affordable, early enough that a file can still
            # land. Write the deliverable with what is already known rather than being cut off
            # mid-thought: a rough artifact that exists beats a perfect plan that never landed.
            if (guarantee and left is not None and left <= guidance.EMERGENCY_S
                    and wanted and not chat and not nested and depth == 0):
                late = tuple(n for n in wanted
                             if n.rsplit("/", 1)[-1] not in written)
                if late:
                    yield {"type": "notify", "level": "info",
                           "message": f"{max(0, int(left))}s of wall budget left and "
                                      f"{', '.join(late)} is not written: delivering now"}
                    yield from _report(missing=late, closing=False)
                    return

            finish = ""
            stream = engine.stream_chat(ctx_msgs,
                                        ref=ref, tools=tools, temperature=temperature,
                                        max_tokens=prof_tokens, reasoning_effort=reasoning)
            for ev in stream:
                if stop is not None and stop.is_set():
                    cancelled = True
                    break
                if ev["type"] == "text":
                    step_text.append(ev["delta"])
                    yield {"type": "text", "delta": ev["delta"]}
                elif ev["type"] == "reason":
                    step_reason.append(ev["delta"])
                    yield {"type": "reason", "delta": ev["delta"]}
                elif ev["type"] == "tool_calls":
                    calls = ev["calls"]
                elif ev["type"] == "usage":
                    u = ev.get("usage") or {}
                    turn_tokens += int(u.get("input") or 0) + int(u.get("output") or 0)
                    yield {"type": "usage", "usage": u}
                elif ev["type"] == "done":
                    model_used = ev.get("model")
                    finish = ev.get("finish") or finish
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

        # Our own output ceiling is the one failure the harness causes on the hard set. When a
        # round is cut off, the tool arguments arrive half-written, _severed() refuses to execute
        # them, and the model gets an ERROR note for work it genuinely tried to do. So ask for the
        # same round once more with no ceiling at all and let the provider finish it. Only on a
        # real length cut with a broken call, and only once per step - 0 omits max_tokens, None
        # would fall back to the model card and still be a ceiling.
        if finish == "length" and _severed(calls) and not cancelled and not chat and not nested:
            yield {"type": "notify", "level": "info",
                   "message": "output ceiling severed that call; retrying the round uncapped"}
            step_text, step_reason, calls = [], [], []
            retry: dict = {}
            try:
                yield from _drain(engine.stream_chat(ctx_msgs, ref=ref, tools=tools,
                                                     temperature=temperature, max_tokens=0,
                                                     reasoning_effort=reasoning),
                                  step_text, step_reason, retry)
            except engine.EngineError:
                pass
            calls = list(retry.get("calls") or [])
            turn_tokens += int(retry.get("tokens") or 0)

        narration = "".join(step_text).strip()
        thinking = "".join(step_reason).strip()

        if not calls:
            # A step that announces intent and runs nothing is deliberation, not
            # progress. It gets one directive to act and another chance, which is
            # how the loop stops describing work it was supposed to be doing.
            if (not chat and tools and not nested and depth == 0
                    and guidance.plan_like(narration)
                    and forced < 2 and i + 1 < steps):
                forced += 1
                messages.append({"role": "assistant", "content": narration})
                messages.append({"role": "user", "content": guidance.nudge()})
                if trace is not None:
                    trace.append({"text": narration, "reason": thinking, "tools": []})
                yield {"type": "notify", "level": "info",
                       "message": "planning detected: guided back to action"}
                continue
            stopped = stop is not None and stop.is_set()
            # Ending the turn on its own without the file the task named is the most
            # common hard-set failure there is: the model thinks out loud, says something
            # sensible, and leaves nothing for a checker to read. Waiting for the step cap
            # never reaches these cells — the ones that stopped at 13 turns had 45,000
            # output tokens and no artifact — so the deliverable phase runs here too.
            unwritten = ()
            if wanted and not stopped and not chat:
                unwritten = tuple(n for n in wanted
                                  if n.rsplit("/", 1)[-1] not in written)
            if unwritten and guarantee:
                yield from _report(missing=unwritten, closing=bool(narration))
            elif (guarantee and wanted and not stopped and not chat and not nested and touched[0]
                    and not ran_after_write[0]
                    and (not deadline or time.time() < deadline - 60)):
                # Everything the task named is on disk, the model wrote it, and it never executed
                # once. Ship it to the checker anyway and the cell dies on a syntax error the
                # model could have seen for one `python3 interp.py`.
                yield from _report(verify=tuple(wanted), closing=bool(narration))
            elif not narration and not stopped:
                # It stopped calling tools and said nothing on the way out. Left as
                # is, the user got a transcript of results with no answer at all.
                yield from _report()
            elif trace is not None:
                trace.append({"text": narration, "reason": thinking, "tools": []})
            yield {"type": "done"}
            return

        assistant = {"role": "assistant", "content": narration, "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
            for c in calls]}
        if thinking:
            # On a thinking model the chain of thought has to travel back with the
            # rest of the transcript. DeepSeek returns 400 for a tools request that
            # omits it; ollama.com tolerates the omission but reasons less when it is
            # gone. This is the model's own text going back to it, not ours.
            assistant["reasoning_content"] = thinking
        messages.append(assistant)
        step_tools: list[dict] = []

        # Independent read-only delegations run concurrently; everything else stays
        # serial and in the order the model asked for it, because each result has to
        # pair back up with the call that requested it.
        parsed = [(c, parse_args(c["arguments"])) for c in calls]
        broken = _severed(calls)
        batch = [(c["id"], c["name"], a) for c, a in parsed if c["name"] == SUBTASK]
        parallel: dict = {}
        if len(batch) > 1 and depth == 0 and not readonly:
            for cid, name, bargs in batch:
                yield {"type": "tool_start", "name": name, "args": bargs, "id": cid}
            yield {"type": "notify", "level": "info",
                   "message": f"running {len(batch)} sub-agents concurrently"}
            yield from _run_parallel(batch, ctx, parallel)

        for c, args in parsed:
            if stop is not None and stop.is_set():
                if trace is not None:
                    trace.append({"text": narration, "reason": thinking,
                                  "tools": step_tools})
                yield {"type": "notify", "message": "stopped", "level": "info"}
                yield {"type": "done"}
                return
            key = (c["name"], json.dumps(args, sort_keys=True)[:400])
            seen = seen_calls.get(key, 0) + 1
            seen_calls[key] = seen
            if seen > 1 and c["name"] not in repeats:
                repeats.append(c["name"])
            if (c["name"] in MUTATING and not snapped and project
                    and not readonly and depth == 0):
                snapped = True
                note = _auto_snapshot(project, c["name"], session)
                if note:
                    yield {"type": "notify", "level": "info", "message": note}
            if c["id"] in broken:
                # Never execute a call that arrived in pieces.
                yield {"type": "tool_start", "name": c["name"], "args": args, "id": c["id"]}
                out = {"result": SEVERED}
                yield {"type": "notify", "level": "info",
                       "message": f"discarded a {c['name']} call cut off mid-arguments"}
            elif c["id"] in parallel:
                out = parallel[c["id"]]
            elif (seen >= REPEAT_BLOCK_AFTER and c["name"] not in REPEAT_EXEMPT
                    and key in call_cache
                    and not str(call_cache[key]).startswith("ERROR:")):
                # Third identical successful call. Running it again is not new information, and
                # the model gets its own previous result back so it cannot claim it was withheld.
                yield {"type": "tool_start", "name": c["name"], "args": args, "id": c["id"]}
                still = [n for n in wanted if n.rsplit("/", 1)[-1] not in written]
                prev = (str(call_cache[key])[:REPEAT_ECHO] if seen == REPEAT_BLOCK_AFTER
                        else "(its result is already in the transcript above)")
                miss = (_MISSING_IN_REPLY.format(names=", ".join(still[:3])) if still else "")
                out = {"result": _REPEAT_REPLY.format(n=seen, tool=c["name"], prev=prev,
                                                     missing=miss)}
                yield {"type": "notify", "level": "info",
                       "message": f"blocked repeat call #{seen} of {c['name']}"}
                blocked_run += 1
            else:
                yield {"type": "tool_start", "name": c["name"], "args": args, "id": c["id"]}
                out = {}
                for p in call_tool(c["name"], args, ctx, out, c):
                    yield p
                blocked_run = 0
            result = out.get("result", "ERROR: the tool produced no result")
            call_cache[key] = result
            if c["name"] in MUTATING:
                path = str(args.get("path") or args.get("file") or "")
                if path:
                    written.add(path.replace("\\", "/").rsplit("/", 1)[-1])
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
            failed = str(result).startswith("ERROR:")
            # Two facts the turn cannot be answered without: did the model touch a file the task
            # named, and did anything ever RUN after that. `written` alone cannot tell them apart.
            here = str(args.get("path") or args.get("file") or "").replace("\\", "/")
            if not failed and here and here.rsplit("/", 1)[-1] in {
                    n.rsplit("/", 1)[-1] for n in wanted}:
                touched[0] = True
            elif not failed and touched[0] and c["name"] == "run_shell":
                # The only tool that proves the deliverable was executed. Reading a file, or
                # listing the directory, is not running eval.scm - a 200-char fragment passed
                # this test before and the cell died on `Unexpected closing parenthesis`.
                ran_after_write[0] = True
            _audit_tool(c["name"], args, result, failed, ctx)
            if c["name"] == SUBTASK and out.get("saved_tokens"):
                # Delegation is only a saving if it is counted. The sub-agent's
                # tokens were spent out of the main window; without this the
                # dashboard's "saved by delegation" row was permanently zero.
                yield {"type": "delegation", "saved": int(out["saved_tokens"]),
                       "spent": int(out.get("delegated_tokens") or 0)}
            # The call and its result are stored together. This record is what a
            # later turn rebuilds its own history from, so a step that ran tools
            # but said nothing is still a step the model can remember.
            step_tools.append({"id": c["id"], "name": c["name"], "args": args,
                               "result": result, "is_error": failed})
            yield {"type": "tool_end", "name": c["name"], "result": result,
                   "id": c["id"], "is_error": failed}
            if failed:
                step_errors.append((c["name"], str(result)[:200]))

        last_errors = step_errors

        # Order the artifact in the middle of the turn, not after it.
        # feal trial 2: 16 turns, 38,500 tokens, no write attempted. schemelike trial 3: 34 turns,
        # no file. Both ended because the turn ran out, so the end-of-turn deliverable phase -
        # which only runs when the model chooses to stop - never got to force anything. Halfway in,
        # while there is still budget to write AND to run it, there is.
        if guarantee and wanted and not chat and not nested and depth == 0:
            still = [n for n in wanted if n.rsplit("/", 1)[-1] not in written]
            # Two triggers, because a turn can run out on either clock. 40% of the steps is the
            # earliest the order pays for itself (a real feal run wrote attack.py only after the
            # order at 15/30 and then had to be cut short), and half the wall budget left catches
            # turns that burn steps slowly - s2's feal cell died at 1,081s with nothing on disk.
            due = (i + 1 >= max(3, steps * 2 // 5)
                   or (left is not None and left <= guidance.EMERGENCY_S * 2))
            late = (i + 1 >= max(3, steps * 3 // 4)
                    or (left is not None and left <= guidance.EMERGENCY_S))
            if still and (due or late) and demanded[0] < (2 if late else 1):
                demanded[0] += 1
                messages.append({"role": "user",
                                 "content": guidance.demand(still, f"{i + 1} of {steps} steps",
                                                            demanded[0])})
                yield {"type": "notify", "level": "info",
                       "message": "no deliverable on disk yet: ordered it written now"}

        # ── the cost governor ───────────────────────────────────────────
        # Reported live, at fractions of the model's real window, so a turn that
        # is running away is visible while it is still running.
        if window:
            for fraction in config.COST_WARN_FRACTIONS:
                mark = round(fraction, 3)
                if mark in warned:
                    continue
                if turn_tokens >= window * fraction:
                    warned.add(mark)
                    pct = fraction * 100
                    yield {"type": "notify", "level": "warn",
                           "message": (f"this turn has billed {token_mod.label(turn_tokens)} "
                                       f"tokens — {pct:g}% of the "
                                       f"{token_mod.format_token_count(window)} window")}
        if config.TURN_TOKEN_BUDGET and turn_tokens >= config.TURN_TOKEN_BUDGET:
            yield {"type": "notify", "level": "warn",
                   "message": (f"turn budget reached ({token_mod.label(turn_tokens)} of "
                               f"{token_mod.label(config.TURN_TOKEN_BUDGET)} tokens) — "
                               "stopping and reporting")}
            # This step's calls are already in messages; record them before the
            # closing report so the transcript does not lose the work just done.
            if trace is not None:
                trace.append({"text": narration, "reason": thinking, "tools": step_tools})
            yield from _report()
            yield {"type": "done"}
            return

        # Compact rather than elide. The window cap is a backstop that throws
        # information away; a summary keeps it. Doing this mid-turn is what stops
        # a long investigation from growing until the backstop is all that is left.
        if (not chat and config.COMPACT_MID_TURN and compactions < 3
                and wire_chars(messages) > budget):
            info = compact_messages(messages, ref=ref, budget_chars=budget)
            if info and info.get("compacted"):
                # Only a real attempt counts against the cap. Early in a turn
                # there is not yet a tail worth keeping, and charging those
                # no-ops spent the whole budget before anything could compact.
                compactions += 1
                saved = max(0, token_mod.estimate_tokens("x" * info["chars_before"])
                            - token_mod.estimate_tokens("x" * info["chars_after"]))
                yield {"type": "compaction", "saved": saved, **{
                    k: info[k] for k in ("compacted", "kept", "chars_before", "chars_after")}}
                yield {"type": "notify", "level": "info",
                       "message": (f"compacted {info['compacted']} earlier steps mid-turn "
                                   f"({info['chars_before']} → {info['chars_after']} chars)")}
            elif info and info.get("error"):
                # A summariser that failed once will fail again next step; count
                # it so the turn is not billed three more attempts.
                compactions += 1
                yield {"type": "notify", "level": "warn",
                       "message": "mid-turn compaction failed: " + str(info["error"])[:160]}

        if trace is not None:
            trace.append({"text": narration, "reason": thinking, "tools": step_tools})

        if blocked_run >= REPEAT_STOP_AFTER:
            # The same lap again, with nothing learned. Ending here is the only way the guard
            # changes the outcome rather than the cost: those steps go to the deliverable instead.
            yield {"type": "notify", "level": "warn",
                   "message": f"{blocked_run} identical calls refused in a row, ending the turn "
                              "on the deliverable"}
            yield from _report([n for n in wanted
                                if n.rsplit("/", 1)[-1] not in written])
            yield {"type": "notify", "message": f"stopped after {i + 1} steps", "level": "warn"}
            yield {"type": "done"}
            return

    # The step budget ran out while work was still open, so report where it stands.
    if not (stop is not None and stop.is_set()):
        yield from _report([n for n in wanted if n.rsplit("/", 1)[-1] not in written])

    yield {"type": "notify", "message": f"stopped after {steps} steps", "level": "warn"}
    yield {"type": "done"}

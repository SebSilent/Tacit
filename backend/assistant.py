"""The Assistant: a second conversation that sits beside the session, not in it.

The main agent does the work. The Assistant helps the *person* direct that work:
drafting prompts, thinking through an approach, spotting what was missed. It can
read the session and help with it. The main agent never sees the Assistant at
all, and that asymmetry is the whole point. It is where you think about the
work, so it must not leak into the work.

It lives inside the session record under ``assistant``, so it travels with the
session and disappears when the session does.

Token discipline applies here too. What the Assistant may read is a set of
explicit switches, and the cost of the current setting is computed before
anything is sent. Tool access is off by default, because a side conversation
that quietly runs tools is an expensive side conversation.
"""

from __future__ import annotations

from . import agent, config, tokens
from .ai import engine

DEFAULTS = {
    "include_user": True,       # what the person asked for
    "include_assistant": True,  # what the agent answered
    "include_meta": True,       # session name, project, model, mode
    "include_tools": False,     # a digest of the tool calls behind each answer
    "turns": 20,                # how many recent turns to include
    "tools": False,             # read-only tool access, off by default
    "model": "",                # empty means: same as the session
    "thinking": "",            # empty means: same as the session
}

PERSONA = """You are the Assistant, a second set of eyes beside a coding agent.

The agent is doing the work. You help the person direct it. You can read the conversation between
them, but you cannot change anything yourself. Your job is the work around the work: drafting
prompts to give the agent, thinking an approach through, noticing what was missed, explaining what
the agent just did.

The agent cannot see you. Nothing you say reaches it unless the person copies it across, so write
for the person, not for the agent.

Be direct and brief. Format answers in Markdown — headings, lists and tables where they help — and
put every command, snippet or prompt-to-paste inside a fenced code block with a language tag,
because the interface gives fenced blocks a one-click copy button. If they ask for something to
paste into the main chat, that block is the whole answer: no preamble before it, no commentary
after."""

# No preset list here. What the Assistant may be told to think with comes from the
# model it is using, discovered and cached on that model: see backend/thinking.py.
TOOL_LIMIT = 6


def settings_of(rec: dict) -> dict:
    stored = rec.get("assistantSettings") or {}
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = stored.get(key, default)
        if isinstance(default, bool):
            out[key] = bool(value)
        elif isinstance(default, str):
            out[key] = str(value or "")
        else:
            try:
                out[key] = max(1, min(int(value), 200))
            except (TypeError, ValueError):
                out[key] = default
    return out


def save_settings(rec: dict, patch: dict) -> dict:
    current = settings_of(rec)
    for key in DEFAULTS:
        if key in (patch or {}):
            current[key] = patch[key]
    rec["assistantSettings"] = current
    return settings_of(rec)


def _turns(rec: dict) -> list[list[dict]]:
    """The transcript grouped into (prompt, reply) turns."""
    turns: list[list[dict]] = []
    current: list[dict] = []
    for m in rec.get("messages") or []:
        role = m.get("role")
        if role == "user":
            if current:
                turns.append(current)
            current = [m]
        elif role == "assistant" and current:
            current.append(m)
    if current:
        turns.append(current)
    return turns


def _tool_digest(m: dict) -> str:
    """One line per tool call behind an answer: name, key argument, outcome.

    "Explain what the agent just did" is one of the jobs this panel is for, and
    prose alone cannot answer it. The digest is deliberately terse — a name and
    the argument that identifies the target — because the full results are what
    filled the agent's window in the first place.
    """
    rows = []
    for t in m.get("tools") or []:
        if not isinstance(t, dict):
            continue
        args = t.get("args") or {}
        key = ""
        for field in ("path", "pattern", "command", "url", "prompt", "question",
                      "name", "label", "action"):
            if args.get(field):
                key = f" {str(args[field])[:80]}"
                break
        rows.append(f"  · {t.get('name')}{key}"
                    + (" [failed]" if t.get("is_error") else ""))
    return "\n".join(rows)


def digest(rec: dict, cfg: dict | None = None) -> str:
    """The part of the session the Assistant is allowed to read.

    Only what the switches allow is included. Prompts and replies are on by
    default; the tool calls behind them are stored on the transcript but stay out
    unless asked for, because they are the expensive part and most questions do
    not need them. Reasoning is never included: it is the agent's private draft,
    and quoting it back as an answer would be worse than not having it.
    """
    cfg = cfg or settings_of(rec)
    lines = []
    if cfg["include_meta"]:
        bits = [f"name: {rec.get('title') or 'untitled'}"]
        if rec.get("project"):
            bits.append(f"project: {rec['project']}")
        if rec.get("model"):
            bits.append(f"model: {rec['model']}")
        if rec.get("mode"):
            bits.append(f"mode: {rec['mode']}")
        lines.append("[session] " + ", ".join(bits))

    for group in _turns(rec)[-cfg["turns"]:]:
        for m in group:
            role = m.get("role")
            body = (m.get("content") or "").strip()
            if role == "user" and cfg["include_user"] and body:
                lines.append("[person] " + body)
            elif role == "assistant" and cfg["include_assistant"]:
                if body:
                    lines.append("[agent] " + body)
                if cfg["include_tools"]:
                    calls = _tool_digest(m)
                    if calls:
                        lines.append("[agent used]\n" + calls)
    return "\n\n".join(lines)


def read_tools() -> list[dict]:
    """The Assistant's tools: read-only, and never the write ones."""
    return agent.tools_for(readonly=True)


def preview(rec: dict, cfg: dict | None = None) -> dict:
    """What the current settings would cost, before anything is sent."""
    cfg = cfg or settings_of(rec)
    text = digest(rec, cfg)
    parts = {
        "persona": tokens.estimate_tokens(PERSONA),
        "session": tokens.estimate_tokens(text),
        "history": sum(tokens.estimate_tokens(m.get("content"))
                       for m in (rec.get("assistant") or [])),
        "tools": tokens.estimate_tools_tokens(read_tools()) if cfg["tools"] else 0,
    }
    parts["total"] = sum(parts.values())
    return {"text": text, "turns": len(_turns(rec)), "tokens": parts,
            "display": tokens.label(parts["total"]), "exact": tokens.exact(),
            "model": cfg.get("model") or "",
            "thinking": cfg.get("thinking") or "",
            "resolved_model": resolve_model(rec, cfg),
            "resolved_thinking": resolve_thinking(rec, cfg)}


def resolve_model(rec: dict, cfg: dict | None = None) -> str:
    """The assistant's model, falling back to the session's."""
    cfg = cfg or settings_of(rec)
    return cfg.get("model") or rec.get("model") or ""


def resolve_thinking(rec: dict, cfg: dict | None = None) -> str:
    """The assistant's thinking level, falling back to the session's."""
    cfg = cfg or settings_of(rec)
    return cfg.get("thinking") or rec.get("thinking") or "default"


def system_prompt(rec: dict, cfg: dict | None = None) -> str:
    cfg = cfg or settings_of(rec)
    body = digest(rec, cfg)
    parts = [PERSONA]
    if body:
        parts.append("What has happened in the session you are helping with:\n\n" + body)
    else:
        parts.append("The session is empty so far.")
    if rec.get("project"):
        parts.append(f"The working project is {rec['project']}. You may read files there, "
                     f"but you cannot change anything.")
    return "\n\n".join(parts)


def messages_for(rec: dict, cfg: dict | None = None) -> list[dict]:
    cfg = cfg or settings_of(rec)
    out = [{"role": "system", "content": system_prompt(rec, cfg)}]
    out.extend(agent.transcript_messages(rec.get("assistant") or [],
                                         reasoning=agent.model_reasons(
                                             resolve_model(rec, cfg))))
    return out


def append(rec: dict, role: str, content: str, extra: dict | None = None) -> dict:
    row = {"role": role, "content": content, "ts": __import__("time").time()}
    if extra:
        row.update(extra)
    rec.setdefault("assistant", []).append(row)
    return row


def clear(rec: dict) -> None:
    rec["assistant"] = []


def _run_read_tool(name: str, args: dict, rec: dict) -> str:
    """Run one tool for the Assistant, refusing anything that could write.

    Filtering the schema is not the whole guard: a model that remembers a tool
    name can still call it, and several of these write in one action and read in
    another. `call_tool` is told this caller is read-only so the refusal happens
    where the call happens.
    """
    allowed = {t["function"]["name"] for t in read_tools()}
    if name not in allowed:
        return f"ERROR: the assistant may only use read-only tools, not '{name}'"
    refusal = agent.readonly_guard(name, args)
    if refusal:
        return refusal
    out: dict = {}
    ctx = {"project": rec.get("project") or None, "readonly": True,
           "depth": config.SUBAGENT_MAX_DEPTH, "session": rec.get("id") or ""}
    for _ in agent.call_tool(name, args, ctx, out):
        pass
    return out.get("result", "")


def run_turn(rec: dict, text: str, cfg: dict | None = None, ref: str | None = None,
             stop=None, max_steps: int = TOOL_LIMIT, trace: list | None = None):
    """Stream a reply from the Assistant. Yields the same event shapes as the agent."""
    cfg = cfg or settings_of(rec)
    messages = messages_for(rec, cfg)
    messages.append({"role": "user", "content": text})
    tools = read_tools() if cfg["tools"] else []
    # A reasoning-heavy model here and a cheap executor for the main work is a
    # perfectly sensible split, so both are per-session settings.
    ref = resolve_model(rec, cfg) or ref
    reasoning = config.reasoning_for(resolve_thinking(rec, cfg))

    for _step in range(max(1, max_steps)):
        calls: list[dict] = []
        step_text: list[str] = []
        step_reason: list[str] = []
        try:
            stream = engine.stream_chat(messages, ref=ref, tools=tools or None,
                                        reasoning_effort=reasoning)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": str(exc)}
            yield {"type": "done"}
            return
        try:
            for ev in stream:
                if stop is not None and stop.is_set():
                    yield {"type": "notify", "message": "stopped", "level": "info"}
                    yield {"type": "done"}
                    return
                if ev["type"] == "text":
                    step_text.append(ev["delta"])
                    yield {"type": "text", "delta": ev["delta"]}
                elif ev["type"] == "reason":
                    step_reason.append(ev["delta"])
                    yield {"type": "reason", "delta": ev["delta"]}
                elif ev["type"] == "usage":
                    yield {"type": "usage", "usage": ev["usage"]}
                elif ev["type"] == "tool_calls":
                    calls = ev["calls"]
        except engine.EngineError as exc:
            yield {"type": "error", "message": str(exc)}
            yield {"type": "done"}
            return

        narration = "".join(step_text).strip()
        thinking = "".join(step_reason).strip()

        if not calls:
            if trace is not None:
                trace.append({"text": narration, "reason": thinking, "tools": []})
            break

        messages.append({"role": "assistant", "content": narration, "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
            for c in calls]})
        step_tools: list[dict] = []
        for call in calls:
            try:
                args = __import__("json").loads(call["arguments"] or "{}")
            except Exception:  # noqa: BLE001
                args = {}
            yield {"type": "tool_start", "name": call["name"], "args": args, "id": call["id"]}
            result = _run_read_tool(call["name"], args, rec)
            result = agent._clip(str(result))
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": result})
            failed = result.startswith("ERROR:")
            step_tools.append({"id": call["id"], "name": call["name"], "args": args,
                               "result": result, "is_error": failed})
            yield {"type": "tool_end", "name": call["name"], "result": result,
                   "id": call["id"], "is_error": failed}

        if trace is not None:
            trace.append({"text": narration, "reason": thinking, "tools": step_tools})

    yield {"type": "done"}

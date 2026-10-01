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
    "turns": 20,                # how many recent turns to include
    "tools": False,             # read-only tool access, off by default
    "model": "",                # empty means: same as the session
    "thinking": "",            # empty means: same as the session
}

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

PERSONA = """You are the Assistant, a second set of eyes beside a coding agent.

The agent is doing the work. You help the person direct it. You can read the conversation between
them, but you cannot change anything yourself. Your job is the work around the work: drafting
prompts to give the agent, thinking an approach through, noticing what was missed, explaining what
the agent just did.

The agent cannot see you. Nothing you say reaches it unless the person copies it across, so write
for the person, not for the agent.

Be direct and brief. If they ask for something to paste into the main chat, give the text on its
own with nothing wrapped around it."""

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


def digest(rec: dict, cfg: dict | None = None) -> str:
    """The part of the session the Assistant is allowed to read.

    Only what is stored is available: prompts, replies and session facts.
    Reasoning and tool calls are not written to the transcript, so they are not
    here, and no switch can conjure them.
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
            if not body:
                continue
            if role == "user" and cfg["include_user"]:
                lines.append("[person] " + body)
            elif role == "assistant" and cfg["include_assistant"]:
                lines.append("[agent] " + body)
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
    return cfg.get("thinking") or rec.get("thinking") or "medium"


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
    for m in rec.get("assistant") or []:
        role, body = m.get("role"), m.get("content")
        if role in ("user", "assistant") and body:
            out.append({"role": role, "content": body})
    return out


def append(rec: dict, role: str, content: str) -> dict:
    row = {"role": role, "content": content, "ts": __import__("time").time()}
    rec.setdefault("assistant", []).append(row)
    return row


def clear(rec: dict) -> None:
    rec["assistant"] = []


def _run_read_tool(name: str, args: dict, rec: dict) -> str:
    allowed = {t["function"]["name"] for t in read_tools()}
    if name not in allowed:
        return f"ERROR: the assistant may only use read-only tools, not '{name}'"
    out: dict = {}
    ctx = {"project": rec.get("project") or None}
    for _ in agent.call_tool(name, args, ctx, out):
        pass
    return out.get("result", "")


def run_turn(rec: dict, text: str, cfg: dict | None = None, ref: str | None = None,
             stop=None, max_steps: int = TOOL_LIMIT):
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
                    yield {"type": "text", "delta": ev["delta"]}
                elif ev["type"] == "reason":
                    yield {"type": "reason", "delta": ev["delta"]}
                elif ev["type"] == "usage":
                    yield {"type": "usage", "usage": ev["usage"]}
                elif ev["type"] == "tool_calls":
                    calls = ev["calls"]
        except engine.EngineError as exc:
            yield {"type": "error", "message": str(exc)}
            yield {"type": "done"}
            return

        if not calls:
            break

        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
            for c in calls]})
        for call in calls:
            try:
                args = __import__("json").loads(call["arguments"] or "{}")
            except Exception:  # noqa: BLE001
                args = {}
            yield {"type": "tool_start", "name": call["name"], "args": args, "id": call["id"]}
            result = _run_read_tool(call["name"], args, rec)
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": result[:config.TOOL_OUTPUT_LIMIT]})
            yield {"type": "tool_end", "name": call["name"], "result": result, "id": call["id"]}

    yield {"type": "done"}

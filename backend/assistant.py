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
    "turns": 10,                # how many recent turns to include
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

Be direct and very brief: your pane is a narrow column, and a long answer is a lost answer.
Answer in a few lines, not sections — no headings unless asked, no restating the problem, no
summary of what you just said. Format in Markdown, and put every command, snippet or
prompt-to-paste inside a fenced code block with a language tag, because the interface gives
fenced blocks a one-click copy button. If they ask for something to paste into the main chat,
that block is the whole answer: no preamble before it, no commentary after.

Your most useful output is the person's next prompt to the agent, so offer one when you can see
the next step — but do not invent work. If the obvious next move is simply to let the agent carry
on, answer with the single word `Continue` in a fenced block and nothing else. One word is a
complete answer. If something specific is wrong or missing, say that instead, in the shortest
form that works."""

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
    # The contract changes when nobody is watching: in autonomous mode the
    # reply IS the control signal, so the fenced block stops being a nicety.
    if autonomy_of(rec).get("enabled"):
        parts.append(AUTONOMY_PERSONA)
    goal = goal_of(rec)
    if goal:
        parts.append("The Goal the person set for this session, which you are "
                     "working the agent toward:\n\n" + goal)
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


def delete_message(rec: dict, index: int) -> bool:
    """Remove one assistant message by index. True if something was removed.

    Indexes are positions in the stored list, which is what the pane renders,
    so the client counts the same rows it can see. Out of range is a no, not an
    exception: a stale pane may well be pointing at a row that is gone.
    """
    rows = rec.get("assistant") or []
    try:
        i = int(index)
    except (TypeError, ValueError):
        return False
    if not (0 <= i < len(rows)):
        return False
    del rows[i]
    rec["assistant"] = rows
    return True


# ── autonomous mode ────────────────────────────────────────────────────────
# The Goal and the mode's counters live as fields on the session record, not as
# messages in the transcript, for the same reason the task list does: a
# compaction rewrites the transcript and would otherwise eat the thing the
# whole run is judged against. A field cannot be compacted away.
GOAL_FIELD = "assistantGoal"
AUTONOMY_FIELD = "assistantAutonomy"

AUTONOMY_DEFAULTS = {
    "enabled": False,
    "cycles": 0,          # agent turns the harness has launched this run
    "work_seconds": 0.0,  # measured agent working time, not wall clock
    "max_cycles": 20,
    "max_hours": 5.0,
    "reason": "",         # why it last stopped, for the pane to show
    "failures": 0,        # consecutive launches that failed; two ends the run
}

# How many launches in a row may fail before the harness gives up. One is a
# provider that hiccuped; a third is a person watching a loop that cannot do
# anything and paying for it in budget.
MAX_LAUNCH_FAILURES = 2

# The block the harness sends the Assistant at the end of every autonomous
# turn. A fixed string, so the pane shows the same prompt it really sent.
CYCLE_PROMPT = ("The agent's turn just ended. Read what happened and say, in a "
                "line or two, whether it moves the Goal along. Put a fenced "
                "block with a prompt for the agent only if it needs direction "
                "it would not otherwise have — if the run is on course, leave "
                "the block out and it carries on. When the Goal is fully met, "
                "put exactly DONE in the block.")

# What the harness sends when the Assistant answered with no prompt at all:
# no block means it saw nothing wrong with carrying on.
CONTINUE_PROMPT = "Continue."

DONE = "DONE"

# The autonomous contract, and what it deliberately does NOT say. The reply used
# to be required to end in a fenced block every single time, which made the
# Assistant a prompt generator that never answered anything: a person asking it
# a question got a code block back. It is still the Assistant — brief, talking
# to the person — and the block is now an offer, not a tax. The one block that
# must never be left out is DONE, because it is the only thing the harness reads
# as a stop; prose saying "the goal is met" means nothing to the loop.
AUTONOMY_PERSONA = """You are running autonomously: the person is not watching live, and the
agent acts on exactly what you hand it. You are still the Assistant, still answering the person in
your own pane, so stay brief and do not turn every reply into a prompt.

Add a fenced code block only when the agent needs direction it would not otherwise have: the next
step, a correction, something to stop doing. If the work is on course and the agent should simply
carry on, say so in a line and leave the block out — the harness lets the run continue, and a run
that needs no steering does not need steering from you.

One exception, and it is not optional: when the Goal is fully met, end with a fenced block holding
exactly `DONE`. That block is the only thing that stops the run. Saying it in prose does nothing."""


def goal_of(rec: dict) -> str:
    return str(rec.get(GOAL_FIELD) or "")


def autonomy_of(rec: dict) -> dict:
    """The mode's state, with defaults filled in for anything older on disk."""
    stored = rec.get(AUTONOMY_FIELD) or {}
    out = dict(AUTONOMY_DEFAULTS)
    for key in AUTONOMY_DEFAULTS:
        if key in stored and stored[key] is not None:
            out[key] = stored[key]
    out["enabled"] = bool(out["enabled"])
    out["cycles"] = int(out["cycles"] or 0)
    out["work_seconds"] = float(out["work_seconds"] or 0.0)
    out["max_cycles"] = max(1, int(out["max_cycles"] or 0))
    out["max_hours"] = max(0.1, float(out["max_hours"] or 0.0))
    out["failures"] = max(0, int(out["failures"] or 0))
    return out


def save_autonomy(rec: dict, changes: dict) -> dict:
    """Validate a change to the mode's state and return the whole new state.

    Counters are not settable from outside: a panel that posts its last-seen
    cycle count after the harness has run three more turns would silently
    rewind the budget. Only the switches and the limits are.
    """
    state = autonomy_of(rec)
    for key, value in (changes or {}).items():
        if key not in ("enabled", "max_cycles", "max_hours", "reason"):
            continue
        if key == "enabled":
            state["enabled"] = bool(value)
        elif key == "max_cycles":
            try:
                state["max_cycles"] = min(500, max(1, int(value)))
            except (TypeError, ValueError):
                pass
        elif key == "max_hours":
            try:
                state["max_hours"] = min(48.0, max(0.1, float(value)))
            except (TypeError, ValueError):
                pass
        else:
            state["reason"] = str(value or "")[:200]
    rec[AUTONOMY_FIELD] = state
    return dict(state)


def bump_counters(rec: dict, *, cycles: int = 0, work_seconds: float = 0.0) -> dict:
    """Add to the counters, the only way they move.

    Deliberately additive: the harness is the only writer, and a run that
    restarted mid-flight continues the same tally rather than resetting it.
    """
    state = autonomy_of(rec)
    state["cycles"] = int(state["cycles"]) + max(0, int(cycles))
    state["work_seconds"] = round(float(state["work_seconds"]) + max(0.0, float(work_seconds)), 2)
    rec[AUTONOMY_FIELD] = state
    return dict(state)


def reset_counters(rec: dict) -> dict:
    """Zero the tally, keeping the limits and the Goal. Called on enable, so
    pressing the switch is the start of a run rather than a continuation of
    the last one."""
    state = autonomy_of(rec)
    state["cycles"] = 0
    state["work_seconds"] = 0.0
    state["reason"] = ""
    state["failures"] = 0
    rec[AUTONOMY_FIELD] = state
    return dict(state)


def note_launch(rec: dict, *, ok: bool) -> dict:
    """Record whether the harness managed to launch the agent this cycle.

    The counter is the harness's, never the panel's: a run that launches fine
    clears it, a run that cannot increments it, and two in a row is what the
    caller uses to end the loop. Kept on disk so a server restart mid-run does
    not hand a broken session a fresh allowance of failures.
    """
    state = autonomy_of(rec)
    state["failures"] = 0 if ok else int(state["failures"]) + 1
    rec[AUTONOMY_FIELD] = state
    return dict(state)


def budget_left(state: dict) -> tuple[bool, str]:
    """Whether the run may launch another agent turn, and why not if it may not."""
    if int(state.get("cycles") or 0) >= int(state.get("max_cycles") or 1):
        return False, f"reached the limit of {state.get('max_cycles')} turns"
    hours = float(state.get("max_hours") or 0)
    if float(state.get("work_seconds") or 0) >= hours * 3600:
        return False, f"reached the limit of {hours:g} hours of work"
    return True, ""


def goal_block(rec: dict) -> dict:
    """What pinning the Goal would cost, as a report. {} when there is no Goal.

    This is NOT injected into the agent, and nothing on the agent's side calls
    it. The Goal belongs to the Assistant alone: the agent is told only whatever
    prompt the Assistant chose to write for it, so wiring this into a turn would
    be the leak, not a fix. It is kept because the Goal rides on the record
    rather than in the transcript — a field survives compaction, a message does
    not — and the cost of that standing block is worth being able to state.
    """
    goal = goal_of(rec)
    if not goal:
        return {}
    text = ("Goal for this session — the objective the work is judged against. "
            "It stays in front of you for every turn and outranks any instruction "
            "to wrap up early.\n\n" + goal)
    return {"text": text, "tokens": tokens.estimate_tokens(text), "chars": len(goal)}


def extract_prompt(text: str) -> str:
    """The last fenced block in an Assistant reply: the prompt to hand the agent.

    Empty string when the reply has no block, which the caller reads as
    "nothing to say, carry on". The *last* block, because the persona puts the
    handoff at the end and a reply may well quote something else earlier.
    """
    body = str(text or "")
    blocks: list[str] = []
    fence = None
    current: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if fence is None:
            if stripped.startswith("```"):
                fence = stripped[3:]
                current = []
            continue
        if stripped.startswith("```"):
            blocks.append("\n".join(current).strip())
            fence = None
            current = []
            continue
        current.append(line)
    if fence is not None and current:
        # An unterminated block is still what the model meant to hand over:
        # a reply cut off mid-stream should not cost the whole turn's work.
        blocks.append("\n".join(current).strip())
    return blocks[-1] if blocks else ""


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

"""The small-context ("fresh call") briefing, T1 (PLAN 21).

A 16k window is a throughput limit, never a memory limit. In this mode the
transcript is not the state: every model call is a freshly composed session -
the standing prompt, a harness-tracked task capsule, the goal as the user
wrote it, at most the previous step verbatim, and one action request. The
capsule is built and updated deterministically from evidence the loop already
holds (writes, failed attempts, verified runs, budget), never by asking a
small model to summarize itself; the disk is the memory, and anything older
than the tail is re-fetched with read_file or grep.
"""

import json
import time

from . import config

MODE_NOTE = (
    "Small-context mode is on: this request is a fresh briefing, not a "
    "continuation of a transcript. The disk is the memory; TASK STATE below is "
    "what the harness has tracked, and older steps are not present. Re-read "
    "anything with read_file or grep instead of assuming; never claim a file "
    "exists or passes without looking.")

ACTION = ("Make exactly ONE tool call now that serves the goal, with a "
          "one-line reason first. When nothing remains, reply `DONE: <one-line "
          "summary>` with no tool call.")

_GOAL_MAX = 6000
_ASSISTANT_MAX = 500
_TOOL_MAX = 900
_USER_MAX = 1400
_OVERHEAD = 12  # role/framing characters charged per row


def goal_of(messages) -> str:
    """The task as the user last stated it: the last user row's text."""
    goal = ""
    for m in (messages or []):
        if isinstance(m, dict) and m.get("role") == "user" \
                and str(m.get("content") or "").strip():
            goal = str(m.get("content"))
    return goal.strip()


def state_text(*, written, pending, verified, last_error, calls_left,
               steps_left, seconds_left, loaded) -> str:
    """The deterministic capsule: harness evidence only, never model prose."""
    lines = ["TASK STATE (harness-tracked; this is the whole picture):"]
    carried = sorted({str(a).rsplit("/", 1)[-1]
                      for a in (loaded.get("artifacts") or [])})
    if carried:
        lines.append("artifacts that exist from earlier turns of this task: "
                     + ", ".join(carried))
    if written:
        lines.append("written this turn: " + ", ".join(sorted(written)))
    if pending:
        lines.append("attempted this turn but ABSENT on disk: "
                     + ", ".join(sorted(pending)))
    lines.append("verification: "
                 + ("a run after the last write passed"
                    if verified else "not verified yet"))
    if last_error:
        lines.append("last tool error: " + str(last_error)[:220])
    budget = f"{calls_left} calls, {steps_left} steps"
    if seconds_left is not None:
        budget += f", {max(0, int(seconds_left))}s of wall left"
    lines.append("budget remaining: " + budget)
    return "\n".join(lines)


def _chars(rows) -> int:
    return sum(_OVERHEAD + len(str(r.get("content") or "")) for r in rows)


def _clip(text, limit) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + " ...[clipped]"


def _clip_row(m: dict) -> dict:
    role = m.get("role")
    out = {k: v for k, v in m.items() if k not in ("content",)}
    if role == "assistant":
        out["content"] = _clip(m.get("content"), _ASSISTANT_MAX)
        if m.get("tool_calls"):
            out["tool_calls"] = m["tool_calls"]
        if m.get("reasoning_content"):
            out["reasoning_content"] = ""
    elif role == "tool":
        out["content"] = _clip(m.get("content"), _TOOL_MAX)
    else:
        out["role"] = "user"
        out["content"] = _clip(m.get("content"), _USER_MAX)
    return out


def view(standing, goal, state, tail, budget_chars, extra_user=None) -> list[dict]:
    """One fresh briefing: standing prompt, capsule, goal, tail, action.

    Fits inside `budget_chars` (the per-call ceiling) by dropping the oldest
    tail rows first, then tightening the goal; the standing prompt, the
    capsule and the action request are never dropped, because they are the
    parts that make the call a session of its own. Rows keep provider-shape
    validity: an assistant tool_calls row and its tool answers stay paired.
    """
    rows: list[dict] = []
    if standing is not None:
        rows.append(standing)
    rows.append({"role": "system", "content": MODE_NOTE + "\n\n" + state})
    tail = [_clip_row(m) for m in (tail or []) if m.get("role") != "system"]
    action = extra_user or ACTION
    fixed = _OVERHEAD + len(action) + _OVERHEAD + len(MODE_NOTE) + len(state) \
        + (2 * _OVERHEAD if standing is not None else 0) \
        + (len(str((standing or {}).get("content") or ""))
           if standing is not None else 0)

    def build(goal_text, tail_rows):
        rows = ([standing] if standing is not None else [])
        rows.append({"role": "system", "content": MODE_NOTE + "\n\n" + state})
        rows.append({"role": "user", "content": goal_text})
        rows.extend(tail_rows)
        rows.append({"role": "user", "content": action})
        return rows

    # The ceiling is law. Oldest tail rows go first; the goal then gives
    # ground by halving, down to a floor; the capsule and the action request
    # never do, because they are the briefing itself. The loop terminates by
    # construction: the halving reaches the floor and stops there.
    floor = 400
    goal_limit = min(_GOAL_MAX, max(floor, int(budget_chars) - fixed))
    kept = list(tail)
    rows = build(_clip(goal, goal_limit), kept)
    while _chars(rows) > budget_chars:
        if kept:
            kept.pop(0)
            rows = build(_clip(goal, goal_limit), kept)
            continue
        if goal_limit <= floor:
            break  # the briefing itself is the budget: ship it honest, not trimmed empty
        goal_limit = max(floor, goal_limit // 2)
        rows = build(_clip(goal, goal_limit), kept)
    return rows


def path_for(session: str):
    if not session:
        return None
    try:
        return config.workspace_dir(session) / "capsule.json"
    except Exception:  # noqa: BLE001
        return None


def save(session: str, *, written, pending, verified) -> None:
    """Persist the durable slice of the state: artifacts and verification.

    Written on every fresh briefing, so a turn that dies mid-task leaves the
    next one a state file, not a shrug. Basenames only: the capsule names what
    exists, the model re-derives paths from the goal.
    """
    path = path_for(session)
    if path is None:
        return
    current = {}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        current = {}
    arts = {str(a).rsplit("/", 1)[-1]
            for a in (current.get("artifacts") or [])}
    arts |= {str(w).rsplit("/", 1)[-1] for w in (written or []) if w}
    payload = {"artifacts": sorted(arts),
               "pending": sorted({str(p).rsplit("/", 1)[-1]
                                  for p in (pending or []) if p}),
               "verified": bool(verified),
               "ts": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def load(session: str) -> dict:
    path = path_for(session)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}

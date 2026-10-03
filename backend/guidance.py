"""Per-turn guidance, the way little-coder does it.

The standing prompt stays small so it is cheap and cacheable, and short
instructions are instead appended at the point of use: after a tool fails, when
work is being repeated, when the step budget is nearly gone. Nothing here is
stored in the transcript, nothing here costs a model call, and no block is
repeated while the previous one still applies.
"""

import re

# Character ceiling for a block. Guidance that outgrows the task description stops
# being guidance.
MAX_BLOCK = 420

# What to say when a specific tool has just failed. Keyed by tool name.
CARDS = {
    "list_files": "list_files takes a path relative to the working project. A bare name is not a "
                  "path: pass the directory you mean, or '.' for the root.",
    "read_file": "read_file returns numbered lines and ends with the range it showed plus the "
                 "offset to continue from. Use that offset for the next chunk instead of "
                 "re-reading from the top, and edit with the exact text you read.",
    "edit_file": "edit_file needs `find` to match the file exactly, whitespace included, and to "
                 "appear once. Read the file first so the match is real; `replace` is the new "
                 "text. To create a new file use write_file.",
    "write_file": "write_file replaces the whole file. To change an existing file, use edit_file on "
                  "the specific lines rather than retyping the file.",
    "glob_files": "glob_files is bounded: pass a base path so the walk stays in the project instead "
                  "of matching the entire home directory.",
    "grep_files": "grep_files searches contents. Pattern plus a likely path beats reading the "
                  "project one file at a time; pass a file to search that file only.",
    "run_shell": "run_shell is stateless: each call starts fresh, so cd does not persist. Use absolute "
                 "paths or chain with &&. Set a longer timeout for installs, builds and downloads.",
    "bg_output": "bg_output should be read when you have a reason to, not in a loop. Polling a "
                 "background job spends the turns the task still needs.",
    "bg_start": "bg_start returns when the job signals; do not sit on bg_output waiting for it.",
    "skill": "skill takes a name from the listed skills. Read it before acting on it rather than "
             "guessing at what it says.",
    "task": "A task subagent is read-only: it gathers facts and returns a summary. The edits are "
            "still yours to make.",
    "fetch": "fetch caps the response and may truncate. Ask for a narrower URL than you think you "
             "need.",
    "browser": "browser: navigate first, then extract. Extract returns a chunk, so advance the "
               "cursor rather than re-reading the same span.",
    "restore": "restore acts on a snapshot id from list_snapshots. Confirm the id before restoring.",
}

_DISCOVER = ("Before changing files, read the project's own instructions if it has them (README, "
             "AGENTS.md, docs/) and the file you intend to edit. Do this once at the start, not on "
             "every turn.")
_REPEAT = ("{names} with those arguments already ran this turn and its result is above. Do the next "
           "thing instead of re-exploring the same ground.")
_BUDGET = ("{n} step(s) left in this turn. Stop gathering and answer now: the finding first, then "
           "what you changed if anything, then what is still unresolved.")
_FAILED = ("The last step's tool call failed{detail}. Fix the cause before repeating it: wrong path, "
           "wrong arguments, or a tool this mode does not allow.")
_DO_IT = ("Do it now with your tools. Do not describe what you are going to do next.")
_CLOSING = ("Out of steps for this turn. Answer now without using any more tools: the finding "
            "first, then what you changed if anything, then what is still unresolved.")

# Both of the above used to open with "what you changed". On an investigation
# task the honest answer to that is "nothing", so every report opened by proving
# it had been harmless and spent its closing tokens on that instead of on the
# answer. The finding leads; the change report still happens, second.
_TIMEOUT = ("A call timed out{detail}. Do not re-run the same command shape: on Windows a piped "
            "find/findstr/more can block forever on a full pipe buffer. Take a different route, or "
            "pass a longer `timeout` if the work is genuinely slow.")

# A step that talks about doing something but runs no tool is a plan, not progress.
_PLAN = re.compile(
    r"\b(i'?ll|i will|let me|let's|first,? i|now i(?:'ll| will)|i (?:plan|need|want|am going) to|"
    r"time to|next,? i|going to)\b", re.I)
# ...unless it clearly contains an answer already.
_ANSWERED = re.compile(r"```|\|\s|^\s*[-*]\s|^\s*\d+[\.\)]\s|total|error|fixed|pass(?:ed|ing)\b",
                       re.I | re.M)

_enabled: bool | None = None
_on_state = None      # cached config lookup, reset by set_enabled


def enabled() -> bool:
    """Guidance is on unless the user turns it off in the harness panel."""
    global _enabled, _on_state
    if _enabled is False:
        return False
    try:
        from . import providers
        cfg = (providers.load() or {}).get("guidance") or {}
        return bool(cfg.get("enabled", True))
    except Exception:
        return True


def set_enabled(on: bool) -> None:
    global _enabled
    _enabled = bool(on)


def plan_like(text: str) -> bool:
    """True when a step with no tool call reads as intent rather than as a result.

    Small and phrased as future work: "I'll start by exploring" is a plan. A step
    that carries code, a list, a count or an error is an answer, however short.
    """
    body = (text or "").strip()
    if not body or len(body) > 400:
        return False
    if _ANSWERED.search(body):
        return False
    return bool(_PLAN.search(body))


def _clip(s: str) -> str:
    return s[:MAX_BLOCK].rstrip()


def for_step(*, step: int = 0, steps: int = 0, errors=(), repeated=(),
             first: bool = False, project: bool = False,
             previous: str = "") -> str:
    """Return the block for this step, or "" for none.

    Priority follows little-coder: a failure first, then the state that changed
    most recently, then standing advice. One block per step, so the model is never
    handed a wall of meta-instructions.
    """
    if not enabled():
        return ""

    if errors:
        names = [str(e[0]) for e in errors]
        detail = ""
        for n in names:
            if n in CARDS:
                detail = " " + CARDS[n]
                break
        # A timeout is its own failure: the generic "fix the cause" advice sends
        # the model straight back into the same hanging command shape, and one
        # hang costs three minutes of a turn at the default limit.
        if any("timed out" in str(e[1]) for e in errors):
            text = _TIMEOUT.format(
                detail=" (" + ", ".join(dict.fromkeys(names)) + ")")
        else:
            text = _FAILED.format(detail=" (" + ", ".join(dict.fromkeys(names)) + ")") + detail
    elif repeated:
        text = _REPEAT.format(names=", ".join(dict.fromkeys(repeated)))
    elif steps and steps - step <= 2:
        text = _BUDGET.format(n=max(0, steps - step))
    elif first and project:
        text = _DISCOVER
    else:
        return ""

    text = _clip(text)
    # A block that is already in force is not worth paying for twice in a row.
    if text == previous:
        return ""
    return text


def nudge() -> str:
    """The instruction sent when a step planned instead of acting."""
    return _clip(_DO_IT)


def closing() -> str:
    """The instruction sent when the step budget ran out mid-work."""
    return _clip(_CLOSING)

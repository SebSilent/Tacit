"""Per-turn guidance blocks, a pattern credited to little-coder and fitted to Tacit's budgets.

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
# The failure this exists for: a cell that spent 59 shell calls deriving the same fact and never
# wrote the file the instruction named, so the checker read an empty directory and scored 0.
# Advice to "answer now" is worthless if the deliverable does not exist yet.
_MISSING = ("{names} is named in the task and does not exist yet. Write it now with write_file "
            "before you run out of steps: a report that only describes the file scores nothing.")
_BUDGET = ("{n} step(s) left in this turn. Stop gathering and answer now: the finding first, then "
           "what you changed if anything, then what is still unresolved.")
_FAILED = ("The last step's tool call failed{detail}. Fix the cause before repeating it: wrong path, "
           "wrong arguments, or a tool this mode does not allow.")
_DO_IT = ("Do it now with your tools. Do not describe what you are going to do next.")
_CLOSING = ("Out of steps for this turn. Answer now without using any more tools: the finding "
            "first, then what you changed if anything, then what is still unresolved.")

# The closing call offers no tools, which is correct for a report and fatal for a task whose score
# comes from a file: the checker reads the disk. This is the one round allowed to write, and it is
# the last one, so a cell that derived the answer and never wrote it down still gets a file.
_SALVAGE_ONE = ("Out of steps, and {names} is not written. Write it now with write_file, from what you "
            "have already established. An approximation you can defend scores; a plan you never "
            "ran scores nothing. Write the file first, then run it and fix what the run shows. "
            "If a tool call fails or you cannot make one, reply with the COMPLETE file in one "
            "fenced code block and nothing else - it will be written to {names} for you.")
# Several tasks are scored on more than one file (one plan per input bucket, say). Asked in the
# singular, the model writes one of them and the round is gone: cells read "missing required output
# file: plan_b1.jsonl" with the sibling already on disk. The calls are independent and a single round
# can carry all of them, so the wording has to ask for every file at once.
_SALVAGE_MANY = ("Out of steps, and none of these are written: {names}. Write every one of them now, "
                 "each with its own write_file call in this one round — they are independent, so one "
                 "round can produce all of them. An approximation you can defend scores; a plan you "
                 "never ran scores nothing. If a tool call fails, reply with each complete file in "
                 "its own fenced code block - they will be written for you.")


def salvage(names) -> str:
    """The instruction for the final round that may still write the deliverable."""
    wanted = [str(n) for n in (names or [])][:3]
    if len(wanted) > 1:
        return _clip(_SALVAGE_MANY.format(names=", ".join(wanted)))
    return _clip(_SALVAGE_ONE.format(names=wanted[0] if wanted else "the file"))


# The salvage rounds above are satisfied as soon as the file exists. Two hard-set cells failed
# exactly that way: eval.scm was written, never run, and the checker reported `Unexpected closing
# parenthesis` and `Undefined variable: error`. Landing a file is not the finish line; the task's
# own commands are. This is the round that runs them.
_VERIFY = ("{names} exists, but it was never executed. Run the check the task itself names, "
           "then repair the file with edit_file and run it again. Run EVERY test the workspace "
           "ships through your deliverable, not just the example in the instruction: the grader's "
           "hidden tests are the same shape as the visible ones, so the visible set is the only "
           "proxy you have. An unrun deliverable scores zero even when the code is nearly right.")


def verify(names) -> str:
    """Ask for execution rather than authoring, when the files exist but were never run."""
    wanted = [str(n) for n in (names or [])][:3]
    return _clip(_VERIFY.format(names=", ".join(wanted) or "The file"))


# Ordered in the middle of a turn, not offered at the end of one. Every zero-artifact cell in the
# hard set - feal t2 at 16 turns, schemelike t3 at 34 - ran out of turn while still investigating,
# and a phase that only fires when the model chooses to stop never reaches them.
_DEMAND = ("{used} of this turn is spent and the file the task names is still not on disk: "
           "{names}. Nothing else in this turn is graded. Put it on disk now with write_file "
           "from what you already know - a partial file that runs beats a plan that never lands.")
_DEMAND_LATE = ("{names} is STILL not on disk and this turn is almost over. Stop investigating. "
                "One write_file call now with your best current answer, then run it.")


def demand(names, used="", nth=1) -> str:
    """The mid-turn order to produce the artifact, escalating once."""
    wanted = ", ".join(str(n) for n in (names or [])[:3]) or "the file"
    if nth > 1:
        return _clip(_DEMAND_LATE.format(names=wanted))
    return _clip(_DEMAND.format(names=wanted, used=used or "Half"))


# When the clock becomes information. The measured hard-set cells run 900-1,010s inside a 1,175s
# allowance, so the last two minutes are the difference between a file that exists and a turn that
# gets abandoned mid-sentence.
LATE_S = 420
# One salvage round, not three - but it has to be reachable. At 120s the guard never fired in
# practice: a step costs 30-90s and the kill lands mid-step, so feal trial 1 of s2 died at 1,081s
# with `attack.py does not exist` and no delivery attempted. 300s is the smallest window in which
# one write plus one run can actually complete before the clock.
EMERGENCY_S = 300

_TIME = ("{left}s of wall-clock budget remain for this task, and at zero the run is killed "
         "mid-sentence with nothing graded. Stop probing and stop verifying. If the deliverable "
         "is not on disk yet, write it now from what you already know - a rough file that exists "
         "outscores a plan that never landed.")


def time_left(left) -> str:
    return _clip(_TIME.format(left=int(max(0, float(left)))))

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


def for_step(*, step: int = 0, steps: int = 0, errors=(), repeated=(), missing=(),
             first: bool = False, project: bool = False,
             previous: str = "") -> str:
    """Return the block for this step, or "" for none.

    Priority order, a pattern credited to little-coder: a failure first, then the state that changed
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
    elif missing:
        # Ahead of the repeat nudge: breaking the loop only helps if the run ends with
        # the artifact on disk, which is what is actually graded.
        text = _MISSING.format(names=", ".join(dict.fromkeys(missing)))
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

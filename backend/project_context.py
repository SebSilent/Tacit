"""A project's own instructions, found rather than guessed at.

Every other harness in this class loads the project's instruction file into the
prompt automatically. Tacit did not: it handed the model a line of guidance saying
"read the project's own instructions if it has them" and relied on compliance, which
meant a wasted tool call per session when the model obeyed and a silently
uninformed agent when it did not.

The tension is real, and it is the reason this module has three modes rather than
one. Inlining `AGENTS.md` enlarges the starting prompt, and the size of that prompt
is the number Tacit exists to keep small. So the default is an *index*: the names
and sizes of what was found, which is enough for the model to read the right file
on the first try instead of guessing at names. Full inlining is available, is
budgeted, and reports its cost like every other feature here.

A design rule from the README applies unchanged: a feature that enlarges the
starting prompt must be optional and must show its cost.
"""

from __future__ import annotations

from pathlib import Path

from . import config, tokens

# Conventional names, most specific first. A project that has both AGENTS.md and
# CLAUDE.md gets both listed; they are not interchangeable and picking one silently
# would be a guess.
NAMES = ("AGENTS.md", "CLAUDE.md", "TACIT.md", "PLAN.md", ".cursorrules",
         ".tacit/instructions.md", ".github/copilot-instructions.md")

# PLAN.md is here because plan mode writes it: approving a plan puts the plan in
# the project as its own instruction file, and this list is what the prompt is
# built from. It sits among the instruction names rather than in DOCS below
# because DOCS is only ever *listed* in index mode, and an approved plan that is
# merely named in the prompt is a plan the agent may read or may not.

# The stamp plan mode puts on the first line of every file it writes into a
# project. The name lives here rather than in plan.py because this module is the
# one that has to judge a file it did not write: a project is allowed to have its
# own PLAN.md, and when it does the approved plan lands beside it as
# PLAN-<session>.md. Without a way to tell the two apart, discover() would either
# ignore a plan the person approved or promote a stranger's file to instruction
# status, so the marker is the contract between the writer and the reader.
PLAN_MARK = "tacit:approved-plan"
PLAN_MARK_LINE = f"<!-- {PLAN_MARK} -->"

# Session-scoped plan files are found by pattern, not by name: the session id is
# not knowable here. A glob is also what makes them eligible at all — a plan the
# prompt never sees is a plan that only exists on disk.
PLAN_GLOB = "PLAN-*.md"

# Documentation rather than instructions. Most real projects have no AGENTS.md and
# put everything in the README, so a project scan that ignored it found nothing at
# all. These are listed in index mode — naming a file and its size is nearly free,
# and it stops the model guessing at what exists — but they are deliberately not
# preferred for inlining, because a README is usually far too large to be worth the
# standing prompt space and reading it on demand costs the same as it ever did.
DOCS = ("README.md", "SPEC.md", "CONTRIBUTING.md")

MODES = ("off", "index", "inline")
DEFAULT_MODE = "index"
DEFAULT_BUDGET = 1200          # tokens, inline mode only
MAX_FILES = 6


def settings() -> dict:
    """The configured mode and budget, with defaults filled in."""
    try:
        from . import providers
        cfg = (providers.load() or {}).get("context") or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    mode = str(cfg.get("instructions") or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        mode = DEFAULT_MODE
    try:
        budget = int(cfg.get("instruction_budget") or DEFAULT_BUDGET)
    except (TypeError, ValueError):
        budget = DEFAULT_BUDGET
    return {"mode": mode, "budget": max(0, min(budget, 20000))}


def save(patch: dict) -> dict:
    """Persist a mode or budget change through the capability registry."""
    from . import providers
    current = (providers.load() or {}).get("context") or {}
    merged = {**current}
    if "instructions" in patch:
        mode = str(patch["instructions"] or DEFAULT_MODE).strip().lower()
        merged["instructions"] = mode if mode in MODES else DEFAULT_MODE
    if "instruction_budget" in patch:
        try:
            merged["instruction_budget"] = max(0, min(int(patch["instruction_budget"]), 20000))
        except (TypeError, ValueError):
            pass
    providers.save({"context": merged})
    return settings()


def _is_plan_name(name: str) -> bool:
    """PLAN.md and the session-scoped files plan mode falls back to."""
    return name == "PLAN.md" or (name.startswith("PLAN-") and name.endswith(".md"))


def _candidates(root: Path) -> list[str]:
    """The names discover() looks for, in the order the prompt is built from.

    NAMES (most specific first), then DOCS. A session-scoped plan is slotted in
    right after PLAN.md rather than appended at the end: it is an instruction, and
    MAX_FILES cuts the list from the tail, so a crowded project root should lose
    its CONTRIBUTING.md before it loses a plan somebody approved. Newest first,
    because a project holding several approved plans is a project where the most
    recent one is the live one.
    """
    names = list(NAMES) + list(DOCS)
    try:
        extra = [p.name for p in root.glob(PLAN_GLOB) if p.name not in names]
        extra.sort(key=lambda n: (root / n).stat().st_mtime, reverse=True)
    except Exception:  # noqa: BLE001 — an unreadable root still has its fixed names
        return names
    if not extra:
        return names
    at = NAMES.index("PLAN.md") + 1
    return names[:at] + extra + names[at:]


def discover(project: str | None) -> list[dict]:
    """Instruction files that actually exist in the project root.

    Root only, deliberately. Walking the tree for CLAUDE.md files in every
    subdirectory is how a harness ends up reading a vendored copy of someone
    else's instructions, and it costs a scan on every turn.
    """
    out: list[dict] = []
    if not project:
        return out
    try:
        root = Path(project).expanduser()
    except Exception:  # noqa: BLE001
        return out
    if not root.is_dir():
        return out
    for name in _candidates(root):
        try:
            path = root / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        # A PLAN-*.md without the marker is not an instruction. The glob is wider
        # on purpose than what plan mode writes — projects keep PLAN-phase2.md and
        # PLAN-old.md and october-roadmap names nobody meant for the agent — so the
        # stamp earns a place in the prompt, not the filename.
        if name not in NAMES and name not in DOCS and PLAN_MARK not in text:
            continue
        out.append({"name": name, "path": str(path), "chars": len(text),
                    "tokens": tokens.estimate_tokens(text), "text": text,
                    "kind": "instructions" if (name in NAMES or _is_plan_name(name))
                            else "docs"})
        if len(out) >= MAX_FILES:
            break
    return out


def block(project: str | None, cfg: dict | None = None) -> dict:
    """What goes into the prompt for this project, and what it costs.

    Returns ``{"text", "tokens", "mode", "files", "held_back"}``. ``text`` is empty
    when the mode is off or nothing was found, so callers can append it unconditionally.
    """
    cfg = cfg or settings()
    mode = cfg["mode"]
    empty = {"text": "", "tokens": 0, "mode": mode, "files": [], "held_back": 0}
    if mode == "off" or not project:
        return empty
    found = discover(project)
    if not found:
        return empty

    if mode == "index":
        lines = ["This project has its own instructions and documentation. They are not in "
                 "your context yet — read the ones that matter before changing anything:"]
        lines += [f"- {f['name']} ({f['tokens']} tokens)" for f in found]
        text = "\n".join(lines)
        return {"text": text, "tokens": tokens.estimate_tokens(text), "mode": mode,
                "files": [{"name": f["name"], "tokens": f["tokens"], "kind": f["kind"]}
                          for f in found],
                "held_back": sum(f["tokens"] for f in found)}

    # Inline: full text, up to the budget, instructions before documentation. What
    # did not fit is reported rather than silently dropped, so the dashboard can say
    # what the budget cost.
    budget = int(cfg["budget"] or 0)
    ordered = sorted(found, key=lambda f: 0 if f["kind"] == "instructions" else 1)
    chosen, used, held = [], 0, 0
    for f in ordered:
        if budget and used + f["tokens"] > budget:
            held += f["tokens"]
            continue
        chosen.append(f)
        used += f["tokens"]
    if not chosen:
        return {**empty, "held_back": held}
    body = "\n\n".join(f"### {f['name']}\n{f['text'].strip()}" for f in chosen)
    text = ("The project's own instructions follow. They outrank your general habits "
            "where they conflict.\n\n" + body)
    return {"text": text, "tokens": tokens.estimate_tokens(text), "mode": mode,
            "files": [{"name": f["name"], "tokens": f["tokens"]} for f in chosen],
            "held_back": held}


def cost_preview(project: str | None) -> dict:
    """What each mode would cost for this project, before it is switched on."""
    found = discover(project)
    total = sum(f["tokens"] for f in found)
    index_cost = 0
    if found:
        lines = 1 + len(found)
        index_cost = tokens.estimate_tokens("x" * (110 + 40 * lines))
    return {
        "mode": settings()["mode"],
        "budget": settings()["budget"],
        "files": [{"name": f["name"], "tokens": f["tokens"], "chars": f["chars"]}
                  for f in found],
        "found": len(found),
        "cost_off": 0,
        "cost_index": index_cost,
        "cost_inline": total,
        "modes": list(MODES),
        "exact": tokens.exact(),
        "note": ("nothing found in this project" if not found else
                 f"{len(found)} file(s), {total} tokens if inlined in full"),
    }

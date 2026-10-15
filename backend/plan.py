import json
import re
from datetime import date
from pathlib import Path

from . import agent, config, project_context
from .ai import engine, prompts

DECOMPOSE = """You are planning a coding task.

Request: {prompt}

List the places in the codebase worth inspecting before writing a plan. Reply with a JSON array
only, at most {limit} entries, each {{"label": "short name", "task": "what to find out"}}.
No prose, no code fences."""

QUESTIONS = """You are planning a coding task.

Request: {prompt}

What you found while exploring:
{findings}

Ask at most {limit} questions whose answers would change the plan. Reply with a JSON array only,
each {{"q": "the question", "options": ["option", "option"]}} - 2-4 concrete options each.
No prose, no code fences."""

SYNTHESISE = """You are writing an implementation plan for a coding task.

Request: {prompt}

What you found while exploring:
{findings}

The user's answers:
{answers}

Write the plan as markdown. Use these sections: Goal, Approach, Files to change, Steps, Risks.
Be specific and concrete, name real files and functions. Do not write the code, describe the
changes. Keep it tight."""

FALLBACK_TASK = {"label": "codebase", "task": "map the files most relevant to the request"}


def _array(text: str, limit: int) -> list:
    body = str(text or "")
    fence = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if fence:
        body = fence.group(1)
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = body.find(opener), body.rfind(closer)
        if 0 <= start < end:
            try:
                parsed = json.loads(body[start:end + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                parsed = [parsed]
            if isinstance(parsed, list):
                out = [x for x in parsed if isinstance(x, dict)]
                if out:
                    return out[:limit]
    return []


class Plan:
    def __init__(self, sid: str, prompt: str, project: str | None, ref: str | None):
        self.sid = sid
        self.prompt = prompt
        self.project = project
        self.ref = ref
        self.phase = "decomposing"
        self.detail = "breaking the request into areas to inspect"
        self.explorers: list[dict] = []
        self.findings: list[str] = []
        self.questions: list[dict] = []
        self.answers: dict = {}
        self.text = ""
        self.cancelled = False
        self.saved = False
        self.plan_file = ""
        self.context_file = ""

    def status(self, phase: str, detail: str = "", explorers=None) -> dict:
        self.phase = phase
        self.detail = detail
        return {"type": "plan_status", "phase": phase, "detail": detail,
                "explorers": explorers if explorers is not None else self.explorers}


def snapshot(p: Plan) -> dict:
    """The plan as data, so it can live somewhere other than this process.

    Plan mode used to exist only inside one socket's memory, which made a page
    reload a lost plan. Everything the interface needs to redraw the strip,
    phase, the explorers and what they were doing, the questions, the answers
    given, the plan text, where it was saved, is here, and it is all plain
    JSON so it can go into the session record and come back as the same shape.
    """
    return {"phase": p.phase, "detail": p.detail, "prompt": p.prompt,
            "project": p.project or "", "explorers": list(p.explorers),
            "findings": list(p.findings), "questions": list(p.questions),
            "answers": dict(p.answers), "text": p.text, "cancelled": p.cancelled,
            "saved": p.saved, "plan_file": p.plan_file, "context_file": p.context_file}


def restore(data: dict, sid: str, ref: str | None = None) -> Plan:
    """Rebuild a plan from a snapshot, so answering is not tied to the process
    that asked.

    After a server restart the questions a person is looking at are still on
    disk; without this, submitting them would find nothing to answer. The model
    reference is not in the snapshot, it comes from the session, which is the
    thing that outlived the restart in the first place.
    """
    data = data or {}
    p = Plan(sid, data.get("prompt") or "", data.get("project") or None, ref)
    p.phase = data.get("phase") or "decomposing"
    p.detail = data.get("detail") or ""
    p.explorers = [dict(e) for e in (data.get("explorers") or []) if isinstance(e, dict)]
    p.findings = [str(f) for f in (data.get("findings") or [])]
    p.questions = [dict(q) for q in (data.get("questions") or []) if isinstance(q, dict)]
    p.answers = dict(data.get("answers") or {})
    p.text = data.get("text") or ""
    p.saved = bool(data.get("saved"))
    p.plan_file = data.get("plan_file") or ""
    p.context_file = data.get("context_file") or ""
    return p


def _ask(system: str, user: str, ref: str | None) -> str:
    try:
        res = engine.chat([{"role": "system", "content": system},
                           {"role": "user", "content": user}], ref=ref, temperature=0.2)
        return res.get("content") or ""
    except engine.EngineError as e:
        return f"__ERROR__ {e}"


def explore(plan: Plan, limit: int | None = None):
    yield plan.status("decomposing", "breaking the request into areas to inspect")
    limit = limit or int(config.PLAN_MAX_TASKS)
    raw = _ask(DECOMPOSE.replace("{limit}", str(limit)), plan.prompt, plan.ref)
    if raw.startswith("__ERROR__"):
        yield plan.status("failed", raw[10:], [])
        return
    tasks = _array(raw, limit) or [FALLBACK_TASK]

    plan.explorers = [{"label": t.get("label") or f"area {i + 1}", "state": "pending",
                       "activity": "waiting"} for i, t in enumerate(tasks)]
    yield plan.status("exploring", f"{len(tasks)} area(s) to inspect")

    for i, task in enumerate(tasks):
        if plan.cancelled:
            yield plan.status("cancelled", "plan cancelled")
            return
        plan.explorers[i] = {"label": plan.explorers[i]["label"], "state": "active",
                             "activity": "exploring…"}
        yield plan.status("exploring", f"inspecting {i + 1} of {len(tasks)}")

        messages = [{"role": "system",
                     "content": prompts.system_prompt(plan.project, False, subagent=True)},
            {"role": "user", "content": str(task.get("task") or plan.prompt)}]
        report = []
        last_activity = "exploring…"
        for ev in agent.run_turn(messages, project=plan.project, ref=plan.ref,
                                 max_steps=config.SUBAGENT_MAX_STEPS, readonly=True, nested=True):
            if ev.get("type") == "text":
                report.append(ev["delta"])
            elif ev.get("type") == "tool_start":
                hint = f"→ {ev.get('name') or 'working'}"
                if hint != last_activity:
                    last_activity = hint
                    plan.explorers[i] = {"label": plan.explorers[i]["label"], "state": "active",
                                         "activity": hint}
                    yield plan.status("exploring", f"inspecting {i + 1} of {len(tasks)}")

        text = "".join(report).strip() or "(nothing found)"
        plan.findings.append(f"[{plan.explorers[i]['label']}] {text}")
        plan.explorers[i] = {"label": plan.explorers[i]["label"], "state": "done",
                             "activity": "done"}
        yield plan.status("exploring", f"finished {i + 1} of {len(tasks)}")

    yield plan.status("questions", "deciding what to ask you")
    findings = "\n\n".join(plan.findings)[:12000] or "(no findings)"
    raw = _ask(QUESTIONS.replace("{limit}", str(config.PLAN_MAX_QUESTIONS))
                       .replace("{prompt}", plan.prompt).replace("{findings}", findings),
               plan.prompt, plan.ref)
    plan.questions = _array(raw, config.PLAN_MAX_QUESTIONS)
    if plan.cancelled:
        yield plan.status("cancelled", "plan cancelled")
        return
    if not plan.questions:
        yield plan.status("synthesizing", "nothing to ask, writing the plan")
        yield from synthesise(plan, {})
        return
    yield plan.status("awaiting_answers", f"{len(plan.questions)} question(s)")
    yield {"type": "plan_questions", "questions": plan.questions}


def synthesise(plan: Plan, answers: dict):
    plan.answers = answers or {}
    yield plan.status("synthesizing", "writing the plan")
    findings = "\n\n".join(plan.findings)[:12000] or "(no findings)"
    answer_text = "\n".join(f"- {k}: {v}" for k, v in plan.answers.items()) or "(skipped)"
    body = (SYNTHESISE.replace("{prompt}", plan.prompt)
                      .replace("{findings}", findings)
                      .replace("{answers}", answer_text))
    raw = _ask(body, plan.prompt, plan.ref)
    if raw.startswith("__ERROR__"):
        yield plan.status("failed", raw[10:])
        return
    plan.text = raw.strip()
    yield {"type": "plan_ready", "plan": plan.text}


# The file an approved plan becomes in the project. project_context.NAMES lists
# what it injects, and this is the one name plan mode writes first: an approved
# plan is an instruction the project has agreed to, which is exactly what that
# module is for. It has to be in NAMES, not DOCS, DOCS is only ever *listed*, and
# a plan nobody reads is a plan nobody implements.
CONTEXT_NAME = "PLAN.md"

# Written at the top of every file this produces. The string is owned by
# project_context because that module is the reader, it is the one that has to
# decide whether a PLAN-*.md in the root is an approved plan or somebody else's
# file with a similar name, and a contract between writer and reader does not get
# to have two copies of itself. It is also what tells a re-approve from a first
# write: a file carrying it was made here and may be replaced.
MARKER = project_context.PLAN_MARK_LINE


def _target(root: Path, plan: Plan) -> tuple[Path, bool]:
    """Where this plan goes in the project, and whether that is the fallback name.

    PLAN.md is the canonical place: the project either has no PLAN.md, or has one
    carrying our marker, which makes it an earlier plan from this same harness and
    fair to replace. Anything else belongs to somebody else. It is not overwritten,
    and it is not a reason to throw the approved plan away either, so the plan lands
    beside it under a name that says which session approved it.
    """
    path = root / CONTEXT_NAME
    if not path.is_file():
        return path, False
    try:
        existing = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001, unreadable is not "ours to overwrite"
        existing = ""
    if MARKER in existing:
        return path, False
    return root / f"PLAN-{plan.sid[:8]}.md", True


def write_context(plan: Plan) -> dict:
    """Put the approved plan where the agent's own context reads from.

    The point of plan mode is that the planning survives the conversation that
    produced it. Saving markdown under ``~/.tacit/plans/`` alone left the agent
    implementing in a fresh session with nothing in its prompt but a copy of the
    text, no record of it in the project, and nothing for the next session to
    find. Writing PLAN.md makes the plan part of the project the way AGENTS.md
    is: discovered, indexed, and injectable by the module that already does that.

    A PLAN.md that is not ours is never overwritten, and never merely refused. Plan
    mode runs against real working folders and a project's own PLAN.md is somebody's
    document, but an approved plan is work product too: refusing to record it because
    of a filename collision would make the feature depend on the state of somebody's
    repo. So it takes a session-scoped name beside the project's file, which
    project_context discovers by pattern and admits only because the marker is
    inside it. The project's document survives and the plan still reaches the prompt.
    """
    if not plan.project:
        return {"written": False,
                "message": "no project folder is bound to this session, so there is "
                           "nowhere to write PLAN.md"}
    try:
        root = Path(plan.project).expanduser()
    except Exception:  # noqa: BLE001 — a path the OS dislikes is not a crash
        return {"written": False, "message": f"unreadable project folder: {plan.project}"}
    if not root.is_dir():
        return {"written": False, "message": f"the project folder does not exist: {plan.project}"}
    path, fallback = _target(root, plan)
    try:
        # Still checked, because the fallback name can be taken too. A project with
        # a hand-written PLAN-abc12345.md that happens to match this session's id
        # prefix is a coincidence nobody should silently lose a file over.
        existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        if existing and MARKER not in existing:
            return {"written": False, "refused": True, "path": str(path),
                    "message": f"{path} already exists and was not written by plan mode - "
                               f"move or rename it to let the approved plan in"}
        body = (f"{MARKER}\n"
                f"# Approved plan\n\n"
                f"Written by Tacit plan mode on {date.today().isoformat()} for the request below. "
                f"This file is the project's instruction for the work: implement it as written, "
                f"and if reality has moved on since, say so rather than quietly re-deciding it.\n\n"
                f"## Request\n\n{plan.prompt.strip()}\n\n"
                f"## Plan\n\n{plan.text.strip()}\n")
        path.write_text(body, encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — reported to the person, not raised at the socket
        return {"written": False, "message": f"could not write {path}: {e}"}
    plan.context_file = str(path)
    if fallback:
        # The name is said out loud because the person is looking at PLAN.md in
        # their project and this plan is not in it.
        return {"written": True, "path": str(path), "fallback": True,
                "message": f"written to {path.name}, the project's own PLAN.md is not "
                           f"ours to overwrite, so this session's plan took its own name"}
    return {"written": True, "path": str(path),
            "message": f"written to {path}, the project's own context now carries it"}


def approve(plan: Plan, approved: bool) -> dict:
    if not approved:
        plan.saved = False
        return {"saved": False, "message": "plan rejected, refine the request and plan again"}
    if not plan.text.strip():
        return {"saved": False, "message": "there was no plan to save"}
    path = config.PLANS_DIR / f"{plan.sid}.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.text, encoding="utf-8")
    except Exception as e:
        return {"saved": False, "message": f"could not save the plan: {e}"}
    plan.saved = True
    plan.plan_file = str(path)
    # The copy under ~/.tacit/plans/ is the record of what was approved; the
    # project's PLAN.md is what makes it act on. Both are written, and the
    # message reports whichever of them actually happened, because a plan that
    # saved in only one place needs to say which.
    ctx = write_context(plan)
    if ctx.get("written"):
        message = (f"saved to {path} and written to {ctx['path']} - "
                   f"Implement runs it in a fresh session")
    elif ctx.get("refused"):
        message = f"saved to {path}. {ctx['message']}"
    else:
        message = f"saved to {path}. {ctx.get('message') or ''} - " \
                  f"Implement runs it in a fresh session"
    return {"saved": True, "plan_file": str(path), "context_file": plan.context_file,
            "context_written": bool(ctx.get("written")), "message": message}


def implement_prompt(plan: Plan) -> str:
    """The first message of the fresh session that does the work.

    The plan is inlined even though PLAN.md is now in the project's context:
    context injection is a setting with a budget and an index mode, and an
    approved plan being implemented is not a thing to leave to whichever mode
    the person happens to have chosen. The path is named as well, so the agent
    can re-read it and so the file is the thing that gets updated if the plan
    changes mid-work.
    """
    where = (f"\n\nIt is also saved as `{plan.context_file}` in the project, which is the "
             f"file to update if the work diverges from it.") if plan.context_file else ""
    return ("Implement the approved plan below. Make the actual file changes now, and verify "
            f"them.{where}\n\n## Approved plan\n\n" + plan.text)

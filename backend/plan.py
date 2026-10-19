import json
import re
from datetime import date
from pathlib import Path

from . import agent, config
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
            "saved": p.saved, "plan_file": p.plan_file}


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
                                 max_steps=config.subagent_steps(), readonly=True, nested=True):
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


# An approved plan becomes a markdown file in Tacit's own session storage. Not a
# file in the project.
#
# The failure this feature exists to solve is a memory failure: a plan that lives
# only in the conversation gets summarised, compressed, and eventually paraphrased,
# and an agent implementing from a paraphrase quietly re-decides things that were
# already decided. A file on disk does not drift, and the read tool reaches it from
# any turn, so the plan can be re-read as often as the work likes.
#
# Where that file goes is the other half of the design. Writing it into the project
# made plan mode litter other people's repositories with a file named PLAN.md that
# collided with documents they had written themselves, and made the harness's
# artefact an *instruction* the next session was bound by. The plan is the session's
# work product, so it lives with the session, under ~/.tacit/plans, and is deleted
# with it. Nothing about approving a plan touches the project folder any more.

# Sids are generated here and never free text, but this path is built from one and
# a stray separator would put the plan outside the plans folder, so it is filtered.
_SID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def plan_path(sid: str) -> Path:
    """Where this session's approved plan lives: ``~/.tacit/plans/<sid>.md``."""
    return config.PLANS_DIR / f"{_SID_SAFE.sub('', str(sid or ''))[:64]}.md"


def body(plan: "Plan") -> str:
    """The text of the file the implementing agent reads back.

    The request rides along with the plan because a plan on its own is an answer to
    a question nobody can re-ask: several sessions later, "why is it done this way"
    is only answerable from the thing that was asked. The framing is load-bearing
    rather than decorative: the file is an instruction somebody agreed to, so
    divergence gets reported instead of re-decided, and it is meant to be edited as
    the work teaches the agent something the plan did not know.
    """
    return ("# Approved plan\n\n"
            f"Written by Tacit plan mode on {date.today().isoformat()} for the request below, "
            "and saved with the session that approved it rather than in the project, so the "
            "work can re-read it as many times as it likes.\n\n"
            "This file is the instruction for the work: implement it as written, and if "
            "reality has moved on since, say so rather than quietly re-deciding it. Update "
            "this file when the plan changes, so whatever reads it next gets what is now "
            "true rather than what was guessed.\n\n"
            f"## Request\n\n{plan.prompt.strip()}\n\n"
            f"## Plan\n\n{plan.text.strip()}\n")


def save(plan: "Plan") -> dict:
    """Write the approved plan to its session file. Never to the project."""
    path = plan_path(plan.sid)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body(plan), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — reported to the person, not raised at the socket
        return {"written": False, "message": f"could not save the plan: {e}"}
    plan.plan_file = str(path)
    return {"written": True, "path": str(path)}


def approve(plan: Plan, approved: bool) -> dict:
    if not approved:
        plan.saved = False
        return {"saved": False, "message": "plan rejected, refine the request and plan again"}
    if not plan.text.strip():
        return {"saved": False, "message": "there was no plan to save"}
    res = save(plan)
    if not res.get("written"):
        return {"saved": False, "message": res["message"]}
    return {"saved": True, "plan_file": res["path"],
            "message": f"saved to {res['path']} - Implement runs it in a fresh session, "
                       f"and every turn of that session is pointed back at this file"}


def implement_prompt(plan: Plan) -> str:
    """The first message of the fresh session that does the work.

    The plan is inlined even though it is on disk. An agent should not have to spend
    a tool call to get to the work it has just been handed, and context injection is
    a setting with a budget and an index mode, so leaving an approved plan to
    whichever mode the person happens to have chosen would make the plan optional.

    The path is named in the same breath because the inlined copy is the one that
    eventually gets summarised away. The file is the durable thing, and `pointer()`
    is what keeps naming it after this first message is a hundred turns old and half
    of it is a summary of a summary.
    """
    where = (f"\n\nIt is saved as `{plan.plan_file}`. Read it with the read tool whenever "
             f"you need the whole plan, and update that file if the work diverges from "
             f"it.") if plan.plan_file else ""
    return ("Implement the approved plan below. Make the actual file changes now, and verify "
            f"them.{where}\n\n## Approved plan\n\n" + plan.text)


def pointer(rec: dict) -> str:
    """The standing line that keeps an approved plan reachable.

    This is the part that makes the file worth having. A plan named once in a first
    message is a plan that survives until the transcript is compacted, and then it
    survives only as a paraphrase. Appended to the system prefix instead — the same
    place the project's own instructions go, and for the same reason — it costs
    about a hundred tokens every turn and never gets summarised, because the prefix
    is rebuilt each turn rather than carried along.

    Two sources, in order of authority. ``planFile`` is the field Implement writes
    onto the session it creates, which is the session doing the work and the one
    that will be compacted. ``planState.plan_file`` is the plan this session
    approved itself, so a session that planned and then kept going can re-read what
    it agreed to. Empty for any session that has neither, so callers append it
    unconditionally.
    """
    rec = rec or {}
    path = str(rec.get("planFile") or "")
    if not path:
        state = rec.get("planState") or {}
        if isinstance(state, dict) and state.get("saved"):
            path = str(state.get("plan_file") or "")
    if not path:
        return ""
    return ("An approved plan for this work is saved here:\n\n"
            f"    {path}\n\n"
            "It is not in your context. Read it with the read tool before changing "
            "anything, re-read it whenever the plan is no longer in front of you, and "
            "update that file if the work diverges from it.")

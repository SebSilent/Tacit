import json
import re

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
each {{"q": "the question", "options": ["option", "option"]}} — 2-4 concrete options each.
No prose, no code fences."""

SYNTHESISE = """You are writing an implementation plan for a coding task.

Request: {prompt}

What you found while exploring:
{findings}

The user's answers:
{answers}

Write the plan as markdown. Use these sections: Goal, Approach, Files to change, Steps, Risks.
Be specific and concrete — name real files and functions. Do not write the code, describe the
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
        self.explorers: list[dict] = []
        self.findings: list[str] = []
        self.questions: list[dict] = []
        self.answers: dict = {}
        self.text = ""
        self.cancelled = False

    def status(self, phase: str, detail: str = "", explorers=None) -> dict:
        self.phase = phase
        return {"type": "plan_status", "phase": phase, "detail": detail,
                "explorers": explorers if explorers is not None else self.explorers}


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
        yield {"type": "plan_status", "phase": "failed", "detail": raw[10:], "explorers": []}
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
        yield plan.status("synthesizing", "nothing to ask — writing the plan")
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
        yield {"type": "plan_status", "phase": "failed", "detail": raw[10:], "explorers": plan.explorers}
        return
    plan.text = raw.strip()
    yield {"type": "plan_ready", "plan": plan.text}


def approve(plan: Plan, approved: bool) -> dict:
    if not approved:
        return {"saved": False, "message": "plan rejected — refine the request and plan again"}
    if not plan.text.strip():
        return {"saved": False, "message": "there was no plan to save"}
    path = config.PLANS_DIR / f"{plan.sid}.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.text, encoding="utf-8")
    except Exception as e:
        return {"saved": False, "message": f"could not save the plan: {e}"}
    return {"saved": True, "plan_file": str(path),
            "message": f"saved to {path} — Implement runs it in a fresh session"}


def implement_prompt(plan: Plan) -> str:
    return ("Implement the approved plan below. Make the actual file changes now, and verify "
            "them.\n\n## Approved plan\n\n" + plan.text)

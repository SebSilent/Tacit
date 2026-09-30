from . import agent, config
from .ai import engine, prompts
from .plan import _array

DECOMPOSE = """Split this research question into at most {n} aspects that can be investigated
independently. Reply with a JSON array only, each {{"label": "short name", "question": "what to find out"}}.
No prose, no code fences.

Question: {question}"""

SYNTHESISE = """Write a research report answering the question, using only the findings below.

Format: a short "## Answer", then "## Findings" with a source for every claim, then
"## Uncertainties". If findings conflict, say so plainly. Do not invent anything that is not in
the findings, and mark anything you could not confirm.

Question: {question}

Findings:
{findings}"""

FALLBACK = {"label": "general", "question": ""}


def _ask(system: str, user: str, ref: str | None, max_tokens: int = 4000) -> str:
    try:
        res = engine.chat([{"role": "system", "content": system},
                           {"role": "user", "content": user}], ref=ref,
                          temperature=0.2, max_tokens=max_tokens)
        return res.get("content") or ""
    except engine.EngineError as e:
        return f"__ERROR__ {e}"


def research(question: str, project: str | None = None, ref: str | None = None) -> str:
    q = str(question or "").strip()
    if not q:
        return "ERROR: question required"

    n = config.RESEARCH_ASPECTS
    raw = _ask(DECOMPOSE.replace("{n}", str(n)), q, ref, 1200)
    if raw.startswith("__ERROR__"):
        return f"ERROR: {raw[10:]}"
    aspects = _array(raw, n) or [FALLBACK]

    findings = []
    for i, aspect in enumerate(aspects):
        sub_q = str(aspect.get("question") or aspect.get("label") or q)
        label = str(aspect.get("label") or f"aspect {i + 1}")
        messages = [
            {"role": "system", "content":
                prompts.system_prompt(project, False, subagent=True) +
                "\n\nResearch your aspect using fetch and the browser tools. Cite each source URL. "
                "Report what you found, and say plainly what you could not confirm."},
            {"role": "user", "content": sub_q},
        ]
        report = []
        for ev in agent.run_turn(messages, project=project, ref=ref,
                                 max_steps=config.RESEARCH_MAX_STEPS, readonly=True, nested=True):
            if ev.get("type") == "text":
                report.append(ev["delta"])
        text = "".join(report).strip() or "(nothing found)"
        findings.append(f"[{label}] {sub_q}\n{text}")

    if not findings:
        return "ERROR: no findings"

    body = SYNTHESISE.replace("{question}", q).replace(
        "{findings}", "\n\n".join(findings)[:24000])
    out = _ask(body, q, ref, 6000)
    if out.startswith("__ERROR__"):
        return f"ERROR: {out[10:]}"
    return out.strip() or "(the synthesis returned nothing)"

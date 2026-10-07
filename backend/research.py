import time

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


def _delegate_ref(explicit: str | None) -> str | None:
    """The model delegated work runs on: the call's own ref, else the setting.

    `explicit` is what the caller passed — for a tool call that is the session's
    model, which used to be the end of it. The setting exists because a session
    model at thinking=max is a poor default for fetch-and-grep work, and the
    choice has to be reachable from the interface.
    """
    return str(explicit or "").strip() or config.delegate_model() or None


def research(question: str, project: str | None = None, ref: str | None = None):
    """Investigate a question across aspects and synthesise a cited report.

    This is a generator now, not a plain function. The old version discarded every
    event the sub-agents emitted, so a research call could burn minutes of tokens
    while the interface showed a frozen cursor and nothing else. Yielding the
    events lets the caller forward them to the interface as they happen.

    Two caps bound the call, because the caller is the one waiting: a wall clock
    (`TACIT_RESEARCH_TIMEOUT`, default 600s) checked between events, and the
    sub-agents' own step budget. Aspects run concurrently — they are independent
    and read-only, which is what `_run_parallel` already does for `task` calls;
    running three of them one after another was three times the wall clock for
    the same tokens.
    """
    q = str(question or "").strip()
    if not q:
        yield "ERROR: question required"
        return
    started = time.time()
    ref = _delegate_ref(ref)

    n = config.RESEARCH_ASPECTS
    raw = _ask(DECOMPOSE.replace("{n}", str(n)), q, ref, 1200)
    if raw.startswith("__ERROR__"):
        yield f"ERROR: {raw[10:]}"
        return
    aspects = _array(raw, n) or [FALLBACK]

    # One shared wall clock across every aspect. A sub-agent that would run past
    # it is abandoned with what it has; the synthesis still names what was found.
    deadline = started + config.RESEARCH_TIMEOUT_S
    specs = []
    for i, aspect in enumerate(aspects):
        sub_q = str(aspect.get("question") or aspect.get("label") or q)
        label = str(aspect.get("label") or f"aspect {i + 1}")
        specs.append((f"research-{i}", label, sub_q))

    findings: dict[str, str] = {}
    # Worker threads cannot yield into the caller's stream, so their activity
    # queues here and the generator drains it after the join.
    events: list = []

    def _run_aspect(label: str, sub_q: str, out: dict):
        """One aspect in a worker thread. Events queue; the generator drains them.

        A thread cannot yield into the caller's stream, so the aspect's activity
        lands in ``events`` and is yielded between the join and the synthesis.
        Live-enough: the panel updates per aspect, not per tool call, and the
        alternative — a queue polled by the generator — would tie the stream to
        the caller's consumption rate anyway.
        """
        messages = [
            {"role": "system", "content":
                prompts.system_prompt(project, False, subagent=True) +
                "\n\nResearch your aspect using fetch and the browser tools. Cite each source URL. "
                "Report what you found, and say plainly what you could not confirm."},
            {"role": "user", "content": sub_q},
        ]
        report: list[str] = []
        for ev in agent.run_turn(messages, project=project, ref=ref,
                                 max_steps=config.RESEARCH_MAX_STEPS, readonly=True,
                                 nested=True, deadline=deadline):
            kind = ev.get("type")
            if kind == "text":
                report.append(ev["delta"])
            elif kind in ("tool_start", "tool_end", "notify"):
                events.append({**ev, "research": True, "aspect": label})
        text = "".join(report).strip() or "(nothing found)"
        out["result"] = f"[{label}] {sub_q}\n{text}"

    # Aspects are independent and read-only, so they run concurrently on threads.
    # `task` would re-derive its own prompt; the aspect question IS the prompt, so
    # the sub-agents are driven directly rather than through call_tool.
    import threading
    parallel: dict = {}
    def worker(cid: str, label: str, sub_q: str):
        out: dict = {}
        parallel[cid] = out
        try:
            _run_aspect(label, sub_q, out)
        except Exception as exc:  # noqa: BLE001
            out["result"] = f"[{label}] {sub_q}\n(the aspect failed: {exc})"
        finally:
            events.append({"type": "notify", "level": "info", "research": True,
                           "message": f"aspect finished: {label}"})

    threads = [threading.Thread(target=worker, args=spec, daemon=True)
               for spec in ((cid, label, sub_q) for cid, label, sub_q in specs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=max(1, int(deadline - time.time())) if deadline > time.time() else 1)
    for ev in events:
        yield ev
    for cid, label, sub_q in specs:
        out = parallel.get(cid) or {}
        findings[label] = str(out.get("result") or f"[{label}] {sub_q}\n(nothing found)")

    if not findings:
        yield "ERROR: no findings"
        return

    if time.time() > deadline:
        # The clock ran out mid-investigation. A partial report beats an error:
        # the findings exist, and the caller is waiting.
        yield {"type": "notify", "level": "warn", "research": True,
               "message": "research hit its wall-clock cap; synthesising what was found"}
    body = SYNTHESISE.replace("{question}", q).replace(
        "{findings}", "\n\n".join(findings.values())[:24000])
    out = _ask(body, q, ref, 6000)
    if out.startswith("__ERROR__"):
        yield f"ERROR: {out[10:]}"
        return
    yield out.strip() or "(the synthesis returned nothing)"
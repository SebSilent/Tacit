"""The bounded multi-source check behind the `research` tool.

Three things used to make this tool feel broken, none of them the report:

1. Aspect activity was appended to a plain list and drained *after* every
   ``t.join()``, so minutes of sub-agent work arrived in one burst at the end of
   the aspect phase, which reads exactly like a hang.
2. Two blocking model calls bracketed the run (decompose, synthesise) and emitted
   nothing at all while they sat there.
3. Nothing said what the call was doing, how long it had been at it, or what it
   was allowed to cost.

So the threads now queue into a ``queue.Queue`` the generator polls while they
run (the shape ``agent._run_parallel`` already uses), every phase change is a
``notify``, and a second of silence produces a heartbeat on the research card.
The report shape, Answer / Findings with a source per claim / Uncertainties,
is untouched, because that part worked.
"""

import queue
import threading
import time

from . import agent, config
from .ai import engine, prompts
from .plan import _array

# A research call spends most of its wall clock inside two blocking model calls
# and inside aspects waiting on one slow tool. Those are the silent stretches, so
# this is how long the call is allowed to say nothing before it says "still here".
HEARTBEAT_S = 1.0

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

    `explicit` is what the caller passed, for a tool call that is the session's
    model, which used to be the end of it. The setting exists because a session
    model at thinking=max is a poor default for fetch-and-grep work, and the
    choice has to be reachable from the interface.
    """
    return str(explicit or "").strip() or config.delegate_model() or None


def _note(level: str, message: str, aspect: str = "", budget: dict | None = None) -> dict:
    """A phase change, as an event. `aspect` routes it to that aspect's card.

    The delegation panel keys research cards on ``aspect`` and falls back to one
    "general" card, which is where the between-aspect news belongs: splitting,
    synthesising, hitting the cap.

    ``budget`` travels as data, not only as prose in `message`: the panel shows it
    as a standing line under the card, and a number copied into JS is a number
    that goes stale the day config changes.
    """
    ev = {"type": "notify", "level": level, "message": message, "research": True}
    if aspect:
        ev["aspect"] = aspect
    if budget:
        ev["budget"] = budget
    return ev


def _tick(call_id: str, started: float) -> dict:
    """Item 2's heartbeat, on the research tool's own card.

    ``chars`` is 0 on purpose: nothing is being produced that a viewer could
    read, and claiming a byte count for a model call that has not returned would
    be a lie. The counting timer is the whole news, and it is the difference
    between "thinking" and "dead". No ``id`` means no card, so no event.

    ``research`` is what routes it: without the tag the delegation panel falls
    back to keying the card on its tool-call id, and a heartbeat would open a
    fourth card labelled "sub-agent" that no sub-agent is behind. With it, the
    tick lands on the general research card the budget note already sits on.
    """
    return {"type": "tool_progress", "id": call_id, "chars": 0, "research": True,
            "elapsed_s": round(time.time() - started, 1), "done": False}


def _ask_live(system: str, user: str, ref: str | None, max_tokens: int,
              out: dict, *, call_id: str, started: float, stop=None):
    """Run a blocking `_ask` on a thread, yielding a heartbeat each second it waits.

    The result lands in ``out["value"]`` because a generator cannot hand one
    back; ``out["stopped"]`` means the person cancelled while it was in flight,
    and the caller must not read a value that never came. The model call itself
    is not interruptible, it keeps running on a daemon thread and its answer is
    dropped. That is the same trade every other cancellation here makes.
    """
    box: queue.Queue = queue.Queue()

    def run():
        try:
            out["value"] = _ask(system, user, ref, max_tokens)
        except Exception as exc:  # noqa: BLE001
            out["value"] = f"__ERROR__ {exc}"
        finally:
            box.put(None)

    threading.Thread(target=run, daemon=True).start()
    while "value" not in out:
        try:
            box.get(timeout=HEARTBEAT_S)
        except queue.Empty:
            if stop is not None and stop.is_set():
                out["stopped"] = True
                return
            if call_id:
                yield _tick(call_id, started)


def research(question: str, project: str | None = None, ref: str | None = None,
             call_id: str = "", stop=None):
    """Investigate a question across aspects and synthesise a cited report.

    A generator, and now a *live* one: aspect events surface as they happen
    rather than in one burst when the aspect phase joins. `call_id` is the id of
    the research tool call in the main transcript, the card the heartbeats tick
    on, and `stop` is the turn's cancellation event, either of which may be
    empty/None when this is driven from a test.

    Two caps bound the call, because the caller is the one waiting: a wall clock
    (`TACIT_RESEARCH_TIMEOUT`) and the sub-agents' own step budget. Aspects run
    concurrently, independent and read-only, which is what `_run_parallel`
    already does for `task` calls; running three of them one after another was
    three times the wall clock for the same tokens. Hitting the clock is not an
    error: the findings that exist get synthesised into a partial report.
    """
    q = str(question or "").strip()
    if not q:
        yield "ERROR: question required"
        return
    started = time.time()
    ref = _delegate_ref(ref)

    n = config.RESEARCH_ASPECTS
    steps = config.research_steps()
    cap = config.RESEARCH_TIMEOUT_S

    # The budget is announced, not hidden in config: a person watching a card
    # cannot go looking these numbers up, and "up to N aspects, T seconds" is
    # what makes a slow call look intentional instead of stuck.
    yield _note("info", f"splitting the question into up to {n} aspects "
                        f"({steps} steps each, {cap}s cap)",
                budget={"aspects": n, "steps": steps, "cap_s": cap})
    box: dict = {}
    yield from _ask_live(DECOMPOSE.replace("{n}", str(n)), q, ref, 1200, box,
                         call_id=call_id, started=started, stop=stop)
    if box.get("stopped"):
        yield "ERROR: research stopped"
        return
    if stop is not None and stop.is_set():
        # Checked before any aspect thread exists: a Stop press should cost
        # nothing more than the model call already in flight, and starting three
        # sub-agents to find out they are unwanted is not "nothing".
        yield "ERROR: research stopped"
        return
    raw = str(box.get("value") or "")
    if raw.startswith("__ERROR__"):
        yield f"ERROR: {raw[10:]}"
        return
    aspects = _array(raw, n) or [FALLBACK]

    # One shared wall clock across every aspect. A sub-agent that would run past
    # it is abandoned with what it has; the synthesis still names what was found.
    deadline = started + cap
    specs = []
    for i, aspect in enumerate(aspects):
        sub_q = str(aspect.get("question") or aspect.get("label") or q)
        label = str(aspect.get("label") or f"aspect {i + 1}")
        specs.append((f"research-{i}", label, sub_q))

    # Worker threads cannot yield into the caller's stream, so their activity
    # queues here and the generator polls it *while they run*, the old version
    # queued into a list and drained it after the join, which is the burst this
    # file exists to remove.
    live: queue.Queue = queue.Queue()
    outs: dict = {}

    def _run_aspect(label: str, sub_q: str, out: dict):
        """One aspect in a worker thread; everything it emits goes on the queue.

        Inner `tool_progress` is deliberately not forwarded: it carries the
        *inner* tool's id, which has no card in the main transcript (delegated
        calls never enter it) and no renderer in the panel. The drain loop below
        ticks on the research card instead, so a silent aspect still reads as
        live without tripling the event volume.
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
                                 max_steps=steps, readonly=True,
                                 nested=True, deadline=deadline):
            kind = ev.get("type")
            if kind == "text":
                report.append(ev["delta"])
                live.put(("ev", {**ev, "research": True, "aspect": label}))
            elif kind in ("reason", "tool_start", "tool_end", "notify"):
                live.put(("ev", {**ev, "research": True, "aspect": label}))
        text = "".join(report).strip() or "(nothing found)"
        out["result"] = f"[{label}] {sub_q}\n{text}"

    def worker(cid: str, label: str, sub_q: str):
        out: dict = {}
        outs[cid] = out
        try:
            _run_aspect(label, sub_q, out)
        except Exception as exc:  # noqa: BLE001
            out["result"] = f"[{label}] {sub_q}\n(the aspect failed: {exc})"
        finally:
            live.put(("end", label))

    # Aspects are independent and read-only, so they run concurrently on threads.
    # `task` would re-derive its own prompt; the aspect question IS the prompt, so
    # the sub-agents are driven directly rather than through call_tool.
    for _cid, label, _sub_q in specs:
        # Announced before the threads start, so the panel shows a card per
        # aspect at once instead of only once one of them has said something.
        yield _note("info", f"aspect started: {label}", aspect=label)
    threads = [threading.Thread(target=worker, args=spec, daemon=True) for spec in specs]
    for t in threads:
        t.start()

    remaining = len(threads)
    timed_out = False
    while remaining:
        if stop is not None and stop.is_set():
            # The aspects are daemon threads with the same deadline; abandoning
            # the drain leaves them to wind down on their own.
            yield "ERROR: research stopped"
            return
        left = deadline - time.time()
        if left <= 0:
            timed_out = True
            break
        try:
            # Poll for at most a heartbeat, so the cap and the stop flag are
            # honoured even when nothing is arriving.
            kind, payload = live.get(timeout=min(HEARTBEAT_S, left))
        except queue.Empty:
            if call_id:
                yield _tick(call_id, started)
            continue
        if kind == "end":
            remaining -= 1
            yield _note("info", f"aspect finished: {payload}", aspect=payload)
        else:
            yield payload

    if stop is not None and stop.is_set():
        # Every aspect can finish at the same moment the person presses Stop. The
        # synthesis is another blocking model call they did not ask for, so the
        # run ends here rather than reporting a report nobody wants.
        yield "ERROR: research stopped"
        return

    findings: dict[str, str] = {}
    for cid, label, sub_q in specs:
        out = outs.get(cid) or {}
        findings[label] = str(out.get("result") or f"[{label}] {sub_q}\n(nothing found)")

    if not findings:
        yield "ERROR: no findings"
        return

    if timed_out:
        # The clock ran out mid-investigation. A partial report beats an error:
        # the findings exist, and the caller is waiting.
        reported = sum(1 for cid, _l, _q in specs if (outs.get(cid) or {}).get("result"))
        yield _note("warn", f"research hit its {cap}s cap after "
                            f"{round(time.time() - started)}s; synthesising what was found "
                            f"({reported} of {len(specs)} aspects reported)")
    elif remaining:
        yield _note("warn", f"{remaining} aspect(s) had not finished; synthesising what was found")

    yield _note("info", "synthesising the report…")
    body = SYNTHESISE.replace("{question}", q).replace(
        "{findings}", "\n\n".join(findings.values())[:24000])
    sbox: dict = {}
    yield from _ask_live(body, q, ref, 6000, sbox,
                         call_id=call_id, started=started, stop=stop)
    if sbox.get("stopped"):
        yield "ERROR: research stopped"
        return
    out_text = str(sbox.get("value") or "")
    if out_text.startswith("__ERROR__"):
        yield f"ERROR: {out_text[10:]}"
        return
    yield out_text.strip() or "(the synthesis returned nothing)"
    yield _note("info", "research complete")

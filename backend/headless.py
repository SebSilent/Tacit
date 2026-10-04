"""Run one Tacit agent turn from a command line, with no web server.

Tacit's only front door is the WebSocket session in ``routers/chat.py``. Benchmarks and
scripts need a process that takes an instruction, does the work, and exits with a number
they can read — which is the same agent loop with no browser, no uvicorn and no analyzer.

This module reproduces the server's turn path rather than inventing a second one: the
same system prompt, the same project-instructions block, the same ``agent.run_turn`` call
with the same arguments, and the same usage accumulation. Two things are deliberately
absent, because both would change what is being measured:

* the background session analyzer, which makes its own model calls after a turn and would
  inflate every token figure a benchmark reports;
* the FastAPI app, which needs the web dependencies at all.

Usage is accumulated here rather than read from ``~/.tacit/audit.jsonl``. The audit ledger
records tool calls, capability changes and analyzer runs; there is no per-step model-usage
event in it, so summing its ``tokens`` field reports the analyzer's spend and the user's
other sessions, not this turn's. The numbers come from ``{"type": "usage"}`` events that
``run_turn`` yields once per model call.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import agent, audit, config, metrics, project_context
from .ai import prompts


def _fingerprint() -> str:
    """Content hash of the code that produced this result.

    A benchmark row is meaningless if you cannot say which build ran it, and source here
    is not necessarily committed.
    """
    import hashlib
    h = hashlib.sha256()
    for rel in ("backend/agent.py", "backend/ai/engine.py", "backend/ai/prompts.py",
                "backend/headless.py"):
        p = Path(config.ROOT) / rel
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:12]


def resolve_ref(wanted: str) -> str:
    """Map whatever the caller called the model onto a model Tacit can actually reach.

    Benchmarks pin a canonical name ("deepseek-v4-flash") while Tacit addresses models as
    ``provider/id``, and the provider's own id may carry a version or a date suffix.

    Validation here is deliberately stricter than ``config.resolve_model``, which answers
    an unknown ref by falling back to ``model_list()[0]`` — a silent substitution. That
    behaviour is tolerable in the UI, which only ever offers real refs, and fatal in a
    benchmark: a pin that does not match would run some other model and report the pinned
    name, which is how a same-model comparison becomes fiction. So the ref must match an
    existing id, and an ambiguous match is an error rather than a coin flip.
    """
    available = [m["id"] for m in config.model_list()]
    if not available:
        raise SystemExit("no models configured; add one to ~/.tacit/models.json")
    wanted = (wanted or "").strip()
    if not wanted:
        default = (config.registry().get("default") or "").strip()
        if default in available:
            return default
        raise SystemExit(
            f"no default model, or it is not in the registry ({default!r}); pass --model. "
            f"Available: {', '.join(sorted(available))}")
    low = wanted.lower()
    # 1. exact provider/id
    exact = [a for a in available if a.lower() == low]
    if len(exact) == 1:
        return exact[0]
    # 2. bare id, unique across providers
    tail = low.split("/")[-1]
    by_tail = [a for a in available if a.lower().split("/")[-1] == tail]
    if len(by_tail) == 1:
        return by_tail[0]
    if len(by_tail) > 1:
        raise SystemExit(
            f"{wanted!r} matches more than one provider: {', '.join(sorted(by_tail))}. "
            "Pass a full provider/id so the pin is unambiguous.")
    # 3. one-sided prefix, so "deepseek-v4-flash" can reach "…/deepseek-v4-flash-0731" —
    #    but only if exactly one candidate exists, and the substitution is reported.
    prefix = [a for a in available
              if a.lower().split("/")[-1].startswith(tail) or tail.startswith(
                  a.lower().split("/")[-1])]
    if len(prefix) == 1:
        print(f"[tacit] model pin {wanted!r} is not an exact match; using "
              f"{prefix[0]!r}", file=sys.stderr)
        return prefix[0]
    raise SystemExit(
        f"no Tacit model matches {wanted!r}"
        + (f"; ambiguous between {', '.join(sorted(prefix))}" if prefix else "")
        + f". Available: {', '.join(sorted(available))}"
        + ". Set one in ~/.tacit/models.json or pass --model with a provider/id.")


def _accumulate(totals: dict, u: dict) -> None:
    """Same fields and same arithmetic as routers/chat.py::_usage_event."""
    inp = int(u.get("input") or 0)
    out = int(u.get("output") or 0)
    totals["input"] += inp
    totals["output"] += out
    totals["total"] += int(u.get("total") or 0) or (inp + out)
    totals["cache_read"] += int(u.get("cache_read") or 0)
    totals["cache_write"] += int(u.get("cache_write") or 0)
    # The number a harness comparison turns on: what this run cost fresh, with cache
    # reads excluded because they are re-reads of bytes already paid for. This matches
    # OpenBench's stated token basis ("uncached input + output").
    totals["fresh"] += max(0, inp - int(u.get("cache_read") or 0)) + out


def run(instruction: str, workdir: str, ref: str, *, max_steps: int | None = None,
        timeout_s: float | None = None, session_title: str = "headless",
        on_event=None, profile: str | None = None) -> dict:
    """One headless agent turn. Returns the result dict; raises nothing on task failure."""
    started = time.time()
    # A profile is a bundle of choices (plugins, memory, and crucially the tool set), and
    # agent.run_turn filters tool schemas off config.prefs()["disabledTools"], so it has to
    # be applied before the turn starts, not after. It writes only into whatever TACIT_HOME
    # this process resolved, which for a benchmark run is a throwaway copy.
    prof: dict = {}
    if profile:
        from . import profiles
        prof = profiles.apply(profile)
        if not prof.get("ok"):
            raise SystemExit(f"[tacit] profile {profile!r} could not be applied: "
                             f"{prof.get('error')}")
    rec = store_create(session_title, ref, workdir)
    messages = [{"role": "system",
                 "content": prompts.system_prompt(workdir, False, chat=False)}]
    instructions = project_context.block(workdir)
    if instructions.get("text"):
        messages.append({"role": "system", "content": instructions["text"]})
        metrics.bump(rec, instruction_tokens=instructions.get("tokens") or 0)
        audit.record("project_instructions", session=rec["id"], backend="context",
                     mode=str(instructions.get("mode") or ""),
                     tokens=int(instructions.get("tokens") or 0),
                     files=[f["name"] for f in instructions.get("files") or []],
                     held_back=int(instructions.get("held_back") or 0))
    # The server builds this from its own session record; here the transcript is exactly
    # this one instruction, so it is appended directly instead of round-tripping through
    # a private router helper that would pull FastAPI into a headless process.
    messages.append({"role": "user", "content": instruction})
    store_append(rec, "user", instruction)

    totals = {"input": 0, "output": 0, "total": 0, "cache_read": 0, "cache_write": 0,
              "fresh": 0}
    trace: list[dict] = []
    steps_seen = 0
    tool_calls = 0
    tool_errors = 0
    compactions = 0
    error: str | None = None
    timed_out = False

    deadline = started + timeout_s if timeout_s else None
    try:
        for ev in agent.run_turn(messages, project=workdir, ref=ref, chat=False,
                                 session=rec["id"],
                                 has_instructions=bool(instructions.get("text")),
                                 reasoning=config.reasoning_for(rec.get("thinking")),
                                 max_steps=max_steps, trace=trace):
            kind = ev.get("type")
            if kind == "usage":
                # Every usage event is counted, including the ones tagged `subagent`:
                # the server adds them to the same totals too, and a sub-agent's API
                # call is real cost that a harness comparison has to charge somewhere.
                _accumulate(totals, ev.get("usage") or {})
                steps_seen += 1
            elif kind == "tool_start":
                # The same counter the session record keeps.
                tool_calls += 1
            elif kind == "tool_end":
                if ev.get("is_error"):
                    tool_errors += 1
            elif kind == "compaction":
                compactions += 1
            elif kind == "error":
                error = ev.get("message") or "agent error"
            if on_event:
                on_event(ev)
            if deadline and time.time() > deadline:
                timed_out = True
                break
    except Exception as exc:  # noqa: BLE001  — a harness crash must still produce a row
        error = f"{type(exc).__name__}: {exc}"
    finally:
        store_save(rec)
        # Nothing to flush: audit.record() opens, appends and closes the ledger per
        # entry, so every line is on disk by the time the call returns. There is no
        # audit.flush() to call, and pretending there was would hide the fact that
        # the ledger is not where this turn's token totals live.

    wall = time.time() - started
    if timed_out:
        error = error or f"headless timeout after {timeout_s:.0f}s"
    turns = len(trace) or steps_seen
    # `text` events are stream deltas; the closing narration is what the trace keeps.
    final_text = (trace[-1].get("text") or "") if trace else ""
    return {
        "ok": error is None and not timed_out,
        "error": error,
        "session": rec["id"],
        "model": ref,
        "workdir": workdir,
        "max_steps": max_steps or config.AGENT_MAX_STEPS,
        "turns": turns,
        "steps": steps_seen,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "compactions": compactions,
        "input_tokens": totals["input"],
        "output_tokens": totals["output"],
        "total_tokens": totals["total"],
        "cache_read_tokens": totals["cache_read"],
        "cache_write_tokens": totals["cache_write"],
        # What to hand a benchmark that asks for "tokens", per its own basis rule.
        "tokens": totals["fresh"],
        "cache_hit_rate": (round(totals["cache_read"] / totals["input"], 4)
                           if totals["input"] else None),
        "wall_time_s": round(wall, 3),
        "final_text": final_text,
        "timed_out": timed_out,
        "tacit_version": f"dev+{_fingerprint()}",
        "system_prompt_chars": len(messages[0]["content"]),
        # Which capability bundle produced these numbers, and how many tools the model was
        # actually offered. Without this a profile comparison is unattributable.
        "profile": profile or "",
        "profile_skipped": [s.get("what") for s in (prof.get("skipped") or [])],
        "tools_offered": (prof.get("applied_now") or {}).get("tool_count"),
        "instruction_tokens": int(instructions.get("tokens") or 0),
        "trace": trace,
    }


def store_create(title: str, ref: str, workdir: str) -> dict:
    from . import store
    return store.create(title=title, model=ref, mode="agent", project=workdir)


def store_append(rec: dict, role: str, content: str) -> None:
    from . import store
    store.append(rec, role, content)


def store_save(rec: dict) -> None:
    from . import store
    store.save(rec)


def _utf8_streams():
    """Force UTF-8 on stdout/stderr, replacing what cannot be encoded.

    Windows consoles default to a legacy code page (cp1252 here), and a model's reply
    routinely contains arrows, box drawing or CJK. Printing that raised
    ``UnicodeEncodeError`` *after* the JSON row was already written correctly: the cell
    succeeded and still exited 1, which an adapter faithfully reports as an incomplete
    harness. That is a silent, platform-shaped scoring bug, so the stream is fixed rather
    than the print being skipped — the transcript belongs on the console too.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    ap = argparse.ArgumentParser(
        prog="backend.headless",
        description="Run one Tacit agent turn with no web server, and print the result.")
    ap.add_argument("--instruction", required=True, help="the task prompt")
    ap.add_argument("--workdir", default=os.getcwd(),
                    help="directory the agent operates in; checkers read these files")
    ap.add_argument("--model", default="",
                    help="model ref, e.g. ollama_cloud/deepseek-v4.1-flash")
    ap.add_argument("--max-steps", type=int, default=None, dest="max_steps")
    ap.add_argument("--timeout", type=float, default=None, help="wall-clock seconds")
    ap.add_argument("--json-out", default=None, dest="json_out",
                    help="write the result dict here as JSON")
    ap.add_argument("--quiet", action="store_true", help="do not stream events to stderr")
    ap.add_argument("--profile", default=None,
                    help="capability bundle to apply first: minimal | silent | safe | "
                         "power-isolation | power-memory | full (default: whatever "
                         "profiles.json already has active)")
    args = ap.parse_args(argv)

    config.ensure_home()
    config.load_env()

    workdir = str(Path(args.workdir).expanduser().resolve())
    if not Path(workdir).is_dir():
        print(f"workdir does not exist: {workdir}", file=sys.stderr)
        return 2
    ref = resolve_ref(args.model)
    if not ref:
        print("no model configured; add one to ~/.tacit/models.json", file=sys.stderr)
        return 2

    def show(ev):
        kind = ev.get("type")
        if kind == "tool_start":
            print(f"[tool] {ev.get('name')}", file=sys.stderr)
        elif kind == "notify":
            print(f"[{ev.get('level') or 'info'}] {ev.get('message')}", file=sys.stderr)

    result = run(args.instruction, workdir, ref, max_steps=args.max_steps,
                 timeout_s=args.timeout,
                 on_event=None if args.quiet else show,
                 profile=args.profile)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Written before the exit code is decided, so a crash still leaves a row behind.
        out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    slim = {k: v for k, v in result.items() if k != "trace"}
    print(json.dumps(slim, indent=1, ensure_ascii=False))
    # Exit 0 means "the turn ran". Whether the task was solved is the checker's call, and
    # a benchmark that conflates the two grades the harness on the model's mistakes.
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

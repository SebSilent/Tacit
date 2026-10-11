"""The optional model pass over finished transcripts.

Heuristics find the sentences that *look* like rules. A model can find the ones
that only *mean* like them — a rule the user stated obliquely, or a preference
they showed three times without ever naming. That is the only reason this file
exists, and it is also the reason it is off by default: a model will find a rule
in anything if you ask it hard enough, and every thing it invents costs somebody
a trip to the review queue.

Three things keep it honest:

* It is off until a person turns it on, in settings, on top of learning being on.
* It proposes. It never writes memory, never edits a prompt, never applies itself.
  The output of this module is input to the same gates `analyzer.detect` uses,
  and a finding that cannot cite a turn that exists is dropped there.
* It costs a call per session, on a timer, off the request path — never inside a
  turn. If it raises, the sweep continues on heuristics alone.

The prompt is in `ai.prompts.PROPOSAL_PASS` with its worked examples, because the
difference between this pass and the noise it is meant to replace is the prompt.
"""

from __future__ import annotations

import re

from . import audit, config, providers
from .ai import engine, prompts
from .plan import _array

# How much transcript one call reads, and how many findings it may return. Both
# are deliberately small: a sweep that reads everything and reports everything is
# a sweep that produces a queue nobody opens.
MAX_TURNS = 60
MAX_CHARS = 12000
MAX_FINDINGS = 6


def enabled() -> bool:
    """Off unless a person switched it on *and* learning is still proposing.

    The default is off and stays off: this is the only part of the memory system
    that spends a model call, and it should not do that on somebody's behalf
    because a default somewhere was written generously.
    """
    # Imported here, not at the top: `analyzer` runs this pass, so a module-level
    # import between the two is a cycle, and the cap constants are the only thing
    # this module needs from it.
    from . import analyzer
    if not analyzer.enabled():
        return False
    return bool((providers.load().get("analyzer") or {}).get("model_pass"))


def _transcript(messages: list[dict]) -> tuple[str, list[int]]:
    """The session as the model sees it, and the turn numbers it may cite.

    Turn indices are printed with every line so the model can point at one, and
    the list is what `review` checks those pointers against. Long tool output is
    cut rather than dropped: the interesting part of a failing command is usually
    its first lines.
    """
    lines: list[str] = []
    valid: list[int] = []
    used = 0
    start = max(0, len(messages) - MAX_TURNS)
    # The real index into `messages` is carried through the loop rather than
    # recovered with messages.index(): a transcript routinely contains two
    # identical messages ("ok" twice), and index() would report the first one for
    # both, so a citation pointing at turn 3 would silently mean turn 9.
    for real, m in enumerate(messages):
        if real < start:
            continue
        role = str(m.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        text = re.sub(r"\s+", " ", str(m.get("content") or "")).strip()
        if not text:
            continue
        line = f"[{real}] {role}: {text[:900]}"
        used += len(line)
        if used > MAX_CHARS:
            break
        lines.append(line)
        valid.append(real)
    return "\n".join(lines), valid


def review(session: dict, ref: str | None = None) -> list[dict]:
    """Ask the model for the rules in one session. Returns candidates, never truth.

    Each entry is `{"content", "kind", "turn", "confidence"}` in the shape
    `analyzer.gate` consumes. The cited turn is checked against the transcript
    here as well as there, because a model that cites a turn that does not exist
    has told us where its finding actually came from: nowhere.
    """
    from . import analyzer  # lazy, see enabled()
    messages = session.get("messages") or []
    if len(messages) < 4:
        # Two exchanges is not a pattern, it is a coincidence, and it is not
        # worth a call to find out which.
        return []
    transcript, valid = _transcript(messages)
    if not transcript:
        return []
    asked = prompts.PROPOSAL_PASS.replace("{n}", str(MAX_FINDINGS)).replace(
        "{transcript}", transcript)
    try:
        res = engine.chat([{"role": "system", "content": prompts.PROPOSAL_SYSTEM},
                           {"role": "user", "content": asked}],
                          ref=ref or config.delegate_model() or None,
                          temperature=0.1, max_tokens=2000)
        raw = res.get("content") or ""
    except engine.EngineError as exc:
        # A pass that cannot run is not an error a person needs to see: the
        # heuristics still ran, and the sweep is not about to lose that.
        audit.record("proposal_pass_failed", session=session.get("id") or "",
                     backend="proposal_pass", status="error", error=str(exc)[:200])
        return []

    found: list[dict] = []
    for item in _array(raw, MAX_FINDINGS * 3):
        if not isinstance(item, dict):
            continue
        try:
            turn = int(item.get("turn"))
        except (TypeError, ValueError):
            continue
        if turn not in valid:
            continue
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if not content:
            continue
        try:
            # A model asked for a number sometimes answers "high". That is not a
            # reason to lose the finding, nor a reason to crash the sweep.
            confidence = float(item.get("confidence") or 0.6)
        except (TypeError, ValueError):
            confidence = 0.6
        found.append({"content": content[:analyzer.MAX_SNIPPET],
                      "kind": str(item.get("kind") or "rule"),
                      "turn": turn,
                      "confidence": confidence})
        if len(found) >= MAX_FINDINGS:
            break
    audit.record("proposal_pass_ran", session=session.get("id") or "",
                 backend="proposal_pass", status="ok", findings=len(found),
                 turns=len(valid))
    return found


def settings() -> dict:
    """What the pass would do right now, for the settings screen to show."""
    return {"model_pass": enabled(), "max_findings": MAX_FINDINGS,
            "max_turns": MAX_TURNS, "min_turns": 4}


def configure(on: bool | None = None) -> dict:
    """Turn the pass on or off. On is a decision a person makes, never a default."""
    if on is None:
        return settings()
    providers.save({"analyzer": {"model_pass": bool(on)}})
    audit.record("proposal_pass_settings", backend="proposal_pass",
                 status="ok", model_pass=bool(on))
    return settings()

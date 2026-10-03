"""Thinking levels resolved against what a model actually supports.

Ollama applies supported names exactly and sends unsupported ones to the model
default, so a level missing from a model's own list is not an error, it is a silent
no-op. The list only exists at /api/show, so it is read from there and cached on the
model rather than guessed at from a table that fits one provider.
"""

from __future__ import annotations

import httpx

# Order, not equivalence. Names a model does not list are mapped to the nearest one
# it does, instead of being passed through to resolve as the default.
RANK = {"none": -1, "off": -1, "minimal": 0, "low": 1, "medium": 2,
        "high": 3, "xhigh": 4, "max": 5}

TIMEOUT = 30

# The names worth testing on a provider that does not publish a list. They are
# candidates to verify, not a menu to offer: a level only reaches the interface
# after the endpoint has been seen to accept it.
CANDIDATES = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]

# A probe has to leave room for the trace it is measuring. Comparing two levels
# under a cap that truncates both produces equal lengths and the false conclusion
# that effort does nothing. Measured this way, low/high/max on deepseek-v4.1-flash
# came out 346/600/609 characters; the same sweep at 300 tokens came out flat.
PROBE_TOKENS = 1500
PROBE_QUESTION = ("Two towns are 300 km apart. A train leaves each at 60 and 40 km/h at "
                  "the same time. A bird flies 90 km/h between them, turning back and "
                  "forth until the trains meet. How far does the bird travel? Work it "
                  "out, then give the number.")
# A named level has to beat a lower one by this much before it is offered as a
# separate rung. Below that the endpoint is accepting the word and ignoring it.
GRADED_RATIO = 1.3


def _ask(ref: str | None, effort: str | None, tokens: int = PROBE_TOKENS) -> dict:
    """One non-streaming request with an exact effort name, unclamped.

    Deliberately bypasses engine.stream_chat: that applies the clamp this feeds, so
    probing through it would test the clamp instead of the endpoint.
    """
    model = _entry(ref)
    base = str(model.get("baseUrl") or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "no baseUrl"}
    body = {"model": model.get("model"), "max_tokens": tokens,
            "messages": [{"role": "user", "content": PROBE_QUESTION}]}
    if effort:
        body["reasoning_effort"] = effort
    try:
        r = httpx.post(base + "/chat/completions",
                       headers={"Authorization": f"Bearer {model.get('apiKey')}",
                                "Content-Type": "application/json"},
                       json=body, timeout=120)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "error": r.text[:200]}
    try:
        msg = (r.json().get("choices") or [{}])[0].get("message") or {}
    except Exception:
        return {"ok": False, "error": "unreadable response"}
    reason = ""
    for k in ("reasoning_content", "reasoning", "thinking"):
        if isinstance(msg.get(k), str) and msg.get(k):
            reason = msg[k]
            break
    return {"ok": True, "reasoning": len(reason), "content": len(msg.get("content") or "")}


def detect(ref: str | None, *, deep: bool = False, probe: bool = True) -> dict:
    """Find out what a model really accepts, from the model itself.

    Two routes. Ollama publishes a list at /api/show, which is exact and free. Every
    other provider is OpenAI-compatible with nothing published, so the list is built
    by measurement: a name is only claimed if the endpoint takes it without error,
    and a rung is only claimed if it produces a measurably different trace. A name
    that is accepted and ignored is reported as no control, because that is what it
    is, and offering it would be a second version of the bug this replaced.
    """
    shown = discover(ref) if "ollama" in str(_entry(ref).get("baseUrl") or "") else {"ok": False}
    if shown.get("ok") and shown.get("thinking"):
        vals = shown["values"]
        return {"ok": True, "source": "show", "values": vals,
                "default": shown.get("default"),
                "graded": len([v for v in vals if isinstance(v, str)]) > 1}

    if not probe:
        return {"ok": False, "error": "no published list; probing was declined"}
    base = _ask(ref, None)
    if not base.get("ok"):
        return {"ok": False, "error": base.get("error") or "model unreachable"}

    accepted: list = []
    if base.get("reasoning"):
        accepted.append(True)          # thinks without being asked
    off = _ask(ref, "none")
    if off.get("ok") and not off.get("reasoning"):
        accepted.append(False)         # "none" genuinely stops it
    sizes: dict = {}
    names = ["low", "high"] + (["minimal", "medium", "xhigh", "max"] if deep else [])
    for name in names:
        got = _ask(ref, name)
        if got.get("ok"):
            sizes[name] = got.get("reasoning") or 0
    graded = bool(sizes)
    if {"low", "high"} <= set(sizes):
        graded = sizes["high"] >= sizes["low"] * GRADED_RATIO
    accepted.extend(sorted(sizes, key=lambda n: RANK.get(n, 0)))
    if not graded:
        # Names went down without error but changed nothing measurable. Dropping them:
        # a word the endpoint accepts and ignores is not a setting, and offering it is
        # the same guessing this whole change replaced. What survives is only what was
        # actually observed, which for such a model is on and off.
        accepted = [v for v in accepted if isinstance(v, bool)]
    return {"ok": True, "source": "probe", "values": accepted,
            "default": None, "graded": graded, "sizes": sizes,
            "baseline_reasoning": base.get("reasoning") or 0}


def save(ref: str | None, found: dict) -> dict:
    """Cache what detect() found on the model, the way context windows are cached."""
    from . import config
    if not found.get("ok"):
        return found
    pid, _, mid = str(ref or "").partition("/")
    reg = config.registry()
    spec = (reg.get("providers") or {}).get(pid)
    if not spec:
        return {"ok": False, "error": "provider not found"}
    for m in (spec.get("models") or []):
        if m.get("id") == mid:
            m["thinkingValues"] = found.get("values") or []
            m["thinkingDefault"] = found.get("default")
            m["thinkingSource"] = found.get("source")
            m["thinkingGraded"] = bool(found.get("graded"))
            if found.get("values"):
                m["reasoning"] = True
            config.save_registry(reg)
            return {"ok": True, "ref": ref, "values": m["thinkingValues"],
                    "source": m["thinkingSource"], "graded": m["thinkingGraded"]}
    return {"ok": False, "error": "model not found"}


def levels(ref: str | None) -> list[str]:
    """The picker's contents for this model, from what was discovered.

    'default' is always available and means send nothing, which is the only honest
    option for a model whose behaviour was never measured.
    """
    out = ["default"]
    vals = values(ref)
    if not vals:
        return out
    if any(v is False or str(v).lower() in ("none", "false") for v in vals):
        out.append("off")
    named = [str(v) for v in vals if isinstance(v, str)]
    if named:
        out.extend(sorted(named, key=lambda n: RANK.get(n, 0)))
    elif any(v is True for v in vals):
        out.append("on")
    return out


def known(ref: str | None) -> bool:
    """True only when something was actually learned.

    The registry projects these keys with a None default, so testing for the key
    would report every model as known and skip detection for all of them forever.
    """
    m = _entry(ref)
    return bool(m.get("thinkingValues")) or bool(m.get("thinkingSource"))


def meta(ref: str | None) -> dict:
    m = _entry(ref)
    return {"source": m.get("thinkingSource"), "graded": bool(m.get("thinkingGraded")),
            "default": m.get("thinkingDefault"), "known": known(ref)}


def ensure(ref: str | None, *, probe: bool = True, deep: bool = False) -> dict:
    """Discover once, then never again unless asked.

    Called when a person selects a model, which is the moment the answer is worth
    paying for: detection costs a few small requests, and the result is cached on
    the model like a context window. Bulk import does not use the probe path, so
    gathering a provider's catalogue stays free.
    """
    if known(ref):
        return {"ok": True, "cached": True, **meta(ref)}
    found = detect(ref, deep=deep)
    if not found.get("ok"):
        return found
    saved = save(ref, found)
    return {"ok": True, "cached": False, **meta(ref),
            "values": saved.get("values"), "probe": bool(found.get("source") == "probe")}


def _entry(ref) -> dict:
    """Accept a model ref or an already resolved model dict."""
    if isinstance(ref, dict):
        return ref
    from . import config
    try:
        return config.resolve_model(ref) or {}
    except Exception:
        return {}


def values(ref: str | None) -> list:
    """The thinking values a model advertises, or [] when it never was probed."""
    raw = _entry(ref).get("thinkingValues")
    return list(raw) if isinstance(raw, list) else []


def default_level(ref: str | None) -> str | None:
    return _entry(ref).get("thinkingDefault") or None


def supports_off(ref: str | None) -> bool:
    """False when the model has no off switch, in which case 'off' cannot be honored."""
    vals = values(ref)
    if not vals:
        return True          # unknown: ask for it anyway, the provider can ignore it
    return any(v is False or str(v).lower() in ("none", "false") for v in vals)


def thinks(ref: str | None) -> bool:
    return bool(_entry(ref).get("reasoning"))


def _nearest(want: str, allowed: list[str]) -> str | None:
    """Closest supported name by rank, preferring not to spend more than asked."""
    if want in allowed:
        return want
    target = RANK.get(want)
    if target is None:
        return None
    scored = sorted(((abs(RANK.get(a, 0) - target), RANK.get(a, 0), a) for a in allowed),
                    key=lambda x: (x[0], x[1]))
    return scored[0][2] if scored else None


def send(ref: str | None, level: str | None) -> str | None:
    """The value to put in reasoning_effort for this model and level, or None to omit."""
    if not level or level == "default":
        # Nothing sent means the model's own default. This is the only option that
        # makes no claim about the model, so it is always offered.
        return None
    # Only strings are levels. A model whose values are [false, true] has an on/off
    # switch and no ladder, and any recognised effort name maps to true there, so
    # there is nothing to clamp towards.
    allowed = [str(v) for v in values(ref) if isinstance(v, str)]
    if level in ("none", "off"):
        # A model with no off switch still gets asked, and the interface can say so
        # rather than pretend the level did something.
        return "none"
    if level == "on":
        # A boolean switch needs a name the provider recognises as true. The cheap
        # one, since this model cannot grade it anyway.
        return "low"
    if not allowed:
        return level          # nothing discovered, or a boolean-only switch
    return _nearest(level, allowed) or level


def discover(ref: str | None) -> dict:
    """Read /api/show and return what it says. Writes nothing; the caller stores it."""
    model = _entry(ref)
    base = str(model.get("baseUrl") or "").rstrip("/")
    if not base or "ollama" not in base:
        return {"ok": False, "error": "only the ollama API exposes /api/show"}
    probe = base[: -len("/v1")] if base.endswith("/v1") else base
    url = probe.rstrip("/") + "/api/show"
    try:
        r = httpx.post(url, headers={"Authorization": f"Bearer {model.get('apiKey')}",
                                     "Content-Type": "application/json"},
                       json={"model": model.get("model")}, timeout=TIMEOUT)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        d = r.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    th = d.get("thinking") or {}
    caps = d.get("capabilities") or []
    vals = th.get("values")
    return {"ok": True, "capabilities": caps,
            "thinking": isinstance(vals, list),
            "values": vals or [],
            "default": th.get("default"),
            "reasoning": "thinking" in caps or bool(vals)}

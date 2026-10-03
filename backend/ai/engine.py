import json
from typing import Iterator

import httpx

from .. import anthropic, config

REASON_KEYS = ("reasoning_content", "reasoning", "thinking")
TIMEOUT = httpx.Timeout(connect=20.0, read=900.0, write=60.0, pool=20.0)


class EngineError(RuntimeError):
    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def _target(ref: str | None):
    m = config.resolve_model(ref)
    if not m:
        raise EngineError("no model configured — add a provider in Settings", 0, "")
    if not m["baseUrl"]:
        raise EngineError(f"no endpoint for {m['ref']}", 0, "")
    return m


def _headers(model: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if model["apiKey"]:
        h["Authorization"] = f"Bearer {model['apiKey']}"
    return h


def _payload(model: dict, messages: list[dict], stream: bool, tools=None,
             temperature=None, max_tokens=None, reasoning_effort=None) -> dict:
    body = {"model": model["model"], "messages": messages, "stream": stream}
    body["temperature"] = config.TEMPERATURE if temperature is None else temperature
    budget = max_tokens if max_tokens is not None else model.get("maxTokens") or None
    if budget:
        body["max_tokens"] = int(budget)
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if stream:
        body["stream_options"] = {"include_usage": True}
    # The flag says the model thinks. The name it is sent under has to be one the
    # model actually lists, because a name it does not list is silently resolved to
    # that model's default rather than rejected. "none" is sent regardless: a model
    # that does not think ignores it, and a model that thinks by default is the only
    # kind that can be turned off this way.
    from .. import thinking
    effort = thinking.send(model, reasoning_effort)
    if effort and (model.get("reasoning") or effort == "none"):
        body["reasoning_effort"] = effort
    return body


def chat(messages: list[dict], *, ref: str | None = None, tools=None,
         temperature=None, max_tokens=None) -> dict:
    model = _target(ref)
    if anthropic.flavour(model) == "anthropic":
        return _chat_anthropic(model, messages, tools, temperature, max_tokens)
    url = f"{model['baseUrl']}/chat/completions"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(url, headers=_headers(model), json=_payload(
                model, messages, False, tools, temperature, max_tokens))
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        raise EngineError(f"HTTP {e.response.status_code}: {e.response.text[:400]}",
                          e.response.status_code, e.response.text[:400]) from e
    except httpx.HTTPError as e:
        raise EngineError(f"connection error: {e}") from e

    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError) as e:
        raise EngineError(f"unexpected response shape: {str(data)[:400]}") from e

    return {
        "message": msg,
        "content": msg.get("content") or "",
        "tool_calls": msg.get("tool_calls") or [],
        "finish_reason": choice.get("finish_reason") or "",
        "usage": _usage(data.get("usage")),
        "model": model,
    }


def _chat_anthropic(model: dict, messages: list[dict], tools, temperature,
                    max_tokens) -> dict:
    """One non-streaming Messages API call, in the same shape `chat` returns.

    This is the path the compaction summariser uses, so a Claude user gets cached
    summaries too rather than silently falling back to a protocol the endpoint does
    not serve.
    """
    url = anthropic.endpoint(model)
    want_cache = anthropic.cache_enabled(model)
    for use_cache in ([True, False] if want_cache else [False]):
        body = anthropic.payload(model, messages, False, tools, temperature,
                                 max_tokens, None, cache=use_cache)
        body.pop("_cache_breakpoints", None)
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                r = client.post(url, headers=anthropic.headers(model),
                                json=anthropic.wire_body(body))
                if r.status_code >= 400:
                    text = r.text or ""
                    if use_cache and anthropic.is_cache_rejection(r.status_code, text):
                        continue          # retry once without the markers
                    raise EngineError(f"HTTP {r.status_code}: {text[:400]}",
                                      r.status_code, text[:400])
                data = r.json()
        except httpx.HTTPStatusError as e:
            raise EngineError(f"HTTP {e.response.status_code}: {e.response.text[:400]}",
                              e.response.status_code, e.response.text[:400]) from e
        except httpx.HTTPError as e:
            raise EngineError(f"connection error: {e}") from e

        text_parts, thinking_parts, calls = [], [], []
        for block in data.get("content") or []:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text") or "")
            elif kind in ("thinking", "redacted_thinking"):
                thinking_parts.append(block.get("thinking") or block.get("data") or "")
            elif kind == "tool_use":
                calls.append({"id": block.get("id") or "", "type": "function",
                              "function": {"name": block.get("name") or "",
                                           "arguments": json.dumps(block.get("input") or {})}})
        msg = {"role": "assistant", "content": "".join(text_parts)}
        if thinking_parts:
            msg["reasoning_content"] = "".join(thinking_parts)
        if calls:
            msg["tool_calls"] = calls
        return {"message": msg, "content": msg["content"], "tool_calls": calls,
                "finish_reason": data.get("stop_reason") or "",
                "usage": anthropic.usage_of(data.get("usage")), "model": model}
    raise EngineError("anthropic request failed", 0, "")


def _usage(raw) -> dict:
    if not raw:
        return {"input": 0, "output": 0, "total": 0, "cache_read": 0, "cache_write": 0}
    # A turn re-sends its whole transcript on every step, so the fixed prefix and
    # everything before the last message is the same bytes each time. Whether the
    # provider actually charged for that again is the difference between a cheap
    # long session and an expensive one, and it is reported — then discarded here,
    # which is why the interface's cache figures were always blank.
    details = raw.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    return {
        "input": raw.get("prompt_tokens") or 0,
        "output": raw.get("completion_tokens") or 0,
        "total": raw.get("total_tokens") or 0,
        # OpenAI-compatible providers report a cache read inside prompt details;
        # Anthropic-shaped ones report both directions at the top level.
        "cache_read": (details.get("cached_tokens")
                       or raw.get("cache_read_input_tokens") or 0),
        "cache_write": raw.get("cache_creation_input_tokens") or 0,
    }


def _mark_openai_cache(body: dict) -> dict:
    """Add Anthropic-style breakpoints to an OpenAI-shaped body.

    Only for gateways that document passthrough. The stable prefix is the system
    message, so that is where the mark goes; the transcript is left alone, because a
    second mark there would be rewritten on every step and cost a cache write each
    time instead of a read.

    Returns a new body. Mutating the caller's message list would rewrite the live
    transcript the agent loop is holding, and the retry-without-markers path would
    then rebuild its "plain" request from the already-marked copy and fail again.
    """
    messages = list(body.get("messages") or [])
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get("role") != "system":
            continue
        text = m.get("content")
        if isinstance(text, str) and text.strip():
            messages[i] = {**m, "content": [
                {"type": "text", "text": text,
                 "cache_control": {"type": "ephemeral"}}]}
        break
    return {**body, "messages": messages}


def _stream_anthropic(model: dict, messages: list[dict], tools, temperature,
                      max_tokens, reasoning_effort):
    """Stream one turn from the native Messages API, translated back to Tacit's events.

    A rejection of the cache markers is visible in the HTTP status line, before a
    single event has been streamed, so retrying without them is always safe: nothing
    has been yielded for the caller to see twice. Anything else is raised as itself.
    """
    url = anthropic.endpoint(model)
    want_cache = anthropic.cache_enabled(model)
    attempts = [True, False] if want_cache else [False]
    state = anthropic.StreamState()
    breakpoints = 0

    for index, use_cache in enumerate(attempts):
        body = anthropic.payload(model, messages, True, tools, temperature,
                                 max_tokens, reasoning_effort, cache=use_cache)
        breakpoints = body.pop("_cache_breakpoints", 0)
        state = anthropic.StreamState()
        rejected = False
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                with client.stream("POST", url, headers=anthropic.headers(model),
                                   json=anthropic.wire_body(body)) as r:
                    if r.status_code >= 400:
                        text = r.read().decode("utf-8", "replace")
                        if use_cache and breakpoints and anthropic.is_cache_rejection(
                                r.status_code, text):
                            rejected = True
                        else:
                            raise EngineError(f"HTTP {r.status_code}: {text[:400]}",
                                              r.status_code, text[:400])
                    else:
                        for line in r.iter_lines():
                            if not line:
                                continue
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            chunk = line[5:].strip()
                            if not chunk or chunk == "[DONE]":
                                continue
                            try:
                                ev = json.loads(chunk)
                            except json.JSONDecodeError:
                                continue
                            try:
                                yield from state.feed(ev)
                            except RuntimeError as exc:
                                raise EngineError(str(exc)) from exc
        except httpx.HTTPError as e:
            raise EngineError(f"connection error: {e}") from e
        if rejected and index + 1 < len(attempts):
            continue
        break

    calls = state.tool_calls()
    if calls:
        yield {"type": "tool_calls", "calls": calls}
    yield {"type": "usage", "usage": state.usage}
    yield {"type": "done", "finish": state.stop_reason, "model": model,
           "cache_breakpoints": breakpoints}


def stream_chat(messages: list[dict], *, ref: str | None = None, tools=None,
                temperature=None, max_tokens=None, reasoning_effort=None) -> Iterator[dict]:
    model = _target(ref)
    if anthropic.flavour(model) == "anthropic":
        yield from _stream_anthropic(model, messages, tools, temperature,
                                     max_tokens, reasoning_effort)
        return
    url = f"{model['baseUrl']}/chat/completions"
    caching = anthropic.cache_enabled(model)
    body = _payload(model, messages, True, tools, temperature, max_tokens, reasoning_effort)
    if caching:
        body = _mark_openai_cache(body)

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            with client.stream("POST", url, headers=_headers(model), json=body) as r:
                if r.status_code >= 400:
                    text = r.read().decode("utf-8", "replace")
                    if caching and anthropic.is_cache_rejection(r.status_code, text):
                        # A gateway that does not implement passthrough rejects an
                        # unknown field. Retrying without the markers is the honest
                        # recovery: caching is an optimisation, not a requirement,
                        # and it must never be the reason a turn fails.
                        plain = _payload(model, messages, True, tools, temperature,
                                         max_tokens, reasoning_effort)
                        yield from _stream_openai(model, url, plain)
                        return
                    raise EngineError(f"HTTP {r.status_code}: {text[:400]}", r.status_code, text[:400])
                yield from _consume_openai_stream(r, model)
    except httpx.HTTPError as e:
        raise EngineError(f"connection error: {e}") from e


def _consume_openai_stream(r, model: dict | None = None):
    """Parse an OpenAI-shaped SSE stream into Tacit's events."""
    pending: dict[int, dict] = {}
    usage = None
    finish = ""
    for line in r.iter_lines():
        if not line:
            continue
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            continue
        try:
            ev = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if ev.get("usage"):
            usage = ev["usage"]
        choices = ev.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
        delta = choice.get("delta") or {}
        if isinstance(delta.get("content"), str) and delta["content"]:
            yield {"type": "text", "delta": delta["content"]}
        for k in REASON_KEYS:
            if isinstance(delta.get(k), str) and delta[k]:
                yield {"type": "reason", "delta": delta[k]}
                break
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            cur = pending.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                cur["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                cur["name"] += fn["name"]
            if fn.get("arguments"):
                cur["arguments"] += fn["arguments"]
    if pending:
        yield {"type": "tool_calls", "calls": [
            {"id": v["id"] or f"call_{i}", "name": v["name"], "arguments": v["arguments"]}
            for i, (_, v) in enumerate(sorted(pending.items()))
        ]}
    yield {"type": "usage", "usage": _usage(usage)}
    yield {"type": "done", "finish": finish, "model": model}


def _stream_openai(model: dict, url: str, body: dict):
    """One plain OpenAI-shaped request, no cache markers."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            with client.stream("POST", url, headers=_headers(model), json=body) as r:
                if r.status_code >= 400:
                    text = r.read().decode("utf-8", "replace")
                    raise EngineError(f"HTTP {r.status_code}: {text[:400]}",
                                      r.status_code, text[:400])
                yield from _consume_openai_stream(r, model)
    except httpx.HTTPError as e:
        raise EngineError(f"connection error: {e}") from e


_CONTEXT_KEYS = ("contextWindow", "context_window", "context_length", "contextLength",
                 "max_model_len", "max_context_length", "max_context_tokens", "context_size",
                 "n_ctx")
_MAXTOKEN_KEYS = ("maxTokens", "max_tokens", "max_output_tokens", "max_completion_tokens")


def _meta_int(obj: dict, keys, depth: int = 0) -> int:
    """First positive integer found under any of `keys` (searching nested meta)."""
    if not isinstance(obj, dict) or depth > 2:
        return 0
    for k in keys:
        v = obj.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
        if isinstance(v, str):
            try:
                n = int(v.strip())
            except ValueError:
                continue
            if n > 0:
                return n
    for nested in ("meta", "arch", "details"):
        if isinstance(obj.get(nested), dict):
            found = _meta_int(obj[nested], keys, depth + 1)
            if found:
                return found
    return 0


def remote_model_details(base_url: str, api_key: str = "") -> list[dict]:
    """List a provider's models with whatever context window the endpoint exposes.

    OpenAI-compatible `/models` responses vary: some return bare ids, others add
    `context_length` (OpenRouter), `max_model_len` (vLLM) or similar. Where the
    endpoint says nothing, contextWindow/maxTokens are 0 and the UI stays blank.
    """
    url = f"{str(base_url).rstrip('/')}/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        raise EngineError(f"probe failed: {e}") from e
    rows = data.get("data") or data.get("models") or []
    out = []
    for m in rows:
        if isinstance(m, str):
            out.append({"id": m, "contextWindow": 0, "maxTokens": 0})
        elif isinstance(m, dict):
            mid = m.get("id") or m.get("name") or ""
            if not mid:
                continue
            out.append({"id": mid,
                        "contextWindow": _meta_int(m, _CONTEXT_KEYS),
                        "maxTokens": _meta_int(m, _MAXTOKEN_KEYS)})
    return out


def remote_models(base_url: str, api_key: str = "") -> list[str]:
    return [m["id"] for m in remote_model_details(base_url, api_key)]

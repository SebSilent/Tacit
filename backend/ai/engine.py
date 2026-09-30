import json
from typing import Iterator

import httpx

from .. import config

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
    if reasoning_effort and model.get("reasoning"):
        body["reasoning_effort"] = reasoning_effort
    return body


def chat(messages: list[dict], *, ref: str | None = None, tools=None,
         temperature=None, max_tokens=None) -> dict:
    model = _target(ref)
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


def _usage(raw) -> dict:
    if not raw:
        return {"input": 0, "output": 0, "total": 0}
    return {
        "input": raw.get("prompt_tokens") or 0,
        "output": raw.get("completion_tokens") or 0,
        "total": raw.get("total_tokens") or 0,
    }


def stream_chat(messages: list[dict], *, ref: str | None = None, tools=None,
                temperature=None, max_tokens=None, reasoning_effort=None) -> Iterator[dict]:
    model = _target(ref)
    url = f"{model['baseUrl']}/chat/completions"
    body = _payload(model, messages, True, tools, temperature, max_tokens, reasoning_effort)
    pending: dict[int, dict] = {}
    usage = None
    finish = ""

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            with client.stream("POST", url, headers=_headers(model), json=body) as r:
                if r.status_code >= 400:
                    text = r.read().decode("utf-8", "replace")
                    raise EngineError(f"HTTP {r.status_code}: {text[:400]}", r.status_code, text[:400])
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
    except httpx.HTTPError as e:
        raise EngineError(f"connection error: {e}") from e

    if pending:
        yield {"type": "tool_calls", "calls": [
            {"id": v["id"] or f"call_{i}", "name": v["name"], "arguments": v["arguments"]}
            for i, (_, v) in enumerate(sorted(pending.items()))
        ]}
    yield {"type": "usage", "usage": _usage(usage)}
    yield {"type": "done", "finish": finish, "model": model}


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

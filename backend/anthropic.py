"""Anthropic's native Messages API, and prompt-cache breakpoints.

Tacit speaks OpenAI-compatible ``/chat/completions`` everywhere else. Anthropic's
endpoint is different enough that passing an OpenAI-shaped body to it does not work:
``system`` is a top-level parameter rather than a message, tools carry
``input_schema`` instead of ``parameters``, a tool result is a ``tool_result``
content block inside a *user* message, ``max_tokens`` is required, and the stream is
a sequence of typed events rather than OpenAI's delta chunks.

The reason to speak it natively at all is caching. Anthropic does not cache
automatically; a request has to mark where the cacheable prefix ends with
``cache_control``. Without that, an agent loop re-pays for its tools and standing
prompt on every single step — which is the exact cost this project exists to make
visible. With two breakpoints, the stable prefix is written once and read back.

Everything else stays as it was: OpenAI-compatible endpoints keep automatic prefix
caching (which needs no markers) and get nothing added to their requests, because a
server that has never seen ``cache_control`` may reject the body outright. Which
strategy applies is decided by :func:`flavour`, reported to the interface, and can
be overridden — and a rejection is always retried once without the markers, so
turning caching on can never be the reason a turn fails.
"""

from __future__ import annotations

import json

import httpx

from . import config

MESSAGES_PATH = "/v1/messages"


def flavour(model: dict) -> str:
    """Which wire protocol and caching strategy this endpoint speaks.

    ``anthropic``  native Messages API, explicit cache breakpoints
    ``openrouter`` OpenAI shape, but forwards ``cache_control`` to Anthropic models
    ``openai``     OpenAI shape, automatic prefix caching, nothing to add
    """
    base = str((model or {}).get("baseUrl") or "").lower()
    provider = str((model or {}).get("provider") or "").lower()
    if "anthropic.com" in base or provider == "anthropic":
        return "anthropic"
    if "openrouter" in base or provider == "openrouter":
        return "openrouter"
    return "openai"


def cache_enabled(model: dict) -> bool:
    """Whether to put breakpoints in this request."""
    mode = str(config.PROMPT_CACHE or "auto").lower()
    if mode == "off":
        return False
    if mode == "on":
        return True
    return flavour(model) in ("anthropic", "openrouter")


def endpoint(model: dict) -> str:
    base = str((model or {}).get("baseUrl") or "").rstrip("/")
    if base.endswith("/v1"):
        return base + "/messages"
    if base.endswith("/messages"):
        return base
    return base + MESSAGES_PATH


def headers(model: dict) -> dict:
    h = {"content-type": "application/json",
         "anthropic-version": config.ANTHROPIC_VERSION}
    key = (model or {}).get("apiKey") or ""
    if key:
        # Anthropic authenticates with x-api-key. A Bearer token is accepted by some
        # gateways, so both are sent and the one that is wrong is ignored.
        h["x-api-key"] = key
        h["authorization"] = f"Bearer {key}"
    return h


# ── request translation ───────────────────────────────────────────────────
def _cache_mark(block: dict) -> dict:
    block["cache_control"] = {"type": "ephemeral"}
    return block


def _tools(tools) -> list[dict]:
    """OpenAI tool schemas to Anthropic's shape."""
    out = []
    for t in tools or []:
        fn = (t or {}).get("function") or t or {}
        schema = fn.get("parameters") or fn.get("input_schema") or {"type": "object"}
        row = {"name": fn.get("name") or "", "input_schema": schema}
        if fn.get("description"):
            row["description"] = fn["description"]
        out.append(row)
    return out


def _image_block(part: dict) -> dict | None:
    url = ((part or {}).get("image_url") or {}).get("url") or ""
    if not url.startswith("data:"):
        return None
    try:
        head, _, data = url.partition(",")
        media = head[5:].split(";", 1)[0] or "image/png"
        return {"type": "image",
                "source": {"type": "base64", "media_type": media, "data": data}}
    except Exception:  # noqa: BLE001
        return None


def _user_blocks(content) -> list[dict]:
    """A user message's content to Anthropic blocks, text and images both."""
    if isinstance(content, list):
        out = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text"):
                out.append({"type": "text", "text": str(part["text"])})
            elif part.get("type") == "image_url":
                block = _image_block(part)
                if block:
                    out.append(block)
        return out or [{"type": "text", "text": ""}]
    return [{"type": "text", "text": str(content or "")}]


def translate(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """OpenAI-shaped messages to (system_blocks, anthropic_messages).

    Two structural rules do the work. ``system`` messages are lifted out into the
    top-level parameter, because Anthropic has no system role. And a ``tool``
    message is not a role of its own — it is a ``tool_result`` block inside a user
    message — so a run of consecutive results has to be merged into one turn, or the
    API rejects the sequence.
    """
    system: list[dict] = []
    out: list[dict] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            text = m.get("content")
            if isinstance(text, list):
                system.extend(_user_blocks(text))
            elif str(text or "").strip():
                system.append({"type": "text", "text": str(text)})
            continue
        if role == "tool":
            block = {"type": "tool_result",
                     "tool_use_id": str(m.get("tool_call_id") or ""),
                     "content": str(m.get("content") or "")}
            # Merge into the previous user turn if it is already carrying results.
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and out[-1]["content"] and out[-1]["content"][-1].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if role == "assistant":
            blocks: list[dict] = []
            body = m.get("content")
            if isinstance(body, list):
                blocks.extend(_user_blocks(body))
            elif str(body or "").strip():
                blocks.append({"type": "text", "text": str(body)})
            for call in m.get("tool_calls") or []:
                fn = (call or {}).get("function") or {}
                raw = fn.get("arguments") or "{}"
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                except json.JSONDecodeError:
                    parsed = {}
                blocks.append({"type": "tool_use", "id": str(call.get("id") or ""),
                               "name": fn.get("name") or "", "input": parsed})
            if not blocks:
                continue
            out.append({"role": "assistant", "content": blocks})
            continue
        if role == "user":
            out.append({"role": "user", "content": _user_blocks(m.get("content"))})
        elif role in ("summary",):
            system.append({"type": "text", "text": str(m.get("content") or "")})
    return system, out


def _mark_cache(system: list[dict], messages: list[dict], tools: list[dict]) -> int:
    """Place breakpoints and return how many were placed.

    Anthropic caches the prefix up to and including a marked block, in the order
    tools, system, messages, and allows four marks. Two are enough and two is the
    documented pattern for an agent loop: one on the system block, which caches the
    tools and the standing prompt together — the part re-paid on every step
    otherwise — and one on the last message, so each step extends the cached
    transcript instead of rewriting it.
    """
    placed = 0
    if tools:
        _cache_mark(tools[-1])
        placed += 1
    if system:
        _cache_mark(system[-1])
        placed += 1
    for m in reversed(messages):
        content = m.get("content")
        if isinstance(content, list) and content:
            _cache_mark(content[-1])
            placed += 1
            break
    return placed


def payload(model: dict, messages: list[dict], stream: bool, tools=None,
            temperature=None, max_tokens=None, reasoning_effort=None,
            cache: bool | None = None) -> dict:
    system, translated = translate(messages)
    anthropic_tools = _tools(tools) if tools else []
    if cache is None:
        cache = cache_enabled(model)
    breakpoints = _mark_cache(system, translated, anthropic_tools) if cache else 0

    body: dict = {"model": (model or {}).get("model") or "", "stream": stream,
                  "messages": translated}
    if system:
        body["system"] = system
    if anthropic_tools:
        body["tools"] = anthropic_tools
        body["tool_choice"] = {"type": "auto"}

    # Anthropic requires max_tokens; without it the request is rejected outright.
    budget = int(max_tokens or (model or {}).get("maxTokens")
                 or config.ANTHROPIC_MAX_TOKENS)
    thinking = _thinking(reasoning_effort, budget)
    if thinking:
        body["thinking"] = thinking
        # Temperature must be unset (it defaults to 1) while extended thinking is on;
        # sending another value is a 400.
        body["max_tokens"] = max(budget, int(thinking["budget_tokens"]) + 1024)
    else:
        body["max_tokens"] = budget
        temp = config.TEMPERATURE if temperature is None else temperature
        if temp is not None:
            body["temperature"] = float(temp)
    body["_cache_breakpoints"] = breakpoints      # stripped before sending
    return body


def _thinking(reasoning_effort, budget: int) -> dict | None:
    """Map Tacit's thinking level onto Anthropic's extended-thinking switch."""
    level = str(reasoning_effort or "").strip().lower()
    if not level or level in ("none", "off", "default"):
        return None
    base = max(1024, min(int(config.ANTHROPIC_THINKING_BUDGET), max(1024, budget - 1024)))
    scale = {"minimal": 0.25, "low": 0.5, "medium": 1.0, "high": 2.0,
             "xhigh": 4.0, "max": 4.0}
    return {"type": "enabled",
            "budget_tokens": int(max(1024, min(base * scale.get(level, 1.0),
                                               max(1024, budget - 1024))))}


def wire_body(body: dict) -> dict:
    """The payload without Tacit's own bookkeeping keys."""
    return {k: v for k, v in (body or {}).items() if not k.startswith("_")}


# ── response translation ──────────────────────────────────────────────────
def usage_of(raw) -> dict:
    """Anthropic reports input at message_start and output at message_delta."""
    raw = raw or {}
    return {
        "input": int(raw.get("input_tokens") or 0),
        "output": int(raw.get("output_tokens") or 0),
        "total": int(raw.get("input_tokens") or 0) + int(raw.get("output_tokens") or 0),
        "cache_read": int(raw.get("cache_read_input_tokens") or 0),
        "cache_write": int(raw.get("cache_creation_input_tokens") or 0),
    }


def merge_usage(a: dict, b: dict) -> dict:
    out = dict(a or {})
    for key, value in (b or {}).items():
        out[key] = int(out.get(key) or 0) + int(value or 0)
    out["total"] = int(out.get("input") or 0) + int(out.get("output") or 0)
    return out


class StreamState:
    """Accumulates Anthropic's typed events into Tacit's event stream."""

    def __init__(self):
        self.blocks: dict[int, dict] = {}
        self.usage = {"input": 0, "output": 0, "total": 0,
                      "cache_read": 0, "cache_write": 0}
        self.stop_reason = ""

    def feed(self, ev: dict):
        """Yield zero or more Tacit events for one Anthropic event."""
        kind = ev.get("type")
        if kind == "message_start":
            self.usage = merge_usage(self.usage,
                                     usage_of((ev.get("message") or {}).get("usage")))
            return
        if kind == "content_block_start":
            block = ev.get("content_block") or {}
            self.blocks[int(ev.get("index") or 0)] = {
                "type": block.get("type") or "text", "id": block.get("id") or "",
                "name": block.get("name") or "", "text": "", "json": ""}
            return
        if kind == "content_block_delta":
            block = self.blocks.get(int(ev.get("index") or 0))
            delta = ev.get("delta") or {}
            dtype = delta.get("type")
            if block is None:
                block = self.blocks.setdefault(int(ev.get("index") or 0),
                                               {"type": "text", "id": "", "name": "",
                                                "text": "", "json": ""})
            if dtype == "text_delta":
                block["text"] += delta.get("text") or ""
                if delta.get("text"):
                    yield {"type": "text", "delta": delta["text"]}
            elif dtype in ("thinking_delta", "redacted_thinking_delta"):
                piece = delta.get("thinking") or delta.get("data") or ""
                block["text"] += piece
                if piece:
                    yield {"type": "reason", "delta": piece}
            elif dtype == "input_json_delta":
                block["json"] += delta.get("partial_json") or ""
            return
        if kind == "message_delta":
            self.usage = merge_usage(self.usage, usage_of(ev.get("usage")))
            reason = (ev.get("delta") or {}).get("stop_reason")
            if reason:
                self.stop_reason = reason
            return
        if kind == "error":
            raise RuntimeError(str((ev.get("error") or {}).get("message") or ev))

    def tool_calls(self) -> list[dict]:
        out = []
        for index in sorted(self.blocks):
            block = self.blocks[index]
            if block["type"] != "tool_use":
                continue
            try:
                arguments = block["json"] or "{}"
                json.loads(arguments)
            except json.JSONDecodeError:
                arguments = "{}"
            out.append({"id": block["id"] or f"call_{index}",
                        "name": block["name"], "arguments": arguments})
        return out


def is_cache_rejection(status: int, text: str) -> bool:
    """True when a request failed *because* of the cache markers.

    A gateway that does not implement passthrough rejects an unknown field, and the
    honest recovery is to retry once without it rather than to fail the turn. Anything
    else — auth, rate limit, a malformed tool call — must surface as itself.
    """
    if status not in (400, 422):
        return False
    low = str(text or "").lower()
    return "cache_control" in low or "cache control" in low or "unsupported parameter" in low

"""Token accounting for Tacit.

Everything the harness injects into a prompt — the base prompt, the skill index,
tool schemas, MCP tool schemas, memory blocks — has a visible cost. This module
is the one place that estimates those costs so the UI and the agent loop agree.

No tokenizer is assumed. If ``tiktoken`` happens to be installed we use it and
report exact counts; otherwise we fall back to a conservative character
heuristic and every count is an *estimate*. Callers must never present an
estimate as exact — use :func:`label` which carries the distinction for you.
"""

from __future__ import annotations

import json

# Conservative: real BPE tokenizers average ~4 chars/token on prose but fewer
# on code and JSON, so we round up rather than down.
CHARS_PER_TOKEN = 4

_ENCODER = None
_ENCODER_TRIED = False


def _encoder():
    global _ENCODER, _ENCODER_TRIED
    if not _ENCODER_TRIED:
        _ENCODER_TRIED = True
        try:
            import tiktoken  # type: ignore

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODER = None
    return _ENCODER


def exact() -> bool:
    """True when counts come from a real tokenizer rather than the heuristic."""
    return _encoder() is not None


def estimate_tokens(text) -> int:
    """Estimated tokens in ``text`` (exact if a tokenizer is available)."""
    if text is None:
        return 0
    if not isinstance(text, str):
        if isinstance(text, bool) or not text:
            return 0          # 0, False, empty containers carry no tokens
        text = str(text)
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def estimate_tool_schema_tokens(tool: dict) -> int:
    """Cost of one tool definition, as it lands in the request ``tools`` array."""
    try:
        return estimate_tokens(json.dumps(tool, ensure_ascii=False))
    except Exception:
        return estimate_tokens(str(tool))


def estimate_tools_tokens(tools) -> int:
    return sum(estimate_tool_schema_tokens(t) for t in (tools or []))


def estimate_message_tokens(message: dict) -> int:
    if not isinstance(message, dict):
        return estimate_tokens(str(message))
    total = 0
    content = message.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                total += estimate_tokens(part.get("text") or "")
    for call in message.get("tool_calls") or []:
        fn = (call or {}).get("function") or {}
        total += estimate_tokens(fn.get("name") or "")
        total += estimate_tokens(fn.get("arguments") or "")
    return total + 4  # per-message envelope (role, separators)


def estimate_messages_tokens(messages) -> int:
    return sum(estimate_message_tokens(m) for m in (messages or []))


def format_token_count(n) -> str:
    """Compact human-readable count: 950, 1.2k, 18.7k, 1.4M."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    n = max(0, n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def label(n) -> str:
    """Formatted count with the estimate/exact distinction made visible."""
    return format_token_count(n) if exact() else f"~{format_token_count(n)}"


def estimate_of(text, unit: str = "tokens") -> str:
    """One-liner for logs and tool output, e.g. ``~1.2k tokens (estimate)``."""
    n = estimate_tokens(text)
    suffix = "" if exact() else " (estimate)"
    return f"{label(n)} {unit}{suffix}"

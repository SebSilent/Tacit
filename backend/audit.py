"""The audit ledger: one append-only record of what actually happened.

Every important action writes exactly one line here. The point is not debugging,
it is that a claim about what a system did should be checkable afterwards. So the
ledger records which backend ran something, under which mode, how much it cost,
and whether it was blocked.

Secrets are masked on the way in. Nothing is ever edited or removed, and there is
no rotation: it is a plain JSONL file under ``~/.tacit`` the user can read,
grep, or delete.
"""

from __future__ import annotations

import json
import time

from . import config
from .mcp_client import looks_secret, mask_url

LIMIT = 2000
SECRET_KEYS = ("env", "headers", "token", "api_key", "apikey")
PATH_KEYS = ("command", "url", "endpoint")


def _clean(fields: dict) -> dict:
    """Mask anything that looks like a credential, at any depth."""
    out = {}
    for key, value in (fields or {}).items():
        low = str(key).lower()
        if isinstance(value, dict):
            if low in SECRET_KEYS or low.endswith("_env"):
                out[key] = {k: ("***" if looks_secret(k) else v) for k, v in value.items()}
            else:
                out[key] = _clean(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [_clean(v) if isinstance(v, dict) else v for v in value][:50]
        elif low in PATH_KEYS and isinstance(value, str):
            out[key] = mask_url(value)
        elif isinstance(value, str) and looks_secret(str(key)) and value:
            out[key] = "***"
        else:
            out[key] = value
    return out


def record(event: str, *, session: str = "", tool: str = "", backend: str = "",
           mode: str = "", status: str = "ok", tokens: int = 0, **extra) -> dict:
    """Append one entry. Never raises: the ledger must not break a feature."""
    row = {
        "ts": round(time.time(), 3),
        "event": str(event),
        "session": session or "",
        "tool": tool or "",
        "backend": backend or "",
        "mode": mode or "",
        "status": status or "ok",
        "tokens": int(tokens or 0),
    }
    row.update(_clean(extra))
    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        with open(config.AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return row


def recent(limit: int = 200, session: str = "", event: str = "") -> list[dict]:
    """The newest entries first."""
    try:
        raw = config.AUDIT_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []
    if session:
        raw = [l for l in raw if f'"session": "{session}"' in l]
    if event:
        raw = [l for l in raw if f'"event": "{event}"' in l]
    rows = []
    for line in raw[-max(1, min(int(limit or 200), LIMIT)):]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.reverse()
    return rows


def count() -> int:
    try:
        with open(config.AUDIT_FILE, "r", encoding="utf-8") as fh:
            return sum(1 for _ in fh)
    except Exception:  # noqa: BLE001
        return 0


def clear() -> dict:
    try:
        config.AUDIT_FILE.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def export(limit: int = LIMIT) -> str:
    rows = recent(limit)
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in reversed(rows))

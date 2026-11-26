"""Run manifests: one local JSON artifact per evaluated run.

A benchmark row is a claim, and a claim without its evidence is narration. The
manifest is the evidence: which build ran, under which configuration, what the
model was asked and what it answered, every tool call with its outcome and
duration, what changed on disk, what it cost in uncached tokens, and how the
run ended. One file per run under ``~/.tacit/manifests``, plain JSON, readable
and greppable like every other Tacit record.

The contract:

- Secrets never land in a manifest. Arguments and fields pass the audit
  ledger's masker, result excerpts are additionally redacted against the
  registry's known key values, and configuration and prompts are recorded as
  hashes, never as content.
- Interactive recording is opt-in (``TACIT_MANIFEST=on``); a headless
  benchmark run always records, because a comparison without manifests is
  exactly the unverifiable claim this module exists to prevent.
- The manifest consumes the same event stream the turn driver and the
  headless runner already read. It invents no second accounting path: usage
  goes through ``metrics.normalize_usage``, the one parser every consumer
  shares.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid

from . import audit, config, metrics
from .mcp_client import mask_url, redact

SCHEMA_VERSION = 1
RESULT_EXCERPT = 500
ARGS_EXCERPT = 300

# The build identity covers the loop and the control plane: a change to any of
# these can change what a run does, so a row produced by a different hash is a
# different build, whatever VERSION says.
_FINGERPRINT_FILES = (
    "backend/agent.py", "backend/ai/engine.py", "backend/ai/prompts.py",
    "backend/headless.py", "backend/manifest.py", "backend/session_runtime.py",
    "backend/turn_worker.py", "backend/sandbox.py", "backend/proctools.py",
)


def _sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()[:16]


def source_fingerprint() -> str:
    h = hashlib.sha256()
    for rel in _FINGERPRINT_FILES:
        try:
            h.update((config.ROOT / rel).read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:12]


def git_identity() -> dict:
    """Commit and branch from the .git files, dirty flag from a bounded local
    git call where one is available. Everything is optional: an installed
    checkout without .git records empty strings, never a guess."""
    out = {"commit": "", "branch": "", "dirty": None}
    git_dir = config.ROOT / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            out["branch"] = head[5:]
            ref = git_dir / head[5:]
            if ref.is_file():
                out["commit"] = ref.read_text(encoding="utf-8").strip()
        else:
            out["commit"] = head
    except OSError:
        pass
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=str(config.ROOT),
                           stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=10)
        if r.returncode == 0:
            out["dirty"] = bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        pass
    return out


def endpoint_class(base_url: str) -> str:
    """Which protocol family an endpoint speaks, without recording the URL's
    credentials: the class is what a comparison groups by."""
    url = str(base_url or "").lower()
    if "anthropic" in url:
        return "anthropic"
    if "openrouter" in url:
        return "openrouter"
    if "127.0.0.1" in url or "localhost" in url or "0.0.0.0" in url:
        return "local"
    return "openai-compatible"


def config_hash() -> str:
    """Hash of the masked model registry, capability configuration and prefs.
    Keys are masked BEFORE hashing, so the hash is stable across machines that
    differ only in credentials, and no key material is ever written."""
    from . import providers
    reg = audit._clean(config.registry() or {})
    caps = audit._clean(providers.load() or {})
    try:
        prefs = config.prefs()
    except Exception:  # noqa: BLE001
        prefs = {}
    return _sha(json.dumps({"registry": reg, "capabilities": caps, "prefs": prefs},
                           sort_keys=True))


def _known_secrets() -> list[str]:
    """The model registry's actual key values, for value-level redaction of
    free text. A key that appears inside a tool result is still a key."""
    out = []
    reg = config.registry() or {}
    for prov in (reg.get("providers") or {}).values():
        for field in ("apiKey", "api_key", "token"):
            value = str((prov or {}).get(field) or "")
            if len(value) >= 8:
                out.append(value)
    return out


def _clip(text, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…(truncated)"


class RunManifest:
    """Collects one run's evidence from its event stream, writes one JSON file.

    ``kind`` is "interactive" (opt-in) or "benchmark" (headless, always).
    The collector never raises into the turn: a manifest is evidence about the
    run, not a dependency of it.
    """

    def __init__(self, *, session: str, model: str, project: str = "",
                 kind: str = "interactive", profile: str = "", reasoning: str = "",
                 prompt_text: str = "", max_steps=None):
        self.id = uuid.uuid4().hex[:12]
        self._pending: dict = {}
        self._totals = {"input": 0, "output": 0, "total": 0,
                        "cache_read": 0, "cache_write": 0, "fresh": 0}
        self._secrets = _known_secrets()
        self._last_error: str | None = None
        self._saw_done = False
        self._cancelled = False
        row = config.resolve_model(model) or {}
        pid = str(model or "").split("/")[0]
        prov = ((config.registry() or {}).get("providers") or {}).get(pid) or {}
        base = str(prov.get("baseUrl") or row.get("baseUrl") or "")
        # The updater owns the one VERSION reader; a second reader would be a
        # second truth (tests/test_brand_version.py pins this).
        from .autoupdater import local_version
        version = local_version()
        self.data = {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "kind": kind,
            "session": str(session or ""),
            "started": round(time.time(), 3),
            "tacit_version": version,
            "source_fingerprint": source_fingerprint(),
            "git": git_identity(),
            "config_hash": config_hash(),
            "prompt": {"chars": len(prompt_text or ""), "hash": _sha(prompt_text)},
            "model": {
                "ref": str(model or ""),
                "provider": pid,
                "endpoint": mask_url(base),
                "endpoint_class": endpoint_class(base),
                "context_window": int(row.get("contextWindow") or 0),
                "reasoning": str(reasoning or ""),
                "profile": str(profile or ""),
                "max_steps": max_steps,
            },
            "project": str(project or ""),
            "model_calls": [],
            "tool_calls": [],
            "file_mutations": [],
            "compactions": [],
            "delegations": [],
            "steering": [],
            "policy": [],
            "terminal": None,
            "usage": self._totals,
        }

    # ── collection ──────────────────────────────────────────────────────────
    def event(self, ev: dict) -> None:
        try:
            self._event(ev or {})
        except Exception:  # noqa: BLE001  evidence must not break the run
            pass

    def _event(self, ev: dict) -> None:
        kind = ev.get("type")
        now = round(time.time(), 3)
        if kind == "usage":
            u = metrics.normalize_usage(ev.get("usage") or {})
            for key, value in u.items():
                self._totals[key] += value
            self.data["model_calls"].append({
                "ts": now, "usage": u, "subagent": bool(ev.get("subagent")),
            })
        elif kind == "tool_start":
            args = ev.get("args") or {}
            self._pending[ev.get("id")] = {
                "name": ev.get("name"),
                "args_hash": _sha(json.dumps(args, sort_keys=True, ensure_ascii=False)),
                "args": audit._clean(args),
                "started": now,
            }
        elif kind == "tool_end":
            start = self._pending.pop(ev.get("id"), {})
            args = start.get("args") or {}
            for key in list(args):
                if isinstance(args[key], str):
                    args[key] = _clip(redact(args[key], self._secrets), ARGS_EXCERPT)
            entry = {
                "id": ev.get("id"),
                "name": ev.get("name") or start.get("name"),
                "args_hash": start.get("args_hash"),
                "args": args,
                "started": start.get("started"),
                "elapsed_ms": int(ev.get("elapsed_ms") or 0),
                "is_error": bool(ev.get("is_error")),
                "result": _clip(redact(ev.get("result"), self._secrets), RESULT_EXCERPT),
            }
            if ev.get("backend"):
                entry["backend"] = ev.get("backend")
                self.data["policy"].append({
                    "ts": now, "tool": entry["name"], "backend": ev.get("backend"),
                    "changed": int((ev.get("changed") or {}).get("count") or 0),
                })
            if ev.get("mutation"):
                mut = dict(ev.get("mutation"))
                mut["tool"] = entry["name"]
                mut["ts"] = now
                self.data["file_mutations"].append(mut)
            self.data["tool_calls"].append(entry)
        elif kind == "compaction":
            self.data["compactions"].append({
                "ts": now, "saved": int(ev.get("saved") or 0),
                "chars_before": int(ev.get("chars_before") or 0),
                "chars_after": int(ev.get("chars_after") or 0),
            })
        elif kind == "delegation":
            self.data["delegations"].append({
                "ts": now, "spent": int(ev.get("spent") or 0),
                "saved": int(ev.get("saved") or 0),
            })
        elif kind == "done":
            self._saw_done = True
        elif kind == "error":
            self._last_error = str(ev.get("message") or "unknown error")

    def note_steering(self, text: str) -> None:
        self.data["steering"].append({
            "ts": round(time.time(), 3),
            "text": _clip(redact(text, self._secrets), ARGS_EXCERPT),
        })

    def note_cancel(self) -> None:
        self._cancelled = True

    # ── completion ──────────────────────────────────────────────────────────
    def finish(self, *, ok: bool | None = None, error: str | None = None,
               wall_s: float | None = None, status: str | None = None) -> dict:
        """Assemble, mask, write. Returns a small summary for the caller's row.

        `status` is the P2.1 terminal outcome (completed / cancelled /
        blocked / budget_exceeded / infrastructure_failed); when the caller
        passes none, the old ok/error derivation applies."""
        error = error if error is not None else self._last_error
        if status in ("completed", "completed_unverified", "cancelled",
                      "blocked", "budget_exceeded", "infrastructure_failed"):
            pass
        elif self._cancelled:
            status = "cancelled"
        elif ok is False or error:
            status = "error"
        elif ok is True or self._saw_done:
            status = "completed"
        else:
            status = "unknown"
        self.data["terminal"] = {
            "status": status,
            "error": _clip(redact(error, self._secrets), RESULT_EXCERPT) if error else None,
            "saw_done": self._saw_done,
            "cancelled": self._cancelled,
        }
        self.data["wall_s"] = round(wall_s if wall_s is not None
                                    else time.time() - self.data["started"], 3)
        self.data["finished"] = round(time.time(), 3)
        self.data["usage"] = dict(self._totals)
        masked = audit._clean(self.data)
        path = config.MANIFESTS_DIR / f"{self.id}.json"
        try:
            config.MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
            config.write_json(path, masked)
        except Exception:  # noqa: BLE001
            path = None
        return {"id": self.id, "path": str(path) if path else "",
                "status": status, "usage": dict(self._totals)}


def load(manifest_id: str) -> dict | None:
    """Read one manifest back, for checkers and reports."""
    key = str(manifest_id or "").strip()
    if not key or not all(c in "0123456789abcdef" for c in key):
        return None
    return config.read_json(config.MANIFESTS_DIR / f"{key}.json", None)
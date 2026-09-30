import json
import time
import uuid
from pathlib import Path

from . import config


def _rows() -> list[dict]:
    path = config.EVIDENCE_FILE
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        return []
    return out


def add(source: str, note: str, snippet: str) -> str:
    src = str(source or "").strip()
    if not src:
        return "ERROR: source required"
    rec = {"id": uuid.uuid4().hex[:8], "source": src,
           "note": (str(note or "").strip() or src)[:300],
           "snippet": str(snippet or "")[:1000], "ts": round(time.time(), 3)}
    try:
        config.EVIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with config.EVIDENCE_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        return f"ERROR: {e}"
    return f"recorded {rec['id']}: {rec['note']}"


def get(eid: str) -> str:
    for r in _rows():
        if r.get("id") == str(eid or "").strip():
            return (f"[{r['id']}] {r['note']}\nsource: {r['source']}\n---\n{r['snippet']}")
    return f"ERROR: no evidence '{eid}'"


def listing(limit: int = 40) -> str:
    rows = _rows()
    if not rows:
        return "no evidence recorded yet"
    tail = rows[-limit:]
    return "\n".join(f"[{r['id']}] {r['note']}\n     {r['source'][:110]}" for r in tail)

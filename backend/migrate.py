"""Manual, one-time import from another tool's export.

Tacit does not scan for other tools, does not read their directories, and does not
stay connected to them afterwards. This module does one thing: given a file or a
folder **you point at**, read it, show you what it found, and add it to Tacit's own
memory if you say so.

The shape of a migration is deliberately narrow:

  - you choose the path; nothing is detected
  - a preview runs first and writes nothing
  - anything that looks like a credential is refused, item by item
  - the import is additive and idempotent, and never overwrites what you have
  - when it is done, the relationship ends. Nothing here reads the source again.

Two text shapes are understood, because those are what exports tend to look like:

  - prose separated by a line containing a section sign
  - markdown headings, bullets and short paragraphs

After import the data belongs to Tacit. Export produces portable text, and placing
it anywhere is your decision.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import audit, memory_store, tokens

MAX_ITEM = 2000
MAX_FILES = 200
TEXT_SUFFIXES = (".md", ".txt", ".markdown", ".json")

# Deliberately broad. A false positive costs one skipped line; a false negative
# puts a key into a prompt and into a database.
_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{8,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|Bearer\s+[A-Za-z0-9._\-]{12,}"
    r"|(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*\S{8,})",
    re.I)


def looks_like_a_secret(text: str) -> bool:
    return bool(_SECRET.search(str(text or "")))


def parse(text: str) -> list[str]:
    """Prose separated by a section sign, or markdown structure."""
    body = str(text or "")
    if "§" in body:
        return _by_section(body)
    return _by_markdown(body)


def _by_section(body: str) -> list[str]:
    blocks, current = [], []
    for raw in body.splitlines():
        if raw.strip().startswith("§"):
            rest = raw.strip().lstrip("§").strip()
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            if rest:
                current.append(rest)
            continue
        current.append(raw)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b][:500]


def _by_markdown(body: str) -> list[str]:
    """One item per bullet or heading, with its paragraph if it has one."""
    items, pending = [], []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if pending:
                items.append("\n".join(pending).strip())
                pending = []
            continue
        if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+\S", line):
            if pending:
                items.append("\n".join(pending).strip())
            pending = [re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)]
            continue
        if line.lstrip().startswith("#"):
            if pending:
                items.append("\n".join(pending).strip())
            pending = [line.lstrip("# ").strip()]
            continue
        if pending:
            items.append("\n".join(pending).strip())
            pending = []
        pending = [line.strip()]
    if pending:
        items.append("\n".join(pending).strip())
    return [i for i in items if i][:500]


def _targets(path: str) -> list[Path]:
    """Only a path the user actually named. An empty path means nothing.

    Treating "" as the current directory would turn a mis-click into a scan of
    whatever the server happened to be running in, which is the opposite of the
    point.
    """
    raw = str(path or "").strip()
    if not raw:
        return []
    p = Path(raw).expanduser()
    if p.is_file():
        return [p]
    if p.is_dir():
        found = [f for f in sorted(p.rglob("*"))
                 if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES]
        return found[:MAX_FILES]
    return []


def preview(path: str) -> dict:
    """What an import would do. Writes nothing, reads only what you named."""
    files = _targets(path)
    if not files:
        return {"ok": False, "error": "point at a file or folder to import from",
                "items": [], "refused": [], "tokens": 0, "files": []}

    items, refused, seen = [], [], []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            seen.append({"file": str(file), "error": str(exc), "items": 0})
            continue
        count = 0
        for index, block in enumerate(parse(text)):
            body = block[:MAX_ITEM]
            if looks_like_a_secret(body):
                refused.append({"file": str(file), "index": index,
                                "preview": body[:80], "why": "looks like a credential"})
                continue
            items.append({"file": str(file), "index": index, "text": body,
                          "tokens": tokens.estimate_tokens(body)})
            count += 1
        seen.append({"file": str(file), "error": "", "items": count,
                     "bytes": len(text)})

    total = sum(i["tokens"] for i in items)
    return {"ok": True, "path": str(path), "files": seen, "items": items,
            "count": len(items), "refused": refused, "refused_count": len(refused),
            "tokens": total, "display": tokens.label(total), "exact": tokens.exact()}


def import_items(path: str, session: str = "", kind: str = "project_fact") -> dict:
    """Add what you pointed at. Additive, idempotent, and it ends there."""
    plan = preview(path)
    if not plan.get("ok"):
        return {"ok": False, "error": plan.get("error") or "nothing to import"}

    existing = {str(r.get("content") or "").strip()
                for r in memory_store.list_memories(limit=4000)}
    added, skipped = [], []
    for item in plan["items"]:
        body = item["text"].strip()
        if body in existing:
            skipped.append({"text": body[:80], "why": "already here"})
            continue
        res = memory_store.add(
            body, type=kind if kind in memory_store.TYPES else "project_fact",
            scope="global", source="user", confidence="medium",
            source_session="migrated",
            reason=f"imported from {Path(item['file']).name}")
        if res.get("ok"):
            existing.add(body)
            added.append(res["memory"]["id"])
        else:
            skipped.append({"text": body[:80], "why": res.get("error") or "refused"})

    audit.record("migration_import", session=session, backend="manual", mode="one-time",
                 status="ok", added=len(added), skipped=len(skipped),
                 refused=plan["refused_count"], source=Path(str(path)).name)
    return {"ok": True, "added": added, "added_count": len(added),
            "skipped": skipped[:50], "skipped_count": len(skipped),
            "refused": plan["refused"], "refused_count": plan["refused_count"],
            "tokens": plan["tokens"], "note": "this import is finished. Nothing stays connected."}


def export_text(style: str = "prose") -> str:
    """Tacit memory as portable text. Writing it anywhere is your decision."""
    rows = [r for r in memory_store.list_memories(enabled=True, limit=4000)
            if (r.get("content") or "").strip()]
    if not rows:
        return ""
    if style == "markdown":
        return "\n\n".join(f"- {r['content'].strip()}" for r in rows)
    return "\n§\n".join(r["content"].strip() for r in rows)


def export_skills() -> dict:
    """Tacit skills as name, description and body, ready to be written out."""
    from . import skills

    return {"ok": True, "skills": [
        {"name": s["name"], "description": s["description"], "body": s["body"]}
        for s in skills.load()]}


def sources_hint() -> str:
    """Shown in the interface. Says plainly that nothing is detected."""
    return ("Point at a file or folder you exported yourself. Tacit does not scan "
            "for other tools and does not read their directories.")

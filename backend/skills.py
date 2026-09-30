import re
from pathlib import Path

from . import config

FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


def _split(text: str):
    m = FRONT.match(text or "")
    if not m:
        return {}, (text or "").strip()
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    return meta, (m.group(2) or "").strip()


def load() -> list[dict]:
    rows = []
    for root, kind in ((config.SKILLS_DIR, "skill"), (config.KNOWLEDGE_DIR, "knowledge")):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                meta, body = _split(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            first = next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
            rows.append({
                "name": meta.get("name") or path.stem,
                "description": (meta.get("description") or first)[:200],
                "path": str(path),
                "body": body,
                "kind": kind,
                "tokens": max(1, len(body) // 4),
            })
    return rows


def index(limit: int | None = None) -> str:
    rows = load()
    if not rows:
        return ""
    limit = limit or config.SKILL_INDEX_LIMIT
    lines = [f"- {s['name']}: {s['description']}" for s in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"- ({len(rows) - limit} more not listed)")
    return ("Skills available on demand. When one looks relevant to the task, call skill(name) to "
            "read it before acting — do not guess at what a skill says.\n" + "\n".join(lines))


def read(name: str) -> str:
    wanted = str(name or "").strip().lower()
    for s in load():
        if s["name"].lower() == wanted:
            return s["body"] or "(this skill is empty)"
    names = ", ".join(s["name"] for s in load()) or "none"
    return f"ERROR: no skill named '{name}'. Available: {names}"


def create(name: str, text: str, description: str = "") -> dict:
    clean = str(name or "").strip()
    if not clean:
        return {"ok": False, "error": "name required"}
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", clean).strip("-") or "skill"
    path = config.SKILLS_DIR / f"{safe}.md"
    if path.exists():
        return {"ok": False, "error": f"'{safe}' already exists"}
    body = str(text or "").strip()
    desc = description.strip() or next((ln.lstrip("# ").strip() for ln in body.splitlines()
                                        if ln.strip()), clean)
    config.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {clean}\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8")
    return {"ok": True, "name": clean, "path": str(path)}


def remove(name: str) -> dict:
    wanted = str(name or "").strip().lower()
    for s in load():
        if s["name"].lower() == wanted:
            try:
                Path(s["path"]).unlink()
            except Exception as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "name": s["name"]}
    return {"ok": False, "error": f"no skill named '{name}'"}

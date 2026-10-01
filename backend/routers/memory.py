"""HTTP surface for the Memory Vault."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config, memory_store, store, tokens
from ..ai import engine

router = APIRouter()


def _ok(**kw):
    return {"ok": True, **kw}


def _fail(error: str):
    return JSONResponse({"ok": False, "error": error})


@router.get("/api/memory")
async def list_memories(type: str = "", scope: str = "", project: str = "",
                        enabled: str = "", pinned: str = "", search: str = "",
                        limit: int = 500):
    def flag(value: str):
        return None if value == "" else value.lower() in ("1", "true", "yes")

    rows = memory_store.list_memories(type=type, scope=scope, project=project,
                                      enabled=flag(enabled), pinned=flag(pinned),
                                      search=search, limit=limit)
    return _ok(memories=rows, stats=memory_store.stats(project))


@router.post("/api/memory")
async def add_memory(request: Request):
    body = await request.json()
    res = memory_store.add(
        body.get("content") or "", type=body.get("type") or "other",
        scope=body.get("scope") or "global", project=body.get("project") or "",
        source=body.get("source") or "user", confidence=body.get("confidence") or "medium",
        pinned=bool(body.get("pinned")), enabled=body.get("enabled", True) is not False)
    return res if res.get("ok") else _fail(res.get("error") or "could not add memory")


@router.patch("/api/memory/{memory_id}")
async def update_memory(memory_id: int, request: Request):
    body = await request.json()
    res = memory_store.update(memory_id, **body)
    return res if res.get("ok") else _fail(res.get("error") or "could not update memory")


@router.delete("/api/memory/{memory_id}")
async def delete_memory(memory_id: int):
    res = memory_store.delete(memory_id)
    return res if res.get("ok") else _fail("no such memory")


@router.post("/api/memory/recall")
async def recall(request: Request):
    body = await request.json()
    hits = memory_store.recall(body.get("query") or "", limit=body.get("limit") or 5,
                               scope=body.get("scope") or "", project=body.get("project") or "")
    return _ok(memories=hits)


@router.get("/api/memory/stats")
async def stats(project: str = ""):
    return _ok(stats=memory_store.stats(project), budget=memory_store.budget())


@router.get("/api/memory/startup")
async def startup(project: str = ""):
    selection = memory_store.startup_selection(project)
    selection["display"] = tokens.label(selection["tokens"])
    return _ok(**selection)


@router.post("/api/memory/budget")
async def set_budget(request: Request):
    body = await request.json()
    res = memory_store.set_budget(body.get("budget") or 0,
                                  override=bool(body.get("override")))
    return res if res.get("ok") else _fail(res.get("error") or "could not set budget")


_SUMMARY_PROMPT = (
    "Summarise this work session so it can be recalled in a later one. Write it for "
    "someone who was not here. Keep it short and factual, and use these four lines:\n"
    "Goal: what was asked\n"
    "Did: what actually changed\n"
    "Decided: choices made and why\n"
    "Open: anything unfinished\n\n"
)


@router.post("/api/memory/summarise")
async def summarise(request: Request):
    """Distil a session into one durable note. Nothing is saved unless asked.

    This is the long-term half of memory: individual facts are extracted from a
    conversation, but a session summary is what makes the session itself
    recallable months later.
    """
    body = await request.json()
    rec = store.get(str(body.get("sid") or ""))
    if not rec:
        return _fail("no such session")
    msgs = rec.get("messages") or []
    transcript = "\n".join(
        f"{m.get('role')}: {(m.get('content') or '').strip()}"
        for m in msgs if (m.get("content") or "").strip())
    if not transcript.strip():
        return _fail("that session has nothing to summarise")

    existing = str(body.get("text") or "").strip()
    if not existing:
        try:
            existing = (engine.chat(
                [{"role": "user", "content": _SUMMARY_PROMPT + transcript[:24000]}],
                ref=body.get("model") or None) or "").strip()
        except engine.EngineError as exc:
            return _fail(f"summarising needs a working model: {exc}")
        if not existing:
            return _fail("the model returned nothing")

    cost = tokens.estimate_tokens(existing)
    result = {"summary": existing, "tokens": cost, "display": tokens.label(cost),
              "sid": rec["id"], "saved": False,
              "title": rec.get("title") or "session"}
    if not body.get("save"):
        return _ok(**result)

    ttl = int(body.get("ttl_days") or 0)
    saved = memory_store.add(
        existing, type=body.get("type") or "lesson", scope="global",
        source="user", confidence=body.get("confidence") or "medium",
        pinned=bool(body.get("pinned")), source_session=f"session:{rec['id']}",
        ttl_days=ttl, reason=f"session summary of {rec.get('title') or rec['id']}")
    if not saved.get("ok"):
        return _fail(saved.get("error") or "could not save the summary")
    result["saved"] = True
    result["memory"] = saved["memory"]
    return _ok(**result)


@router.get("/api/memory/compressions")
async def compressions(limit: int = 50):
    return _ok(compressions=memory_store.compressions(limit))


_COMPRESS_PROMPT = (
    "Rewrite these memory notes as a single compact block. Keep every distinct fact, "
    "drop pleasantries and repetition, prefer short noun phrases. Reply with the "
    "rewritten text only.\n\n"
)


@router.post("/api/memory/compress")
async def compress(request: Request):
    body = await request.json()
    project = body.get("project") or ""
    ids = body.get("ids")
    if ids:
        rows = [memory_store.get(int(i)) for i in ids]
        rows = [r for r in rows if r]
    else:
        selection = memory_store.startup_selection(project)
        rows = [memory_store.get(int(i)) for i in selection["used"]]
        rows = [r for r in rows if r]
    if not rows:
        return _fail("nothing selected to compress")
    before = "\n".join(f"- [{r['type']}] {r['content']}" for r in rows)
    try:
        after = (engine.chat([{"role": "user", "content": _COMPRESS_PROMPT + before}],
                             ref=body.get("model") or None) or "").strip()
    except engine.EngineError as exc:
        return _fail(f"compression needs a working model: {exc}")
    if not after:
        return _fail("the model returned nothing")
    counts = memory_store.record_compression(before, after, approved=bool(body.get("apply")))
    result = {"before": before, "after": after, "ids": [r["id"] for r in rows], **counts,
              "before_display": tokens.label(counts["before_tokens"]),
              "after_display": tokens.label(counts["after_tokens"]),
              "saved_display": tokens.label(counts["saved"])}
    if body.get("apply"):
        created = memory_store.add(after, type="other", scope="global", project=project,
                                   source="session_extract", confidence="high", pinned=True)
        if created.get("ok"):
            for row in rows:
                memory_store.update(row["id"], enabled=False)
            result["created"] = created["memory"]["id"]
            result["disabled"] = [r["id"] for r in rows]
    return _ok(**result)


_EXTRACT_PROMPT = (
    "Read the conversation and propose durable memories worth keeping for future "
    "sessions. Return ONLY a JSON array; each item is "
    '{"content": str, "type": one of '
    "[preference, project_fact, decision, lesson, pattern, contact, other], "
    '"confidence": one of [low, medium, high], "snippet": str}. \n'
    "Propose at most {limit} items and prefer durable facts over transient detail.\n\n"
)


@router.post("/api/memory/extract")
async def extract(request: Request):
    """Propose candidate memories from a session. Nothing is saved until approved."""
    body = await request.json()
    rec = store.get(str(body.get("sid") or ""))
    if not rec:
        return _fail("no such session")
    limit = max(1, min(int(body.get("limit") or 6), 20))
    transcript = "\n".join(
        f"{m.get('role')}: {(m.get('content') or '')[:1500]}"
        for m in (rec.get("messages") or [])[-40:] if (m.get("content") or "").strip())
    if not transcript:
        return _fail("that session has nothing to read")
    prompt = _EXTRACT_PROMPT.replace("{limit}", str(limit)) + transcript
    try:
        raw = engine.chat([{"role": "user", "content": prompt}],
                          ref=body.get("model") or None) or ""
    except engine.EngineError as exc:
        return _fail(f"extraction needs a working model: {exc}")
    candidates = _parse_candidates(raw)
    for item in candidates:
        item["token_estimate"] = tokens.estimate_tokens(item.get("content") or "")
        item["token_display"] = tokens.label(item["token_estimate"])
    return _ok(candidates=candidates, raw=raw[:2000], model=body.get("model") or rec.get("model"))


def _parse_candidates(raw: str) -> list[dict]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        rows = __import__("json").loads(text)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not str(row.get("content") or "").strip():
            continue
        out.append({
            "content": str(row["content"]).strip(),
            "type": row.get("type") if row.get("type") in memory_store.TYPES else "other",
            "confidence": (row.get("confidence")
                           if row.get("confidence") in memory_store.CONFIDENCE else "medium"),
            "snippet": str(row.get("snippet") or "")[:400],
        })
    return out


@router.get("/api/memory/summary")
async def summary(project: str = ""):
    """One call for the dashboard: what memory costs at startup."""
    selection = memory_store.startup_selection(project)
    return _ok(stats=memory_store.stats(project),
               startup_tokens=selection["tokens"],
               startup_display=tokens.label(selection["tokens"]),
               budget=selection["budget"],
               text=selection["text"],
               dir=str(config.MEMORY_DB))

import html
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx

from . import config

BG: dict[str, dict] = {}
TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
BLOCK_RE = re.compile(r"</(p|div|li|h[1-6]|tr|section|article)>", re.I)
ANY_TAG = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t]+")


def _new_id() -> str:
    return uuid.uuid4().hex[:6]


def _drain(proc, buf: list, cap: int):
    def run(stream):
        try:
            for line in iter(stream.readline, ""):
                buf.append(line)
                if len(buf) > 4000:
                    del buf[:1000]
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            threading.Thread(target=run, args=(stream,), daemon=True).start()


def bg_start(command: str, project: str | None = None, cwd: str | None = None) -> str:
    cmd = str(command or "").strip()
    if not cmd:
        return "ERROR: empty command"
    work = cwd or project or str(config.USER_HOME)
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=work, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                bufsize=1)
    except Exception as e:
        return f"ERROR: {e}"
    bid = _new_id()
    out: list[str] = []
    BG[bid] = {"proc": proc, "cmd": cmd, "cwd": work, "out": out, "started": time.time()}
    _drain(proc, out, 4000)
    return f"started [{bid}] in {work}: {cmd}\nread it with bg_output(id='{bid}')"


def bg_output(bid: str, tail: int = 4000) -> str:
    rec = BG.get(str(bid or "").strip())
    if not rec:
        names = ", ".join(BG) or "none"
        return f"ERROR: no background job '{bid}'. Running: {names}"
    body = "".join(rec["out"])
    if len(body) > tail:
        body = "...(truncated)\n" + body[-tail:]
    code = rec["proc"].poll()
    state = "running" if code is None else f"exited {code}"
    return f"[{bid}] {state} | {round(time.time() - rec['started'], 1)}s\n{body.strip() or '(no output yet)'}"


def bg_stop(bid: str) -> str:
    key = str(bid or "").strip()
    rec = BG.get(key)
    if not rec:
        return f"ERROR: no background job '{key}'"
    if rec["proc"].poll() is None:
        try:
            rec["proc"].terminate()
        except Exception:
            pass
    BG.pop(key, None)
    return f"stopped [{key}]"


def fetch(url: str, max_chars: int | None = None) -> str:
    target = str(url or "").strip()
    if not re.match(r"^https?://", target, re.I):
        return "ERROR: url must start with http:// or https://"
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, read=40.0), follow_redirects=True) as client:
            r = client.get(target, headers={"User-Agent": "Tacit"})
    except httpx.HTTPError as e:
        return f"ERROR: {e}"
    if r.status_code >= 400:
        return f"ERROR: HTTP {r.status_code}"
    ctype = r.headers.get("content-type", "")
    body = r.text
    if "html" in ctype.lower():
        body = TAG_RE.sub(" ", body)
        body = BLOCK_RE.sub("\n", body)
        body = ANY_TAG.sub(" ", body)
        body = html.unescape(body)
        body = "\n".join(WS_RE.sub(" ", ln).strip() for ln in body.splitlines())
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
    limit = max_chars or config.FETCH_LIMIT
    head = f"{target} -> {r.status_code}, {len(body)} chars\n"
    return head + (body[:limit] + f"\n...(truncated, {len(body)} chars total)" if len(body) > limit else body)


def snapshot(root: str, label: str = "") -> str:
    src = Path(str(root or "")).expanduser()
    if not src.is_dir():
        return f"ERROR: {root} is not a directory"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = config.CHECKPOINT_DIR / f"{stamp}-{_new_id()}"
    count = 0
    try:
        for path in src.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel_parts = path.relative_to(src).parts
            except ValueError:
                continue
            # Only the path *inside* the project counts. Testing absolute parts
            # would skip everything whenever the project itself sat in a folder
            # named build/ dist/ checkpoints/ …
            if any(part in config.SNAPSHOT_SKIP for part in rel_parts):
                continue
            if path.stat().st_size > config.SNAPSHOT_MAX_FILE:
                continue
            rel = path.relative_to(src)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(path.read_bytes())
            count += 1
            if count >= config.SNAPSHOT_MAX_FILES:
                break
    except Exception as e:
        return f"ERROR: {e}"
    if label:
        try:
            (dest / ".label").write_text(label, encoding="utf-8")
        except Exception:
            pass
    return f"snapshot {dest.name}: {count} file(s)" + (f" - {label}" if label else "")


def list_snapshots() -> str:
    root = config.CHECKPOINT_DIR
    if not root.is_dir():
        return "no snapshots yet"
    rows = sorted([p for p in root.iterdir() if p.is_dir()], reverse=True)[:20]
    if not rows:
        return "no snapshots yet"
    out = []
    for p in rows:
        label = ""
        try:
            label = (p / ".label").read_text(encoding="utf-8").strip()
        except Exception:
            pass
        n = sum(1 for f in p.rglob("*") if f.is_file())
        out.append(f"{p.name}  {n} file(s)" + (f"  - {label}" if label else ""))
    return "\n".join(out)


def snapshot_index(limit: int = 200) -> list[dict]:
    """Structured snapshots for the UI timeline (the string helpers below stay
    for the agent tools).

    Each row carries what the timeline needs without re-stat'ing the tree: the
    id, when it was taken, its label, and the files it holds.
    """
    root = config.CHECKPOINT_DIR
    if not root.is_dir():
        return []
    dirs = sorted([p for p in root.iterdir() if p.is_dir()], reverse=True)
    rows = []
    for path in dirs[:max(1, min(int(limit or 200), 1000))]:
        label = ""
        try:
            label = (path / ".label").read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            label = ""
        files, total = [], 0
        for f in path.rglob("*"):
            if f.is_file() and f.name != ".label":
                files.append(str(f.relative_to(path)))
                try:
                    total += f.stat().st_size
                except Exception:  # noqa: BLE001
                    pass
        created = ""
        m = re.match(r"^(\d{8})-(\d{6})-", path.name)
        if m:
            d, t = m.group(1), m.group(2)
            created = (f"{d[0:4]}-{d[4:6]}-{d[6:8]} "
                       f"{t[0:2]}:{t[2:4]}:{t[4:6]}")
        rows.append({
            "name": path.name,
            "label": label,
            "created": created,
            "file_count": len(files),
            "files": sorted(files)[:200],
            "truncated": len(files) > 200,
            "bytes": total,
        })
    return rows


def snapshot_detail(name: str) -> dict | None:
    return next((s for s in snapshot_index(1000) if s["name"] == str(name or "").strip()), None)


def snapshot_compare(name: str, root: str, limit: int = 500) -> dict:
    """Compare a snapshot with the project as it stands now.

    Uses size-then-content so a cheap comparison answers most of the time and a
    full read only happens on a size match. Capped so a huge tree cannot stall
    the UI.
    """
    src = config.CHECKPOINT_DIR / str(name or "").strip()
    if not src.is_dir():
        return {"ok": False, "error": f"no snapshot '{name}'"}
    dest = Path(str(root or "")).expanduser()
    if not dest.is_dir():
        return {"ok": False, "error": f"{root} is not a directory"}

    def collect(base: Path) -> dict:
        out = {}
        for path in base.rglob("*"):
            if not path.is_file() or path.name == ".label":
                continue
            try:
                rel = path.relative_to(base)
            except ValueError:
                continue
            # relative parts only — see the note in snapshot()
            if any(part in config.SNAPSHOT_SKIP for part in rel.parts):
                continue
            rel_str = str(rel)
            try:
                out[rel_str] = path.stat().st_size
            except OSError:
                continue
            if len(out) >= limit:
                break
        return out

    old, new = collect(src), collect(dest)
    changed, added, removed = [], [], []
    for rel, size in old.items():
        if rel not in new:
            removed.append(rel)
        elif new[rel] != size:
            changed.append(rel)
        else:
            try:
                if (src / rel).read_bytes() != (dest / rel).read_bytes():
                    changed.append(rel)
            except OSError:
                changed.append(rel)
    for rel in new:
        if rel not in old:
            added.append(rel)
    return {
        "ok": True,
        "name": src.name,
        "project": str(dest),
        "changed": sorted(changed),
        "added": sorted(added),
        "removed": sorted(removed),
        "count": len(changed) + len(added) + len(removed),
        "truncated": len(old) >= limit or len(new) >= limit,
    }


def restore(name: str, root: str) -> str:
    src = config.CHECKPOINT_DIR / str(name or "").strip()
    if not src.is_dir():
        return f"ERROR: no snapshot '{name}'"
    dest = Path(str(root or "")).expanduser()
    if not dest.is_dir():
        return f"ERROR: {root} is not a directory"
    restored = 0
    for path in src.rglob("*"):
        if not path.is_file() or path.name == ".label":
            continue
        rel = path.relative_to(src)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(path.read_bytes())
        restored += 1
    return f"restored {restored} file(s) from {src.name} into {dest}"

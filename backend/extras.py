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
            if any(part in config.SNAPSHOT_SKIP for part in path.parts):
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

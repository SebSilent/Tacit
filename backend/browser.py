import json
import os
import subprocess
import threading
from pathlib import Path

from . import config


class Browser:
    def __init__(self):
        self._proc = None
        self._seq = 0
        self._lock = threading.Lock()
        self._pending: dict[int, dict] = {}

    def _start(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        driver = Path(__file__).with_name("browser_driver.mjs")
        if not driver.exists():
            return False
        pkg = Path(config.PLAYWRIGHT_PATH or "")
        if not (pkg / "package.json").exists():
            return False
        env = {**os.environ, "TACIT_PLAYWRIGHT_PATH": config.PLAYWRIGHT_PATH}
        try:
            self._proc = subprocess.Popen(
                ["node", str(driver)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env)
        except Exception:
            self._proc = None
            return False
        threading.Thread(target=self._reader, daemon=True).start()
        return True

    def _reader(self):
        try:
            for line in self._proc.stdout:
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                slot = self._pending.get(msg.get("id"))
                if slot is not None:
                    slot["result"] = msg
                    slot["done"].set()
        except Exception:
            pass

    def call(self, action: str, timeout: int | None = None, **kw) -> dict:
        with self._lock:
            if not self._start():
                return {"ok": False, "error": (
                    "browser automation is not set up. Install one of these:\n"
                    "  npm install                      (uses this repo's playwright)\n"
                    "  pip install playwright && playwright install chromium\n"
                    "or point TACIT_PLAYWRIGHT_PATH at an existing playwright install.")}
            self._seq += 1
            cid = self._seq
            slot = {"result": None, "done": threading.Event()}
            self._pending[cid] = slot
            try:
                payload = json.dumps({"id": cid, "action": action, **kw}) + "\n"
                self._proc.stdin.write(payload.encode("utf-8"))
                self._proc.stdin.flush()
            except Exception as e:
                self._pending.pop(cid, None)
                self._proc = None
                return {"ok": False, "error": f"browser driver died: {e}"}
            slot["done"].wait(timeout or config.BROWSER_TIMEOUT)
            self._pending.pop(cid, None)
            out = slot["result"]
            if out is None:
                try:
                    self._proc.kill()
                except Exception:
                    pass
                self._proc = None
                return {"ok": False, "error": f"browser timed out after "
                                              f"{timeout or config.BROWSER_TIMEOUT}s"}
            return out


BROWSER = Browser()


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f"\n...(truncated, {len(text)} chars)"


def open_page(url: str, limit: int | None = None) -> str:
    if not str(url or "").strip():
        return "ERROR: url required"
    r = BROWSER.call("open", url=str(url).strip())
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    head = f"{r.get('title') or '(no title)'} - {r.get('url')} [{r.get('status')}]"
    body = _clip(r.get("text") or "", limit or config.BROWSER_TEXT_LIMIT)
    return f"{head}\n\n{body}" if body.strip() else head


def page_text(selector: str = "", limit: int | None = None) -> str:
    r = BROWSER.call("text", selector=selector or None)
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return _clip(r.get("text") or "(empty)", limit or config.BROWSER_TEXT_LIMIT)


def page_links(limit: int = 40) -> str:
    r = BROWSER.call("links", limit=limit)
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    rows = r.get("links") or []
    if not rows:
        return "(no links)"
    return "\n".join(f"- {x['text'][:80]} -> {x['href']}" for x in rows)


def click(selector: str) -> str:
    r = BROWSER.call("click", selector=str(selector or "").strip())
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return f"clicked {selector} -> {r.get('title') or ''} {r.get('url') or ''}".strip()


def type_into(selector: str, text: str, enter: bool = True) -> str:
    r = BROWSER.call("type", selector=str(selector or "").strip(), text=str(text or ""),
                     enter=bool(enter))
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return f"typed into {selector}" + (" and pressed Enter" if enter else "")


def screenshot(path: str, full: bool = False) -> str:
    target = str(path or "").strip()
    if not target:
        return "ERROR: path required"
    r = BROWSER.call("screenshot", path=target, full=bool(full))
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return f"saved screenshot to {r.get('saved')}"


def close() -> str:
    r = BROWSER.call("close")
    return "browser closed" if r.get("ok") else f"ERROR: {r.get('error')}"

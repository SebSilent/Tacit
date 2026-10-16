"""Time what a page load actually costs, and find out whether the event loop blocks.

Two separate questions, measured separately:

1. How long does each boot request take on its own?
2. Does a slow request delay an unrelated one? If it does, the loop is blocked -
   a subprocess or a sleep running inside an `async def`, which stops the
   WebSocket handshake and every other request at the same time. That is what
   "connecting..." for ages actually is.

    python scripts/measure_load.py

Needs a server already running on the configured port, and only ever issues
GETs. Kept in the repo beside `measure_registry.py`, which it is the same genre
of: measure, don't assert, run by hand when a page feels slow.
"""
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config

BASE = f"http://127.0.0.1:{config.PORT}"

# `?v=NN` is not a constant - index.html carries the live one, and a stale literal here
# would time a URL no browser ever asks for. Read it off the page instead.
_PAGE = (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")


def asset(name):
    m = re.search(re.escape(name) + r"\?v=(\d+)", _PAGE)
    return "/static/%s?v=%s" % (name, m.group(1)) if m else "/static/%s" % name


def get(path, timeout=30):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            body = r.read()
            return path, (time.perf_counter() - t0) * 1000, r.status, len(body)
    except Exception as exc:
        return path, (time.perf_counter() - t0) * 1000, f"ERR {exc.__class__.__name__}", 0


print("== each on its own ==")
paths = ["/", asset("app.js"), asset("style.css"), "/api/info",
         "/api/sessions/registry", "/api/git/status", "/api/git/log?limit=40",
         "/api/git/branches", "/api/git/identity", "/api/capabilities", "/api/deps",
         "/api/tasks/state?sid=nope", "/health"]
for p in paths:
    path, ms, status, size = get(p)
    print(f"{ms:9.1f} ms  {status!s:>6}  {size:>8} B  {path}")

print("\n== is the loop blocked? /health while one slow request runs ==")
for probe in ["/api/git/status", "/api/git/branches", "/api/deps", "/api/capabilities",
              "/api/git/log?limit=200"]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(get, probe)
        time.sleep(0.02)                      # let the slow one get into the handler
        fast = pool.submit(get, "/health")
        s, f = slow.result(), fast.result()
    print(f"{probe:32} slow={s[1]:8.1f} ms   /health during it={f[1]:8.1f} ms"
          + ("   <-- BLOCKED" if f[1] > 60 else ""))

print("\n== 8 parallel /api/git/status, the way the panel fires them ==")
t0 = time.perf_counter()
with ThreadPoolExecutor(max_workers=8) as pool:
    res = list(pool.map(lambda _: get("/api/git/status"), range(8)))
wall = (time.perf_counter() - t0) * 1000
print(f"wall {wall:.0f} ms, per-request " +
      ", ".join(f"{r[1]:.0f}" for r in res))

import os
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (analyzer, autoupdater, config, mcp_registry, plan_runtime,
                proctools, session_runtime, store)
from .routers import api, capabilities, chat, hosting, mcp, memory, plugins, profiles, vcs


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_home()
    config.load_env()
    try:
        config.PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    # Orphan sweep, before anything else starts: children of a previous crash
    # (turn workers, watchers, a replaced server) are identified by their argv
    # marker (never by the exe name) and killed only when the current command
    # line still proves they are ours. This process registers itself so
    # /api/processes shows the server too.
    try:
        proctools.sweep()
    except Exception:
        pass
    try:
        proctools.register(proctools.ROLE_SERVER, os.getpid(),
                           cmdline=" ".join(sys.argv))
    except Exception:
        pass
    # Reads finished transcripts on a timer and leaves proposals behind. It is
    # never on the request path, and it cannot change an answer by itself.
    # Stage 8: the startup check is authoritative, learning off means the
    # worker is not even started.
    try:
        analyzer.sync_worker()
    except Exception:
        pass
    # Start autoupdater background task
    autoupdater_task = None
    try:
        autoupdater_task = asyncio.create_task(autoupdater_background())
    except Exception:
        pass
    yield
    # Every worker this server spawned, turn workers, assistant workers,
    # gets the two-stage stop, and its driver's epilogue (the transcript
    # save) is given a moment to land. A restart that saves the partial
    # transcript instead of orphaning the children.
    try:
        session_runtime.shutdown()
    except Exception:
        pass
    # A plan is an in-process thread, so it dies with the server whether this
    # goes well or badly. Flagging each one as aborted is what keeps the
    # snapshot the transcript saved from claiming, after a restart, that a plan
    # is still exploring, a plan nobody is running would otherwise come back on
    # reload and hold the composer shut.
    try:
        plan_runtime.shutdown()
    except Exception:
        pass
    # Stop autoupdater background task
    if autoupdater_task:
        autoupdater_task.cancel()
        try:
            await autoupdater_task
        except Exception:
            pass
    try:
        analyzer.worker.stop()
    except Exception:
        pass
    try:
        mcp_registry.stop_all()
    except Exception:
        pass
    try:
        if config.PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            config.PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    # The server's own registry row comes off at shutdown, so the next boot's
    # sweep does not meet a dead pid wearing this boot's marker.
    try:
        proctools.unregister(os.getpid())
    except Exception:
        pass


async def autoupdater_background():
    """Background task that periodically checks for updates."""
    while True:
        try:
            await asyncio.sleep(60)  # Check every minute if a check is due
            await autoupdater.auto_check_if_needed()
        except asyncio.CancelledError:
            break
        except Exception:
            # Log but don't crash the background task
            pass


app = FastAPI(title="Tacit", lifespan=lifespan)


@app.middleware("http")
async def no_cache_api(request, call_next):
    """Every /api/ response is revalidated before it is reused.

    The pages already carried `Cache-Control: no-cache` (see / and /mobile below),
    but the JSON they read did not - and the JSON is where the bug lived. PLAN's
    item-3 anchor names it: "/api/sessions is not covered by that header - check
    it." Checked: `/api/sessions/registry` came back with no Cache-Control, no
    ETag and no Last-Modified, so a browser was free to keep its own copy and show
    a session list that no longer matches the disk - the reported symptom, deleted
    sessions reappearing, with the store itself behaving correctly. app.js:197
    fetches that path with a bare fetch(), so nothing on the request side saves it
    either (and a request-side directive is a hint to proxies, not a browser's
    cache policy anyway).

    `/static/` is deliberately untouched: those assets are versioned by the `?v=NN`
    query and revalidate by ETag, which is the mechanism that lets a restart swap
    app.js without a hard refresh. Marking them no-store would only make every load
    re-download them.
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(api.router)
app.include_router(chat.router)
app.include_router(vcs.router)
app.include_router(hosting.router)
app.include_router(mcp.router)
app.include_router(plugins.router)
app.include_router(memory.router)
app.include_router(profiles.router)
app.include_router(capabilities.router)


@app.get("/health")
async def health():
    return {"ok": True, "sessions": len(store.list_sessions())}


@app.get("/")
async def index():
    page = config.STATIC_DIR / "index.html"
    if not page.exists():
        return JSONResponse({"ok": False, "error": "static/index.html not found"}, status_code=404)
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/mobile")
async def mobile():
    """The phone frontend. Same origin, same socket, same store: this serves a page,
    not a second service - there is nothing here for CORS or a second port to get wrong."""
    page = config.STATIC_DIR / "mobile.html"
    if not page.exists():
        return JSONResponse({"ok": False, "error": "static/mobile.html not found"}, status_code=404)
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


if config.STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


def _loopback(host: str) -> bool:
    return str(host or "").strip().lower() in ("127.0.0.1", "localhost", "::1", "")


def main():
    import uvicorn
    host = config.HOST
    if not _loopback(host):
        # There is no authentication, and startup is the moment the person running
        # it can still do something about that. The README says so in prose; a
        # banner says it where it is actionable, and names the variable to set.
        # Their own access log showing a LAN address driving the agent is the
        # reason this is printed rather than left to the documentation.
        print(f"[tacit] WARNING: bound to {host} and Tacit has no authentication.")
        print("[tacit]          Anyone who can reach this port can drive the agent, "
              "read your files and run commands.")
        print("[tacit]          Set TACIT_HOST=127.0.0.1 to keep it on this machine.")
    # Built by hand rather than handed to uvicorn.run so the Server object stays
    # reachable: a restart asks this instance to exit, which runs the lifespan
    # shutdown (analyzer, MCP servers, the pid file) instead of killing the
    # process mid-request.
    # timeout_graceful_shutdown bounds the wait for open connections at exit.
    # Without it a WebSocket that never answers the close handshake holds the
    # shutdown open forever, the port never frees, and the restart watcher
    # gives up with no server left running. Five seconds is generous for the
    # close handshake; a turn's worker thread is a daemon thread and dies with
    # the process either way.
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=config.PORT,
                                           log_level="info",
                                           timeout_graceful_shutdown=5))
    config.SERVER = server
    server.run()


if __name__ == "__main__":
    main()

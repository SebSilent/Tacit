import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, mcp_registry, store
from .routers import api, chat, dsh, hosting, mcp, memory, plugins, profiles, vcs


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_home()
    config.load_env()
    try:
        config.PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    yield
    try:
        mcp_registry.stop_all()
    except Exception:
        pass
    try:
        if config.PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            config.PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


app = FastAPI(title="Tacit", lifespan=lifespan)
app.include_router(api.router)
app.include_router(chat.router)
app.include_router(vcs.router)
app.include_router(hosting.router)
app.include_router(mcp.router)
app.include_router(plugins.router)
app.include_router(memory.router)
app.include_router(dsh.router)
app.include_router(profiles.router)


@app.get("/health")
async def health():
    return {"ok": True, "sessions": len(store.list_sessions())}


@app.get("/")
async def index():
    page = config.STATIC_DIR / "index.html"
    if not page.exists():
        return JSONResponse({"ok": False, "error": "static/index.html not found"}, status_code=404)
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


if config.STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


def main():
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()

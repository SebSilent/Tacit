import json
import os
import platform as _platform
import sys
from pathlib import Path

PLATFORM = os.environ.get("TACIT_PLATFORM") or {
    "Windows": "Windows (cmd.exe/PowerShell — there is no ls, grep, find or rm)",
    "Darwin": "macOS (a POSIX shell)",
    "Linux": "Linux (a POSIX shell)",
}.get(_platform.system(), _platform.system())

SHELL = os.environ.get("COMSPEC") or os.environ.get("SHELL") or "/bin/sh"

ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT / "backend"
STATIC_DIR = ROOT / "static"

USER_HOME = Path(os.environ.get("TACIT_USER_HOME") or Path.home())
HOME = Path(os.environ.get("TACIT_HOME") or (USER_HOME / ".tacit"))

MODELS_FILE = HOME / "models.json"
ENV_FILE = HOME / ".env"
PREFS_FILE = HOME / "prefs.json"
GITHUB_FILE = HOME / "github.json"
SESSIONS_DIR = HOME / "sessions"
SKILLS_DIR = HOME / "skills"
KNOWLEDGE_DIR = HOME / "knowledge"
EVIDENCE_FILE = HOME / "evidence.jsonl"
BENCHMARK_FILE = HOME / "benchmarks.json"
PLANS_DIR = HOME / "plans"
CHECKPOINT_DIR = HOME / "checkpoints"
PID_FILE = HOME / "tacit.pid"
PLUGINS_FILE = HOME / "plugins.json"
PLUGINS_USER_DIR = HOME / "plugins"
MCP_FILE = HOME / "mcp.json"
PROFILES_FILE = HOME / "profiles.json"
MEMORY_DB = HOME / "memory.db"
ADAPTERS_DIR = HOME / "adapters"
BRIDGES_DIR = ROOT / "bridges"
BUNDLED_PLUGINS_DIR = BACKEND_DIR / "plugins"

HOST = os.environ.get("TACIT_HOST", "0.0.0.0")
PORT = int(os.environ.get("TACIT_PORT", "8550"))

AGENT_MAX_STEPS = int(os.environ.get("TACIT_MAX_STEPS", "24"))
AGENT_CONTEXT_BUDGET = int(os.environ.get("TACIT_CONTEXT_BUDGET", "120000"))
TEMPERATURE = float(os.environ.get("TACIT_TEMPERATURE", "0.2"))
TOOL_OUTPUT_LIMIT = int(os.environ.get("TACIT_TOOL_OUTPUT_LIMIT", "6000"))
SHELL_TIMEOUT = int(os.environ.get("TACIT_SHELL_TIMEOUT", "180"))

SUBAGENT_MAX_STEPS = int(os.environ.get("TACIT_SUBAGENT_STEPS", "12"))
SUBAGENT_RESULT_LIMIT = int(os.environ.get("TACIT_SUBAGENT_RESULT_LIMIT", "4000"))
SUBAGENT_MAX_DEPTH = int(os.environ.get("TACIT_SUBAGENT_DEPTH", "1"))

COMPACT_AT = float(os.environ.get("TACIT_COMPACT_AT", "0.65"))
COMPACT_KEEP_TAIL = int(os.environ.get("TACIT_COMPACT_KEEP", "6"))

PLAN_MAX_TASKS = int(os.environ.get("TACIT_PLAN_TASKS", "4"))
PLAN_MAX_QUESTIONS = int(os.environ.get("TACIT_PLAN_QUESTIONS", "4"))
def _find_playwright() -> str:
    env = os.environ.get("TACIT_PLAYWRIGHT_PATH")
    if env:
        return env
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    candidates = [Path(__file__).resolve().parent.parent / "node_modules" / "playwright",
                  home / "node_modules" / "playwright",
                  home / "AppData/Roaming/npm/node_modules/playwright",
                  home / ".local/lib/node_modules/playwright"]
    for p in candidates:
        if (p / "package.json").exists():
            return str(p)
    return ""


def python_playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


PLAYWRIGHT_PATH = _find_playwright()
BROWSER_TIMEOUT = int(os.environ.get("TACIT_BROWSER_TIMEOUT", "90"))
BROWSER_TEXT_LIMIT = int(os.environ.get("TACIT_BROWSER_TEXT_LIMIT", "6000"))
BROWSER_SEARCH_TIMEOUT = int(os.environ.get("TACIT_BROWSER_SEARCH_TIMEOUT", "120"))
RESEARCH_ASPECTS = int(os.environ.get("TACIT_RESEARCH_ASPECTS", "3"))
RESEARCH_MAX_STEPS = int(os.environ.get("TACIT_RESEARCH_STEPS", "10"))


SKILL_INDEX_LIMIT = int(os.environ.get("TACIT_SKILL_INDEX_LIMIT", "24"))

FETCH_LIMIT = int(os.environ.get("TACIT_FETCH_LIMIT", "6000"))
SNAPSHOT_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
                 "checkpoints", ".pytest_cache", ".mypy_cache"}
SNAPSHOT_MAX_FILE = int(os.environ.get("TACIT_SNAPSHOT_MAX_FILE", str(2 * 1024 * 1024)))
SNAPSHOT_MAX_FILES = int(os.environ.get("TACIT_SNAPSHOT_MAX_FILES", "3000"))



def load_env() -> None:
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v
    except FileNotFoundError:
        pass


def ensure_home() -> None:
    for d in (HOME, SESSIONS_DIR, SKILLS_DIR, KNOWLEDGE_DIR, PLANS_DIR, CHECKPOINT_DIR,
              PLUGINS_USER_DIR, ADAPTERS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not MODELS_FILE.exists():
        MODELS_FILE.write_text(json.dumps({"default": "", "providers": {}}, indent=2), encoding="utf-8")
    if not PREFS_FILE.exists():
        PREFS_FILE.write_text(json.dumps({
            "projectRoots": [], "model": "", "mode": "agent", "thinking": "medium",
        }, indent=2), encoding="utf-8")


def read_json(path: Path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path: Path, value) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2), encoding="utf-8")


def prefs() -> dict:
    return read_json(PREFS_FILE, {})


def save_prefs(patch: dict) -> dict:
    merged = {**prefs(), **patch}
    write_json(PREFS_FILE, merged)
    return merged


def registry() -> dict:
    return read_json(MODELS_FILE, {"default": "", "providers": {}})


def save_registry(reg: dict) -> None:
    write_json(MODELS_FILE, reg)


def key_for(spec: dict) -> str:
    k = (spec or {}).get("apiKey")
    if not k:
        return ""
    if isinstance(k, str):
        if k.isupper() and k.replace("_", "").isalnum():
            return os.environ.get(k, "")
        return k
    kind = k.get("kind")
    if kind == "env":
        return os.environ.get(k.get("env", ""), "")
    if kind == "value":
        return k.get("value", "")
    return ""


def model_list() -> list[dict]:
    reg = registry()
    out = []
    for pid, spec in (reg.get("providers") or {}).items():
        for m in spec.get("models") or []:
            out.append({
                "id": f"{pid}/{m.get('id')}",
                "name": m.get("name") or m.get("id"),
                "provider": pid,
                "model": m.get("id"),
                "reasoning": bool(m.get("reasoning")),
                "contextWindow": m.get("contextWindow") or 0,
                "maxTokens": m.get("maxTokens") or 0,
                "baseUrl": spec.get("baseUrl") or "",
            })
    return out


def resolve_model(ref: str | None = None) -> dict | None:
    reg = registry()
    providers = reg.get("providers") or {}
    available = model_list()
    chosen = (ref or reg.get("default") or "").strip()
    if chosen.partition("/")[0] not in providers and available:
        chosen = available[0]["id"]
    if not chosen:
        return None
    pid, _, mid = chosen.partition("/")
    spec = providers.get(pid)
    if not spec:
        return None
    meta = next((m for m in (spec.get("models") or []) if m.get("id") == mid), {})
    return {
        "ref": chosen,
        "provider": pid,
        "model": mid,
        "baseUrl": str(spec.get("baseUrl") or "").rstrip("/"),
        "apiKey": key_for(spec),
        "contextWindow": meta.get("contextWindow") or 0,
        "maxTokens": meta.get("maxTokens") or 0,
        "reasoning": bool(meta.get("reasoning")),
    }


def project_roots() -> list[str]:
    """Explicitly configured workspace roots only — never an assumed layout.

    There is deliberately no auto-scan of ~/Projects, ~/Dev and friends: a
    folder becomes a workspace only because the user pointed at it. With
    nothing configured this is just the home directory, used as a neutral
    fallback (a working directory for version-control credential lookups).
    """
    env = (os.environ.get("TACIT_PROJECTS_ROOTS") or "").strip()
    if env:
        cands = [p.strip() for p in env.split(os.pathsep) if p.strip()]
    else:
        cands = [str(p) for p in (prefs().get("projectRoots") or []) if p]
    return [c for c in cands if Path(c).is_dir()] or [str(USER_HOME)]


def fs_roots() -> list[dict]:
    """Places to start browsing: drives on Windows, roots/mounts elsewhere."""
    out: list[dict] = []
    if os.name == "nt":
        import string as _string
        for letter in _string.ascii_uppercase:
            drive = f"{letter}:\\"
            if Path(drive).exists():
                out.append({"name": drive, "path": drive})
    else:
        out.append({"name": "/", "path": "/"})
        for base in ("/Volumes", "/mnt", "/media"):
            if Path(base).is_dir():
                out.append({"name": base, "path": base})
    home = str(USER_HOME)
    if not any(r["path"] == home for r in out):
        out.append({"name": "Home", "path": home})
    return out


def fs_list(path: str | None = None) -> dict:
    """List the sub-directories of `path` for the in-app folder browser."""
    raw = str(path or "").strip() or str(USER_HOME)
    try:
        p = Path(raw).expanduser().resolve()
    except Exception:
        p = Path(raw)
    if not p.is_dir():
        return {"ok": False, "error": "not a directory", "path": str(p)}
    dirs = []
    try:
        for entry in sorted(p.iterdir(), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
            except OSError:
                continue
    except PermissionError:
        return {"ok": False, "error": "permission denied", "path": str(p)}
    parent = str(p.parent) if p.parent != p else ""
    return {"ok": True, "path": str(p), "name": p.name or str(p),
            "parent": parent, "dirs": dirs, "count": len(dirs)}


def all_project_roots(sessions: list[dict]) -> list[str]:
    roots = list(project_roots())
    seen = {r.lower() for r in roots}
    for s in sessions or []:
        project = (s or {}).get("project")
        if not project:
            continue
        parent = str(Path(project).parent)
        if parent == str(USER_HOME) or Path(parent).parent == Path(parent):
            continue
        if parent.lower() in seen or not Path(parent).is_dir():
            continue
        seen.add(parent.lower())
        roots.append(parent)
    return roots


def list_projects(roots: list[str]) -> list[dict]:
    seen, out = set(), []
    for root in roots:
        try:
            entries = sorted(Path(root).iterdir(), key=lambda p: p.name.lower())
        except Exception:
            continue
        for e in entries:
            if not e.is_dir() or e.name.startswith(".") or e.name == "node_modules":
                continue
            key = str(e).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": e.name, "path": str(e), "root": root})
    return out


REASONING_EFFORT = {
    "off": None, "minimal": "minimal", "low": "low", "medium": "medium",
    "high": "high", "xhigh": "high", "max": "high",
}


def model_inputs(ref: str | None = None) -> list[str]:
    reg = registry()
    chosen = (ref or reg.get("default") or "").strip()
    pid, _, mid = chosen.partition("/")
    spec = (reg.get("providers") or {}).get(pid) or {}
    meta = next((m for m in (spec.get("models") or []) if m.get("id") == mid), {})
    return meta.get("input") or ["text"]


def accepts_images(ref: str | None = None) -> bool:
    return "image" in model_inputs(ref)


def reasoning_for(level: str | None) -> str | None:
    return REASONING_EFFORT.get((level or "").lower())

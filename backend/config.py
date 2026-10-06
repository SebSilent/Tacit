import json
import os
import platform as _platform
import sys
from pathlib import Path

OS = os.environ.get("TACIT_OS") or _platform.system()

# The interpreter that shell=True will actually pick. The agent is told this, because a model
# that assumes bash on Windows emits commands that run, return 0, and do nothing.
SHELL_KIND = {"Windows": "cmd.exe", "Darwin": "zsh", "Linux": "bash"}.get(OS, OS)

PLATFORM = os.environ.get("TACIT_PLATFORM") or {
    "Windows": "Windows (cmd.exe): there is no ls, grep, find or rm",
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
FOLDERS_FILE = HOME / "folders.json"
CAPABILITIES_FILE = HOME / "capabilities.json"
AUDIT_FILE = HOME / "audit.jsonl"
LEARNING_FILE = HOME / "learning.json"
MEMORY_DB = HOME / "memory.db"
ADAPTERS_DIR = HOME / "adapters"
BRIDGES_DIR = ROOT / "bridges"
BUNDLED_PLUGINS_DIR = BACKEND_DIR / "plugins"

HOST = os.environ.get("TACIT_HOST", "0.0.0.0")
PORT = int(os.environ.get("TACIT_PORT", "8550"))

AGENT_MAX_STEPS = int(os.environ.get("TACIT_MAX_STEPS", "24"))
AGENT_CONTEXT_BUDGET = int(os.environ.get("TACIT_CONTEXT_BUDGET", "120000"))
# Fallback above is used only when a model's window is unknown. When it is known
# the window decides, clamped to these so a 1M-token model is not sent an
# unbounded transcript and an 8k one is not sent more than it can hold.
CONTEXT_BUDGET_MIN = int(os.environ.get("TACIT_CONTEXT_BUDGET_MIN", "24000"))
CONTEXT_BUDGET_MAX = int(os.environ.get("TACIT_CONTEXT_BUDGET_MAX", "2000000"))
TEMPERATURE = float(os.environ.get("TACIT_TEMPERATURE", "0.2"))
# Whether that number was chosen by the operator or is just the default. A thinking model is left
# alone only when nobody has asked for a specific temperature; an explicit setting always wins,
# including when it is set to the same 0.2.
TEMPERATURE_SET = "TACIT_TEMPERATURE" in os.environ
TOOL_OUTPUT_LIMIT = int(os.environ.get("TACIT_TOOL_OUTPUT_LIMIT", "6000"))

# Reading a file is the one tool where a partial answer is worse than none. To
# reimplement or repair a source file the agent needs the whole thing in context: a
# 6,000-character ceiling on a 17,578-character file leaves two thirds of it unseen,
# and the measured failure mode is the agent abandoning `read_file` and re-deriving the
# same facts through shell probes, which is how a task that needs one 400-line write
# ends up as 30 thin rounds. Shell and search output stay clipped: those are logs and
# match lists, not an artifact to reproduce.
READ_OUTPUT_LIMIT = int(os.environ.get("TACIT_READ_OUTPUT_LIMIT", "48000"))
SHELL_TIMEOUT = int(os.environ.get("TACIT_SHELL_TIMEOUT", "180"))

SUBAGENT_MAX_STEPS = int(os.environ.get("TACIT_SUBAGENT_STEPS", "12"))
SUBAGENT_RESULT_LIMIT = int(os.environ.get("TACIT_SUBAGENT_RESULT_LIMIT", "4000"))
SUBAGENT_MAX_DEPTH = int(os.environ.get("TACIT_SUBAGENT_DEPTH", "1"))

COMPACT_AT = float(os.environ.get("TACIT_COMPACT_AT", "0"))
# What has to stay free is room for the next turn's work — the summary the model
# has to write plus whatever the current step still produces. That is roughly
# CONSTANT in tokens, not a fraction of the window, so a flat percentage is wrong
# at both ends: 0.65 compacted a 1M-token model at 650K (throwing away 350K of
# usable context) and an 8K model at 5.2K (leaving too little to answer in).
# Claude Code derives its trigger the same way, at a ~33K reserve. Set
# TACIT_COMPACT_AT explicitly to go back to a flat fraction.
CONTEXT_RESERVE_TOKENS = int(os.environ.get("TACIT_CONTEXT_RESERVE", "33000"))
# A floor, not a ceiling. On a large window the reserve is the binding constraint
# and needs no cap on top — leaving 33K free *is* the safety margin, and capping at
# 95% would override the reserve model exactly where it works best. The floor only
# matters where the reserve exceeds the window itself, so a small model still gets
# half its context to work in rather than a negative budget.
CONTEXT_FILL_FLOOR = float(os.environ.get("TACIT_CONTEXT_FILL_FLOOR", "0.5"))

# The tail is kept by token budget, not by message count. Six messages is six
# huge tool results on one turn and six one-line answers on another; the first
# blows the summary's input and the second throws away usable recent work.
COMPACT_TAIL_TOKENS = int(os.environ.get("TACIT_COMPACT_TAIL_TOKENS", "8000"))
COMPACT_TAIL_MIN_MESSAGES = int(os.environ.get("TACIT_COMPACT_TAIL_MIN", "4"))
COMPACT_TAIL_MAX_MESSAGES = int(os.environ.get("TACIT_COMPACT_TAIL_MAX", "40"))
# At most this share of the fill target is kept verbatim. The share is what makes
# the tail work on a small window: an absolute 8,000-token tail is larger than an
# 8K model's whole usable budget, and a tail that swallows everything leaves
# nothing to summarise.
COMPACT_TAIL_SHARE = float(os.environ.get("TACIT_COMPACT_TAIL_SHARE", "0.4"))
# Old tool output is cleared before paying a model to summarise it. Often that
# alone brings the transcript back under budget and the summary call is skipped.
COMPACT_PRUNE_KEEP_CHARS = int(os.environ.get("TACIT_COMPACT_PRUNE_KEEP", "400"))
COMPACT_PRUNE_MIN_CHARS = int(os.environ.get("TACIT_COMPACT_PRUNE_MIN", "1200"))
# Summarising with the working model is expensive and can fail. A cheap model can
# be nominated for the job alone, and a deterministic digest is the floor.
COMPACT_MODEL = os.environ.get("TACIT_COMPACT_MODEL", "")
# The summariser call must not itself overflow the window it is relieving.
SUMMARY_INPUT_MAX_CHARS = int(os.environ.get("TACIT_SUMMARY_INPUT_MAX", "160000"))
COMPACT_KEEP_TAIL = int(os.environ.get("TACIT_COMPACT_KEEP", "6"))
# Compaction used to be checked once, before a turn began. A 134-call turn then
# grew without anything ever re-measuring it, which is how one session reached
# 1.9M prompt tokens with the dashboard's "saved by compaction" row still at zero.
COMPACT_MID_TURN = os.environ.get("TACIT_COMPACT_MID_TURN", "1") not in ("0", "false", "")
# Hard ceiling on tokens one turn may bill, 0 for none. Off by default like every
# other constraint here; the fractions below are the always-on part, and they only
# report.
COST_WARN_FRACTIONS = tuple(
    float(part) for part in
    (p.strip() for p in os.environ.get("TACIT_COST_WARN", "0.25,0.5,0.75").split(","))
    if part
)
TURN_TOKEN_BUDGET = int(os.environ.get("TACIT_TURN_TOKEN_BUDGET", "0"))

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



def context_fill_target(window: int) -> int:
    """How many tokens of conversation to allow before compacting.

    The reserve model: fill the window up to a fixed token reserve, because what
    must stay free is room for the next turn's work, and that is roughly constant
    rather than proportional. Floored at half the window, where the reserve is
    larger than the window itself. ``TACIT_COMPACT_AT`` set explicitly overrides
    the whole thing with a flat fraction.

        1,000,000 window ->   967,000  (96.7%)
          200,000 window ->   167,000  (83.5%)
           32,000 window ->    16,000  (50%, floored)
            8,000 window ->     4,000  (50%, floored)
    """
    try:
        window = int(window or 0)
    except (TypeError, ValueError):
        return 0
    if window <= 0:
        return 0
    if COMPACT_AT > 0:
        return max(1, int(window * min(COMPACT_AT, 1.0)))
    return max(int(window * CONTEXT_FILL_FLOOR), window - CONTEXT_RESERVE_TOKENS)


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
            "projectRoots": [], "model": "", "mode": "agent", "thinking": "default",
            "deliverGuarantee": True,
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


def deliver_guarantee() -> bool:
    """The delivery guarantee, on by default.

    On means the turn never ends quietly short of the artifact: the file the task names gets
    ordered written mid-turn, forced written at the end, run before the turn claims done, and
    landed inside the last 300 seconds of wall budget instead of dying to the clock. Off means
    the agent works like a thin wrapper: it stops when it stops, and nothing is forced, which is
    cheaper when a person is steering by hand. The mechanic is not part of the switch: a round
    our own output ceiling destroyed gets re-asked uncapped either way, because discarding it
    would bill the model's thinking as waste.
    """
    raw = os.environ.get("TACIT_DELIVER_GUARANTEE")
    if raw is not None:
        return raw.strip().lower() not in ("0", "off", "false", "no")
    v = prefs().get("deliverGuarantee")
    return True if v is None else bool(v)


def save_prefs(patch: dict) -> dict:
    merged = {**prefs(), **patch}
    write_json(PREFS_FILE, merged)
    return merged


def allow_vcs() -> bool:
    """Whether the agent may run version-control commands. Off by default.

    Version control is a human action here. A model left to its own devices will
    commit and push far more than anyone asked for, and a repository fills up
    with noise that has to be cleaned up by hand. The Git panel exists so the
    user can do it deliberately. This switch is for the people who do want the
    agent to handle it, and it is theirs to turn on.
    """
    return bool(prefs().get("allowVersionControl"))


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
                "temperature": m.get("temperature"),
                "stale": bool(m.get("stale")),
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
        # Some vendors ship a sampling default for a thinking model that is better than any number
        # picked here. A model entry may state one, and it beats TACIT_TEMPERATURE only for that
        # model; the flat default still covers every model that says nothing.
        "temperature": meta.get("temperature"),
        # Read from /api/show and cached: what a model accepts, and what it does when
        # nothing is sent. Without these a level the model does not list would be
        # passed through and resolved to its default in silence.
        "thinkingValues": meta.get("thinkingValues") or [],
        "thinkingDefault": meta.get("thinkingDefault") or None,
        # Both halves of the provenance, not just the list: "published" and "measured"
        # are different claims, and an interface that cannot tell them apart cannot
        # say how much to trust the menu it is showing.
        "thinkingSource": meta.get("thinkingSource") or None,
        "thinkingGraded": bool(meta.get("thinkingGraded")),
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


# Levels are passed through under the provider's own names where it has them, so
# its table decides what a level means. One translation is ours and it is the one
# that was wrong: "off" must send a value that actually turns thinking off. Sending
# nothing does not disable thinking, it leaves thinking on, which is the opposite of
# what the control claims. Measured on ollama.com: every named effort produced 1.5k
# to 1.8k characters of reasoning, while "none" and "off" produced zero.
REASONING_EFFORT = {
    "off": "none", "minimal": "minimal", "low": "low", "medium": "medium",
    "high": "high", "xhigh": "xhigh", "max": "max",
}

# Prompt caching. "auto" puts cache breakpoints where the endpoint is known to
# understand them and does nothing elsewhere, because an OpenAI-compatible server
# that has never seen `cache_control` may reject the request outright. "on" forces
# them for a gateway that supports them but is not recognised; "off" never sends
# them. Either way a rejection is retried once without them, so enabling caching
# cannot be the reason a turn fails.
PROMPT_CACHE = os.environ.get("TACIT_PROMPT_CACHE", "auto").strip().lower()
# Anthropic requires max_tokens and requires a thinking budget below it.
ANTHROPIC_MAX_TOKENS = int(os.environ.get("TACIT_ANTHROPIC_MAX_TOKENS", "8192"))
ANTHROPIC_VERSION = os.environ.get("TACIT_ANTHROPIC_VERSION", "2023-06-01")
ANTHROPIC_THINKING_BUDGET = int(os.environ.get("TACIT_ANTHROPIC_THINKING", "4096"))


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
    """Map a picker value to the name to ask for. Unknown names pass through, so a
    level discovered from a model is not dropped by a table that predates it."""
    lv = (level or "").strip().lower()
    if not lv:
        return None
    return REASONING_EFFORT.get(lv, lv)

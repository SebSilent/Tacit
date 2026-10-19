import json
import os
import shutil
import platform as _platform
import threading
import time
import sys
import time
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
# Scratch root for a session that has no project. See `work_root` below: the point of
# this directory is that a project-less Agent session no longer gets its tools rooted
# in the user's home folder.
WORKSPACES_DIR = HOME / "workspaces"
SKILLS_DIR = HOME / "skills"
KNOWLEDGE_DIR = HOME / "knowledge"
EVIDENCE_FILE = HOME / "evidence.jsonl"
# Per-model settings (temperature / max_tokens / context_budget overrides).
# The file keeps its old name so existing installs keep their rows without a
# migration; it used to be written by the benchmark harness, which is where the
# name comes from.
MODEL_SETTINGS_FILE = HOME / "benchmarks.json"
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
# The uvicorn.Server, set by main() once it is running. A restart asks this
# instance to exit rather than killing the process, so the lifespan shutdown
# (the analyzer, the MCP servers, the pid file) still runs.
SERVER = None

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
# What has to stay free is room for the next turn's work, the summary the model
# has to write plus whatever the current step still produces. That is roughly
# CONSTANT in tokens, not a fraction of the window, so a flat percentage is wrong
# at both ends: 0.65 compacted a 1M-token model at 650K (throwing away 350K of
# usable context) and an 8K model at 5.2K (leaving too little to answer in).
# Claude Code derives its trigger the same way, at a ~33K reserve. Set
# TACIT_COMPACT_AT explicitly to go back to a flat fraction.
CONTEXT_RESERVE_TOKENS = int(os.environ.get("TACIT_CONTEXT_RESERVE", "33000"))
# A floor, not a ceiling. On a large window the reserve is the binding constraint
# and needs no cap on top, leaving 33K free *is* the safety margin, and capping at
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
# Retuned for what this tool actually is: a bounded read-only sweep, not an
# unbounded investigation. Ten steps per aspect was sized for work nobody asks
# research for, an ordinary check is two or three fetches and a browse, and the
# steps past that are a sub-agent wandering. The wall clock came down with it:
# 600s of a card that looked frozen was indistinguishable from a hang, and the
# cap fires a *partial report* rather than an error, so a shorter cap costs a
# thinner answer, never a missing one. These three numbers are what the tool
# description and the run's first notify state, so the budget is on screen and
# not hidden here.
RESEARCH_MAX_STEPS = int(os.environ.get("TACIT_RESEARCH_STEPS", "6"))
# A research call used to be unbounded in wall clock and tokens: three sub-agents
# at ten steps each, serially, with nothing watching the clock. The cap is on the
# whole call, not per aspect, because the caller is the one waiting.
RESEARCH_TIMEOUT_S = int(os.environ.get("TACIT_RESEARCH_TIMEOUT", "240"))
# Delegated work (sub-agents, research) runs on the session's model unless this
# names another one. Empty means: same model, which is the old behaviour.
DELEGATE_MODEL = os.environ.get("TACIT_DELEGATE_MODEL", "")


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


# A state file is read constantly and rewritten by a driver thread while those
# reads happen. Windows refuses either side of that for the length of an open
# handle (and a handle on a small JSON file is open for milliseconds) so the
# answer is to wait a moment, not to report the file as missing or drop the
# write. Bounded, because a genuine lock (antivirus, an editor) should surface
# as a warning rather than hang the turn.
_SHARE_TRIES = 20
_SHARE_WAIT = 0.005


def _locked(exc: OSError) -> bool:
    """Is this the "someone else is holding it" error rather than a real fault?"""
    return isinstance(exc, PermissionError)


def read_json(path: Path, fallback):
    """Read a JSON state file, or `fallback` if it is not there.

    A missing file is the normal case and stays quiet. A file that is there but
    will not read is not: it used to be swallowed into the same fallback, so a
    session that was merely being rewritten looked identical to a session that
    had never existed, and the caller then behaved as if the person's work had
    been deleted. Say so on stderr rather than pretend it was never there.
    """
    path = Path(path)
    last = None
    for _ in range(_SHARE_TRIES):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return fallback
        except Exception as exc:
            if _locked(exc):
                last = exc
                time.sleep(_SHARE_WAIT)
                continue
            print(f"[config] {path} is unreadable ({exc.__class__.__name__}: {exc}); "
                  f"using fallback", file=sys.stderr)
            return fallback
    print(f"[config] {path} stayed locked "
          f"({last.__class__.__name__}: {last}); using fallback", file=sys.stderr)
    return fallback


def write_json(path: Path, value) -> None:
    """Write a state file so a reader never sees half of it.

    A plain write_text truncates first and fills after. The turn's driver thread
    saves the session record while the interface reads the same file, and a read
    that landed in that gap got an empty or partial document — which read_json
    reported as "no such session". Tests papered over it by waiting for the bus
    to die before reading; the real fix is the write.

    So: write beside the target, then rename over it. os.replace is atomic on
    Windows and POSIX alike, so a reader gets the whole old file or the whole
    new one, never a mixture. The temp name carries the pid and thread id
    because two threads may save different records from the same directory at
    the same instant, and it leads with a dot with the real extension kept out
    of the tail: a stray file left by a crash must not be read back as a
    session, a plugin, or — audit.py globs ``audit.json.*`` — a retired ledger.

    The rename is the part Windows objects to: it is refused with Access denied
    while any reader holds the target open, so it waits for the handle to close
    instead of losing the save.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.stem}.{os.getpid()}.{threading.get_ident()}.writing"
    try:
        tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
        for _ in range(_SHARE_TRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                time.sleep(_SHARE_WAIT)
        else:
            os.replace(tmp, path)          # one last try, then let it surface
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def prefs() -> dict:
    return read_json(PREFS_FILE, {})


def delegate_model() -> str:
    """The model delegated work runs on: the pref, else the env var, else ''.

    Empty means the session's own model. A sub-agent inherits its parent's model
    by construction, which is usually right — but a reasoning-heavy session model
    at thinking=max burns the expensive tokens on fetch-and-grep work a cheap
    model does just as well, and that choice was never reachable.
    """
    v = str(prefs().get("delegateModel") or "").strip()
    if v:
        return v
    return str(os.environ.get("TACIT_DELEGATE_MODEL") or "").strip()


def turn_worker_mode() -> str:
    """How a session turn runs: its own process, or a thread in here.

    "process" (the default) is the decision the deleted plan records: a turn is a
    unit of work a person must be able to kill, and a thread cannot be
    killed in Python under any circumstance. The worker process gets a
    cooperative cancel first and a process-tree kill after a short grace,
    so a hung model read or a stuck shell command dies with the turn
    instead of owning the server.

    "thread" is the escape hatch: the old in-process path, which the tests
    that fake the model engine use — a fake engine patched into this
    process is invisible to a worker subprocess.
    """
    raw = os.environ.get("TACIT_TURN_WORKER", "process").strip().lower()
    return "thread" if raw in ("thread", "0", "off", "false", "no") else "process"


def turn_sound() -> bool:
    """Whether the browser chimes when a turn ends. On by default.

    A server preference rather than localStorage, because it describes the
    machine: a person who silenced it wants every device pointed at this
    install to stay silent, and a turn that ends in a background tab should
    announce itself the same way as one that ends in front of them.
    """
    v = prefs().get("turnSound")
    return True if v is None else bool(v)


def alert_sound() -> bool:
    """Whether the browser chimes for things that happen *during* work. Off by default.

    The turn-end chime above announces finished work, which is why it can start
    on. These two are a different promise: a budget warning fires while a turn is
    still running, and the tick fires once per cycle of a loop that can run for
    hours unattended. Sound that repeats by itself is the kind a person reaches
    for the mute on, so it starts silent and is opted into.
    """
    v = prefs().get("alertSound")
    return False if v is None else bool(v)


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


SHELL_OVERLAP_MODES = ("block", "advise", "off")


def shell_overlap_pinned() -> bool:
    """True when TACIT_SHELL_OVERLAP outranks the stored preference.

    The Settings switch needs to know: saving a mode the environment overrides
    would leave a control that lies about what is running, which is the failure
    mode the gated MCP rows on the same tab exist to avoid.
    """
    return os.environ.get("TACIT_SHELL_OVERLAP") is not None


def shell_overlap() -> str:
    """How the agent's shell is policed for commands that re-implement a tool.

    "block" (the default) refuses a command whose every segment is a worse version
    of read_file/list_files/grep_files/write_file/edit_file and returns the tool
    call instead; "advise" runs it and appends one line; "off" stays silent.

    Defaulting to block is the point: the observed behaviour is the agent reaching
    for `type` and `dir` habitually (174 shell calls to 44 read_file on the
    recorded runs), and a switch that defaults off fixes nothing. Off exists
    because an interception that is occasionally wrong has to be overridable from
    inside the session, or it reads as a broken tool.
    """
    raw = os.environ.get("TACIT_SHELL_OVERLAP")
    if raw is None:
        raw = str(prefs().get("shellOverlap") or "")
    v = raw.strip().lower()
    return v if v in SHELL_OVERLAP_MODES else "block"


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


# ── turn budget ────────────────────────────────────────────────────────────
# Each of these caps also has an environment variable, which is not the same
# thing as being tunable: a limit that is too low for one task cannot be raised
# without closing Tacit and editing whatever launches it. So the environment,
# the stored preference and the constant are all read here, in that order, and
# the call sites ask instead of importing a number.
LIMIT_KEYS = {
    "max_steps": ("maxSteps", "TACIT_MAX_STEPS", "AGENT_MAX_STEPS", 1, 200),
    "subagent_steps": ("subagentSteps", "TACIT_SUBAGENT_STEPS", "SUBAGENT_MAX_STEPS", 1, 100),
    "research_steps": ("researchSteps", "TACIT_RESEARCH_STEPS", "RESEARCH_MAX_STEPS", 1, 40),
    "turn_token_budget": ("turnTokenBudget", "TACIT_TURN_TOKEN_BUDGET",
                          "TURN_TOKEN_BUDGET", 0, 50000000),
}


def _limit(name: str) -> int:
    """One budget: the env var, else the saved value, else the module constant.

    The environment outranks the preference the way it does for every other
    harness switch, so a pinned deploy cannot be talked out of its limit by a
    click, and `limits_pinned` can tell the panel when that is the case. The
    constant is looked up by name rather than captured, so a test that patches
    `config.AGENT_MAX_STEPS` still moves what the loop reads.
    """
    pref_key, env_key, const_key, lo, hi = LIMIT_KEYS[name]
    raw = os.environ.get(env_key)
    if raw is None:
        raw = prefs().get(pref_key)
    if raw is None:
        raw = globals()[const_key]
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = int(globals()[const_key])
    return max(lo, min(hi, value))


def max_steps() -> int:
    """Tool steps one agent turn may take before it is asked to wrap up."""
    return _limit("max_steps")


def subagent_steps() -> int:
    """Tool steps one delegated `task` may take."""
    return _limit("subagent_steps")


def research_steps() -> int:
    """Tool steps one aspect of a research sweep may take."""
    return _limit("research_steps")


def turn_token_budget() -> int:
    """Tokens one turn may bill before it stops and reports. 0 means no cap."""
    return _limit("turn_token_budget")


def limits() -> dict:
    """The four budgets as one mapping, for the settings route."""
    return {"max_steps": max_steps(), "subagent_steps": subagent_steps(),
            "research_steps": research_steps(), "turn_token_budget": turn_token_budget()}


def limit_bounds() -> dict:
    """The floor and ceiling each budget is clamped to.

    The panel asks for these instead of hardcoding them, so an input can refuse
    a number the server would clamp anyway.
    """
    return {name: {"min": spec[3], "max": spec[4]} for name, spec in LIMIT_KEYS.items()}


def limits_pinned() -> list[str]:
    """Which env vars currently outrank what Settings stores.

    The panel needs this the way the shell-overlap switch does: a control that
    saves into an override and then reports the saved number is a control that
    lies about the limit the next turn will actually pay.
    """
    return [env_key for (_pref, env_key, _const, _lo, _hi) in LIMIT_KEYS.values()
            if os.environ.get(env_key) is not None]


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
    """Explicitly configured workspace roots only, never an assumed layout.

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


_WS_KEEP = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _workspace_name(session: str) -> str:
    """A usable directory name for one session id.

    Every character that could act as a path separator is replaced, so the result
    cannot climb out of WORKSPACES_DIR no matter what arrives on the wire — `..`
    collapses to nothing and is replaced by "default". Real ids (`s-<hex>`) pass
    through untouched; this is a guard, not a renaming.
    """
    keep = "".join(c if c in _WS_KEEP else "_" for c in str(session or ""))
    return keep.strip("._-")[:48] or "default"


def workspace_dir(session: str = "", create: bool = True) -> Path:
    """The scratch root for a session with no project.

    Read on every call, never cached at import, for the reason documented at
    `store._index_path`: a test that redirects `WORKSPACES_DIR` has to actually
    redirect it.

    `create=False` answers "where would this session work" without making the
    directory. Prompt-building needs that: an estimate of the system prompt's size
    must not leave a folder behind on disk.
    """
    d = Path(WORKSPACES_DIR) / _workspace_name(session)
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def remove_workspace(session: str) -> bool:
    """Delete one session's scratch folder, when it has one.

    Called from `store.remove`, because a deleted session's files would otherwise sit
    in `WORKSPACES_DIR` forever: nothing else ever collects them, and they are the
    thing the model wrote while nobody had chosen a project for it.

    Refuses anything that is not a real per-session folder — an empty id maps to the
    shared `default`, and a path that does not sit under WORKSPACES_DIR is not ours to
    delete. It returns False rather than raising: a session is already gone by then,
    and a failed cleanup must not make the delete look like it failed.
    """
    if not str(session or "").strip():
        return False
    base = Path(WORKSPACES_DIR).resolve()
    d = base / _workspace_name(session)
    try:
        d = d.resolve()
    except OSError:
        return False
    if d == base or not d.is_relative_to(base) or not d.is_dir():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return not d.exists()


def project_usable(project: str | None) -> bool:
    """The `work_root` test on its own, for callers that must describe the situation
    rather than resolve it (the system prompt says where the tools will work, and has
    to agree with `work_root` or it lies)."""
    return bool(project) and Path(str(project)).expanduser().is_dir()


def work_root(project: str | None = "", session: str = "", create: bool = True) -> Path:
    """Where a session's tools operate. One rule, shared by every caller.

    A project that exists always wins — a session with a workdir must never be
    re-rooted into a scratch folder. Only when there is no usable project does the
    session get its own workspace, and the home directory is not a fallback any
    more: it used to be, which meant a session with no project (or with a project
    that had since been deleted) read and wrote in the user's home folder with only
    a short blocklist in the way.

    A project that is *named but missing* counts as no project. Silently working in
    the home folder is worse than working in a scratch folder, and the reason this
    branch exists at all is that stale paths arrive from stored sessions.
    """
    if project:
        p = Path(project).expanduser()
        if p.is_dir():
            return p.resolve()
    return workspace_dir(session, create=create)


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

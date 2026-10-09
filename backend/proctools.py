"""Process identity and containment for everything Tacit spawns.

The problem this module exists for: during a stuck test run, ~23 ``python.exe``
processes sat in the task list and none of them could be attributed — Tacit's
children look like any other Python process, and the user runs other Python
projects on the same machine, so nothing could be killed safely. Two fixes:

1. **Identity at spawn.** Every child gets a recognizable token in argv right
   after the interpreter (``tacit-turn-worker``, ``tacit-watcher``), plus
   ``TACIT_ROLE`` / ``TACIT_SESSION_ID`` in its environment. On Windows the
   command line is the only reliable identifier — the exe name is not — so the
   marker lives in argv, and every sweep matches the marker, never the exe.

2. **Job-object containment.** On Windows every child is assigned to a Job
   Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: if the parent crashes or
   is hard-killed, the kernel closes the job handle and the children die with
   it. try/finally cleanup cannot do this — it does not run on a hard kill.

The parent also keeps a live registry ({pid, role, session, started, cmdline})
in memory and persisted under the state dir, so ``GET /api/processes`` can show
what is running and the startup sweep can find orphans from a previous crash.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config

# The argv markers. They sit right after the interpreter, so a command line
# like `python.exe tacit-turn-worker -m backend.turn_worker ...` is ours even
# when every other field is identical to someone else's python.
ROLE_WORKER = "tacit-turn-worker"
ROLE_WATCHER = "tacit-watcher"
ROLE_SERVER = "tacit-server"

MARKERS = (ROLE_WORKER, ROLE_WATCHER, ROLE_SERVER)

REGISTRY_FILE = "processes.json"

# ── job objects (Windows) ─────────────────────────────────────────────────
# No new dependency: the three calls we need are declared against the ctypes
# prototypes. A job object is a kernel container; KILL_ON_JOB_CLOSE means the
# last handle closing kills everything in the job — which is exactly the
# orphan guarantee try/finally cannot give, because it does not run on a hard
# kill of the parent.
def _job_available() -> bool:
    return os.name == "nt"


if os.name == "nt":
    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _kernel32.SetInformationJobObject.restype = ctypes.c_bool
    _kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.AssignProcessToJobObject.restype = ctypes.c_bool
    _kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _kernel32.CloseHandle.restype = ctypes.c_bool
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    JobObjectExtendedLimitInformation = 9  # JobObjectExtendedLimitInformationClass
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    _JOB_LIMITS = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    _JOB_LIMITS.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def _assign_job(proc) -> None:
    """Put the child in a kill-on-close job. Best effort: a failure to contain
    is logged, never fatal — the marker and registry still identify it."""
    if not _job_available() or proc is None:
        return
    try:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(limits), ctypes.sizeof(limits)):
            _kernel32.CloseHandle(job)
            return
        # Popen stores the process handle on Windows; hProcess is the kernel
        # handle AssignProcessToJobObject wants.
        if not _kernel32.AssignProcessToJobObject(job, int(proc._handle)):
            _kernel32.CloseHandle(job)
            return
        # The job's own handle must stay open for the life of the child: the
        # kill-on-close fires when the LAST handle closes, so we keep ours.
        proc._tacit_job = job
    except Exception:
        pass


def _release_job(proc) -> None:
    """Close the job handle deliberately (normal reaping). The child is
    already gone, so the kill-on-close has nothing to kill."""
    job = getattr(proc, "_tacit_job", None)
    if job:
        try:
            _kernel32.CloseHandle(job)
        except Exception:
            pass
        proc._tacit_job = None


# ── the registry ──────────────────────────────────────────────────────────
# In-memory rows, persisted so a crash leaves a record the next boot sweeps.
_ROWS: dict[int, dict] = {}
_LOCK = None  # created lazily; threading is imported lazily to keep import cheap


def _lock():
    global _LOCK
    if _LOCK is None:
        import threading
        _LOCK = threading.RLock()
    return _LOCK


def _path() -> Path:
    return config.HOME / REGISTRY_FILE


def _load() -> dict:
    try:
        data = config.read_json(_path(), {})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _persist() -> None:
    try:
        config.write_json(_path(), {"rows": list(_ROWS.values())})
    except Exception:
        pass


def register(role: str, pid: int, session: str = "", cmdline: str = "") -> dict:
    """Record a child. Called by the parent right after a successful spawn."""
    row = {"pid": int(pid), "role": str(role), "session": str(session or ""),
           "started": time.time(), "cmdline": str(cmdline or "")}
    with _lock():
        _ROWS[int(pid)] = row
        _persist()
    return row


def unregister(pid: int) -> None:
    with _lock():
        _ROWS.pop(int(pid), None)
        _persist()


def rows() -> list[dict]:
    """The live rows, pruned of the provably dead.

    unregister() is the normal exit — reap() calls it — but a process that
    dies without its parent reaping it (a crash, a kill from outside) would
    otherwise haunt every listing until the next server restart. A read is
    the one moment someone is actually looking, so the dead are dropped
    there too: only rows whose pid is provably gone are removed, and a pid
    that cannot be checked is left alone. The lock is held throughout, so
    a concurrent register cannot resurrect a dropped pid's row.
    """
    with _lock():
        dead = [pid for pid, row in _ROWS.items()
                if pid != os.getpid() and not _pid_alive(pid)]
        for pid in dead:
            _ROWS.pop(pid, None)
        if dead:
            _persist()
        return [dict(r) for r in _ROWS.values()]


def _pid_alive(pid: int) -> bool:
    """Is this pid running right now? Cheap check, no external tools."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE are implied by
        # the SYNCHRONIZE right we actually need for WaitForSingleObject.
        SYNCHRONIZE = 0x00100000
        h = _kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not h:
            return False
        try:
            WAIT_OBJECT_0 = 0
            WAIT_TIMEOUT = 0x00000102
            code = _kernel32.WaitForSingleObject(h, 0)
            return code == WAIT_TIMEOUT  # still running
        finally:
            _kernel32.CloseHandle(h)
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, not ours to signal


def _pid_cmdline(pid: int) -> str:
    """The process's current command line, or '' when it cannot be read.

    On Windows this is the only reliable identity check; on POSIX /proc does
    the same job. Empty string means "cannot prove it is ours" — and the sweep
    treats cannot-prove as leave-alone.
    """
    if os.name == "nt":
        try:
            import ctypes.wintypes
            k32 = _kernel32
            # NtQueryInformationProcess would be the direct route; going
            # through PowerShell is slower but dependency-free and honest
            # about failure. The sweep is a startup path, not a hot loop.
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}')"
                 ".CommandLine"],
                capture_output=True, text=True, timeout=20)
            return (out.stdout or "").strip()
        except Exception:
            return ""
    else:
        try:
            with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
                return fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except Exception:
            return ""


def sweep() -> dict:
    """On startup: drop dead rows, kill proven orphans, keep live rows.

    An orphan is a pid that is alive, whose CURRENT command line still
    contains one of our markers (proving it is ours and guarding against pid
    reuse), and which this boot did not register. Anything else — dead, or
    alive but unprovable — is left alone. Never matches on the exe name.
    """
    killed: list[dict] = []
    dropped: list[dict] = []
    kept: list[dict] = []
    me = os.getpid()
    with _lock():
        stored = _load().get("rows") or []
        fresh = {r.get("pid") for r in _ROWS.values()}
        for row in stored:
            pid = int(row.get("pid") or 0)
            if pid == me:
                # This boot's own registration from a previous life (the
                # restart flow writes the replacement server's row before the
                # server boots). Killing ourselves here would make every
                # restart a crash loop.
                kept.append(row)
                continue
            if pid in fresh:
                kept.append(row)  # registered this boot; not an orphan
                continue
            if not _pid_alive(pid):
                dropped.append(row)  # died with its parent; just bookkeeping
                continue
            cmdline = _pid_cmdline(pid)
            if any(m in cmdline for m in MARKERS):
                # Ours, from a previous crash. Kill it.
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                                       capture_output=True, timeout=15)
                    else:
                        import signal
                        os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
                killed.append(row)
            else:
                # Alive but we cannot prove it is ours (pid reuse, or the
                # command line is unreadable). Leave it alone.
                kept.append(row)
        _ROWS.clear()
        for row in kept:
            _ROWS[int(row["pid"])] = row
        _persist()
    return {"killed": killed, "dropped": dropped, "kept": len(kept)}


# ── spawn ─────────────────────────────────────────────────────────────────
def _marked_argv(argv: list[str], role: str) -> list[str]:
    """Insert the role marker where Python's grammar allows it.

    NOT argv[:1] + [role] + argv[1:]: for `python -m backend.turn_worker`
    that puts the marker before `-m`, and Python reads `tacit-turn-worker` as
    a script file to run — the worker dies instantly with "can't open file"
    and the turn produces nothing. And NOT between `-m` and the module name:
    there it would BE the module name. The marker goes after the complete
    interpreter spec — `-m module` or `-c code` — as the first argument the
    script itself sees, which is still the front of the command line a human
    scans, and is ignored by everything Tacit runs.
    """
    argv = list(argv)
    if len(argv) >= 3 and argv[1] == "-m":
        # python -m backend.turn_worker ...  ->  python -m backend.turn_worker tacit-turn-worker ...
        return argv[:3] + [role] + argv[3:]
    if len(argv) >= 3 and argv[1] == "-c":
        # python -c "<code>" tacit-turn-worker ...  — the marker rides as
        # sys.argv[1] of the -c script, which never reads it.
        return argv[:3] + [role] + argv[3:]
    return argv[:1] + [role] + argv[1:]


def spawn(role: str, argv: list[str], *, session: str = "", cwd: str | None = None,
          contain: bool = True, mark: bool = True, **popen_kwargs) -> subprocess.Popen:
    """Spawn a marked, contained, registered child.

    The marker goes right after the interpreter so the command line identifies
    the process at a glance; TACIT_ROLE / TACIT_SESSION_ID ride the
    environment; the Windows job object makes the child die with the parent;
    the registry row makes it visible and sweepable.

    ``contain=False`` is for the two children that must OUTLIVE their spawner
    by design — the restart watcher and the server it starts. A kill-on-close
    job would kill them the moment the spawner's handle closed, which is the
    opposite of what a restart needs. They keep the marker and the registry
    row, so they stay identifiable and sweepable.

    ``mark=False`` is for children whose argv is the user's own command —
    agent-launched background jobs. A marker token would corrupt that command,
    so they are identified by the env vars and the registry row alone, and the
    sweep deliberately leaves them alone (it only ever kills a pid whose
    command line proves it is ours). With ``mark=False`` the argv may be a raw
    string: passing a list to ``shell=True`` on Windows re-quotes it through
    list2cmdline, which misparses commands that contain their own quotes —
    the string form goes to cmd.exe untouched, exactly as the old bg_start
    passed it.
    """
    env = dict(popen_kwargs.pop("env", None) or os.environ)
    env["TACIT_ROLE"] = role
    if session:
        env["TACIT_SESSION_ID"] = str(session)
    else:
        env.pop("TACIT_SESSION_ID", None)
    if not mark and isinstance(argv, str):
        # The user's command, verbatim.
        proc = subprocess.Popen(argv, cwd=cwd, env=env, **popen_kwargs)
        proc._tacit_role = role
        if contain:
            _assign_job(proc)
        register(role, proc.pid, session=session, cmdline=argv)
        _log(f"spawned {role} pid={proc.pid} session={session or '-'}")
        return proc
    argv = list(argv)
    if not argv:
        raise ValueError("empty argv")
    marked = _marked_argv(argv, role) if mark else list(argv)
    proc = subprocess.Popen(marked, cwd=cwd, env=env, **popen_kwargs)
    proc._tacit_role = role
    if contain:
        _assign_job(proc)
    register(role, proc.pid, session=session, cmdline=" ".join(marked))
    _log(f"spawned {role} pid={proc.pid} session={session or '-'}")
    return proc


def _log(text: str) -> None:
    try:
        sys.stderr.write(f"[proctools] {text}\n")
        sys.stderr.flush()
    except Exception:
        pass


def reap(proc, role: str = "") -> None:
    """Record the exit and release the job handle. Best effort throughout."""
    try:
        role = role or (proc._tacit_role if hasattr(proc, "_tacit_role") else "")
    except Exception:
        pass
    try:
        unregister(proc.pid)
    except Exception:
        pass
    try:
        _release_job(proc)
    except Exception:
        pass
    _log(f"reaped {role} pid={getattr(proc, 'pid', '?')}")
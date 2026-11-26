"""Process identity and containment for everything Tacit spawns.

The problem this module exists for: during a stuck test run, ~23 ``python.exe``
processes sat in the task list and none of them could be attributed, Tacit's
children look like any other Python process, and the user runs other Python
projects on the same machine, so nothing could be killed safely. Two fixes:

1. **Identity at spawn.** Every child gets a recognizable token in argv right
   after the interpreter (``tacit-turn-worker``, ``tacit-watcher``), plus
   ``TACIT_ROLE`` / ``TACIT_SESSION_ID`` in its environment. On Windows the
   command line is the only reliable identifier (the exe name is not) so the
   marker lives in argv, and every sweep matches the marker, never the exe.

2. **Job-object containment.** On Windows every child is assigned to a Job
   Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: if the parent crashes or
   is hard-killed, the kernel closes the job handle and the children die with
   it. try/finally cleanup cannot do this, it does not run on a hard kill.

The parent also keeps a live registry ({pid, role, session, started, cmdline,
owner, identity, pgid}) in memory and persisted under the state dir, one file
per owning process so the server and its workers never clobber each other's
rows. ``GET /api/processes`` shows what is running, the startup sweep finds
orphans from a previous crash, and the parent settles a dead worker's shell
groups by the identity its rows carry. A persisted pid is only ever killed
after its birth identity (creation time on Windows, /proc start time on Linux)
proves it is still the process that was registered; a reused pid is a stranger.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config

# The argv markers. They sit right after the interpreter, so a command line
# like `python.exe tacit-turn-worker -m backend.turn_worker ...` is ours even
# when every other field is identical to someone else's python.
ROLE_WORKER = "tacit-turn-worker"
ROLE_ASSISTANT = "tacit-assistant-worker"
ROLE_WATCHER = "tacit-watcher"
ROLE_SERVER = "tacit-server"

MARKERS = (ROLE_WORKER, ROLE_ASSISTANT, ROLE_WATCHER, ROLE_SERVER)

REGISTRY_FILE = "processes.json"

# ── job objects (Windows) ─────────────────────────────────────────────────
# No new dependency: the three calls we need are declared against the ctypes
# prototypes. A job object is a kernel container; KILL_ON_JOB_CLOSE means the
# last handle closing kills everything in the job, which is exactly the
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
    _kernel32.TerminateJobObject.restype = ctypes.c_bool
    _kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _kernel32.CloseHandle.restype = ctypes.c_bool
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    _kernel32.GetProcessTimes.restype = ctypes.c_bool
    _kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]

    JobObjectExtendedLimitInformation = 9  # JobObjectExtendedLimitInformationClass
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    _JOB_LIMITS = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    _JOB_LIMITS.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def _assign_job(proc) -> None:
    """Put the child in a kill-on-close job. Best effort: a failure to contain
    is logged, never fatal, the marker and registry still identify it.

    ``TACIT_DISABLE_JOB_OBJECTS=1`` forces the assignment to fail, the same
    escape-hatch shape as ``TACIT_TURN_WORKER``: it exists so the conformance
    suite can prove what the shell supervisor reports when containment is
    unavailable, instead of only testing the happy path.
    """
    if not _job_available() or proc is None:
        return
    if os.environ.get("TACIT_DISABLE_JOB_OBJECTS", "").strip().lower() in ("1", "true", "yes", "on"):
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


def _dbg_signal(site, pid, how, ok, error=""):
    try:
        from . import debuglog
        debuglog.signal_sent(site, pid, how, ok, error)
    except Exception:  # noqa: BLE001
        pass


def _dbg_refused(site, pid, reason):
    try:
        from . import debuglog
        debuglog.signal_refused(site, pid, reason)
    except Exception:  # noqa: BLE001
        pass


def terminate_tree(proc) -> bool:
    """Terminate a contained process tree, even after its root has exited.

    A Windows Job Object remains the containment boundary after the root process
    disappears. Falling straight to taskkill in that case loses the descendants
    that still hold the command's pipes. POSIX shells owned by the sandbox have
    their own process group, so their pgid is the equivalent boundary.
    """
    if proc is None:
        return True
    try:
        if os.name == "nt":
            job = getattr(proc, "_tacit_job", None)
            if job and _kernel32.TerminateJobObject(job, 1):
                return True
            if proc.poll() is None:
                result = subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True, timeout=15)
                return result.returncode == 0
            return False
        if getattr(proc, "_tacit_group", False):
            # start_new_session makes the root pid the durable pgid. Looking it
            # up through the root would fail precisely when only children remain.
            os.killpg(proc.pid, signal.SIGKILL)
            _dbg_signal("proctools.terminate_tree", proc.pid, "killpg SIGKILL", True)
        elif proc.poll() is None:
            os.kill(proc.pid, signal.SIGKILL)
            _dbg_signal("proctools.terminate_tree", proc.pid, "kill SIGKILL", True)
        return True
    except ProcessLookupError:
        return True
    except Exception:
        return False


def _release_job(proc) -> None:
    """Close the job handle deliberately after execution has settled."""
    job = getattr(proc, "_tacit_job", None)
    if job:
        try:
            _kernel32.CloseHandle(job)
        except Exception:
            pass
        proc._tacit_job = None


# ── the registry ──────────────────────────────────────────────────────────
# In-memory rows for this process's own children, persisted so a crash leaves
# a record the next boot sweeps. One file per owning process: the server and
# its turn workers all register children, and a single shared file would have
# every writer clobber the other writers' rows on each persist. A worker's
# shells are therefore visible to the server (the listing merges the files)
# and cleanable by the server when the worker dies hard.
_ROWS: dict[int, dict] = {}
_LOCK = None  # created lazily; threading is imported lazily to keep import cheap

ROLE_SHELL = "tacit-shell"
ROLE_BG = "tacit-bg"


def _lock():
    global _LOCK
    if _LOCK is None:
        import threading
        _LOCK = threading.RLock()
    return _LOCK


def _path(pid: int | None = None) -> Path:
    owner = os.getpid() if pid is None else int(pid)
    return config.HOME / f"processes-{owner}.json"


def _legacy_path() -> Path:
    return config.HOME / REGISTRY_FILE


def _owner_paths() -> list[Path]:
    try:
        return sorted(config.HOME.glob("processes-*.json"))
    except OSError:
        return []


def _load_file(path: Path) -> list[dict]:
    try:
        data = config.read_json(path, {})
    except Exception:  # noqa: BLE001
        return []
    rows = data.get("rows") if isinstance(data, dict) else None
    # Generic: the process registry rows carry a pid, the container ledger
    # rows carry a name; the shape check is the dict, not either key.
    return [r for r in (rows or []) if isinstance(r, dict)]


def _persist() -> None:
    try:
        config.write_json(_path(), {"rows": list(_ROWS.values())})
    except Exception:  # noqa: BLE001
        pass


def register(role: str, pid: int, session: str = "", cmdline: str = "",
             pgid: int | None = None, job: bool | None = None) -> dict:
    """Record a child. Called by the parent right after a successful spawn.

    The row carries the owner (this process), the process's birth identity
    (creation/start time, where the platform exposes one) and, on POSIX, the
    process group when the child leads one. Identity is what lets a later
    process prove a persisted pid is still the process it registered before
    killing it: a reused pid has a different birth. ``job`` records whether
    Windows containment actually took, so a later cleanup can tell a tree the
    kernel already took down from one it cannot reach.
    """
    row = {"pid": int(pid), "role": str(role), "session": str(session or ""),
           "started": time.time(), "cmdline": str(cmdline or ""),
           "owner": os.getpid()}
    identity = process_identity(int(pid))
    if identity is not None:
        row["identity"] = identity
    if pgid:
        row["pgid"] = int(pgid)
    if job is not None:
        row["job"] = bool(job)
    with _lock():
        _ROWS[int(pid)] = row
        _persist()
    return row


def unregister(pid: int) -> None:
    pid = int(pid)
    with _lock():
        if pid in _ROWS:
            _ROWS.pop(pid, None)
            _persist()
            return
    # A row owned by another process (the launcher clearing a server row it
    # found in the merged listing). The owner is dead or being killed by the
    # caller, so rewriting its file cannot race a live writer's persist.
    for path in _owner_paths():
        rows = _load_file(path)
        kept = [r for r in rows if int(r.get("pid") or 0) != pid]
        if len(kept) != len(rows):
            try:
                config.write_json(path, {"rows": kept})
            except Exception:  # noqa: BLE001
                pass


def _row_alive(row: dict) -> bool:
    """A row is alive while its kill boundary is alive, not merely its root.

    A POSIX shell whose root exited can still have a live group holding the
    command's pipes; the group is the boundary the supervisor kills, so the
    group is what the listing and the sweep must judge.
    """
    pid = int(row.get("pid") or 0)
    if os.name != "nt" and row.get("pgid"):
        try:
            return group_alive(int(row["pgid"]))
        except (TypeError, ValueError):
            pass
    return _pid_alive(pid)


def rows() -> list[dict]:
    """The live rows, pruned of the provably dead.

    unregister() is the normal exit (reap() calls it) but a process that
    dies without its parent reaping it (a crash, a kill from outside) would
    otherwise haunt every listing until the next server restart. A read is
    the one moment someone is actually looking, so the dead are dropped
    there too: only rows whose pid is provably gone are removed, and a pid
    that cannot be checked is left alone. The lock is held throughout, so
    a concurrent register cannot resurrect a dropped pid's row.

    The answer merges this process's rows with the files other owners left
    (a worker's shells belong in the server's listing), and reads the legacy
    single-file registry for rows written before the per-owner split. Other
    owners' files are not rewritten here; their dead rows are pruned from the
    answer only, and the sweep settles the files themselves.
    """
    with _lock():
        dead = [pid for pid, row in _ROWS.items()
                if pid != os.getpid() and not _pid_alive(pid)]
        for pid in dead:
            _ROWS.pop(pid, None)
        if dead:
            _persist()
        out = {pid: dict(r) for pid, r in _ROWS.items()}
    me = os.getpid()
    own = _path()
    for path in _owner_paths() + [_legacy_path()]:
        if path == own:
            continue
        for row in _load_file(path):
            pid = int(row.get("pid") or 0)
            if pid <= 0 or pid == me or pid in out:
                continue
            if _row_alive(row):
                out[pid] = dict(row)
    return list(out.values())


def _proc_state(pid: int) -> str:
    """The POSIX process state letter ('R', 'S', 'Z', ...), or '' unreadable.

    The comm field is parenthesized and may itself contain spaces and ')', so
    the split starts after the LAST ')' rather than at a fixed offset.
    """
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            data = fh.read()
        fields = data[data.rfind(b")") + 2:].split()
        return fields[0].decode() if fields else ""
    except Exception:  # noqa: BLE001
        return ""


def _pid_alive(pid: int) -> bool:
    """Is this pid running right now? Cheap check, no external tools.

    A zombie is not running: it is a dead process whose exit status nobody has
    collected. ``kill(pid, 0)`` succeeds on zombies, which read a killed child
    as alive forever when its parent exited without waiting (the cross-process
    background-job case). Where /proc exists the state letter settles it; on
    macOS there is no /proc and the signal probe remains the whole answer.
    """
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
    if _proc_state(pid) == "Z":
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours to signal


def process_identity(pid: int):
    """A birth certificate for a pid, or None where it cannot be read.

    Windows: the process creation time (FILETIME, 100ns ticks). Linux: the
    start time from /proc (clock ticks since boot). Both are fixed for the
    life of the process, so a persisted row can prove the pid it names is
    still the process it registered before anything kills it: a reused pid
    has a different birth. macOS has neither interface here and returns None,
    which callers must treat as "cannot prove", not as "match".
    """
    if pid <= 0:
        return None
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            import ctypes.wintypes
            creation = ctypes.wintypes.FILETIME()
            exit_t = ctypes.wintypes.FILETIME()
            kernel_t = ctypes.wintypes.FILETIME()
            user_t = ctypes.wintypes.FILETIME()
            if not _kernel32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_t),
                                             ctypes.byref(kernel_t), ctypes.byref(user_t)):
                return None
            return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        except Exception:  # noqa: BLE001
            return None
        finally:
            _kernel32.CloseHandle(h)
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            data = fh.read()
        fields = data[data.rfind(b")") + 2:].split()
        # starttime is field 22 overall; state (field 3) is index 0 here.
        return int(fields[19])
    except Exception:  # noqa: BLE001
        return None


def group_alive(pgid: int) -> bool:
    """POSIX: does this process group still have a living (non-zombie) member?

    ``killpg(pgid, 0)`` is not that question: signal 0 succeeds on zombies, so
    a group whose members were killed but never reaped reads alive forever.
    The stop path polls this after SIGKILL; counting the unreaped dead as
    running turned every cross-process stop into a false failure.
    """
    if os.name == "nt" or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours to signal
    except OSError:
        return False
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True  # no /proc (macOS): the signal probe is the whole answer
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                data = fh.read()
            fields = data[data.rfind(b")") + 2:].split()
            # state, ppid, pgrp are fields 3, 4, 5: indices 0, 1, 2 here.
            if len(fields) >= 3 and int(fields[2]) == pgid and fields[0] != b"Z":
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def _pid_cmdline(pid: int) -> str:
    """The process's current command line, or '' when it cannot be read.

    On Windows this is the only reliable identity check; on POSIX /proc does
    the same job. Empty string means "cannot prove it is ours", and the sweep
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
                stdin=subprocess.DEVNULL,
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


def kill_tree(pid: int) -> bool:
    """Hard-stop a process and everything under it, by pid.

    Only ever called on a pid whose *current* command line has been proved to
    carry one of our markers, that proof is what makes a tree kill safe, and it
    is why nothing here looks at the exe name. The Windows call keeps `taskkill`
    on the inherited-stdin allowlist (tests/test_shell_stdin.py): it is a Tacit
    tool call, not an agent command, and it takes no input.
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))],
                           capture_output=True, timeout=15)
        else:
            import signal
            os.kill(int(pid), signal.SIGKILL)
        _dbg_signal("proctools.kill_tree", pid, "tree kill", True)
        return True
    except Exception as e:  # noqa: BLE001
        _dbg_signal("proctools.kill_tree", pid, "tree kill", False, str(e))
        return False


def kill_shell_group(row: dict) -> bool:
    """Kill a registered shell's containment boundary, with identity proof.

    POSIX: the group id is the root pid (start_new_session). While a process
    holds that pid it must be the registered root, proved by its birth
    identity, or the kill is refused: the pid may have been reused by a
    stranger who now leads a group with the same number. With no process
    holding the pid no new leader can exist, and killpg reaches only the old
    group's members. A row with no readable identity (macOS) is refused while
    its leader lives, which is the conservative direction.

    Windows: the job object is the boundary, and it dies with the worker that
    holds its handle. A live root without a job (assignment failed) is killed
    through taskkill /T after the same identity proof; a dead root with no job
    may have left descendants nothing can reach, and that is reported as a
    failure, not a success.
    """
    pid = int(row.get("pid") or 0)
    if pid <= 0:
        return False
    identity = row.get("identity")
    if os.name == "nt":
        if not _pid_alive(pid):
            return bool(row.get("job"))
        if identity is not None and process_identity(pid) != identity:
            return False
        try:
            result = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                                    capture_output=True, timeout=15)
            return result.returncode == 0
        except Exception:  # noqa: BLE001
            return False
    pgid = int(row.get("pgid") or pid)
    if _pid_alive(pgid):
        if identity is None or process_identity(pgid) != identity:
            _dbg_refused("proctools.kill_shell_group", pgid,
                         "identity of the group leader is not the registered one")
            return False
    try:
        os.killpg(pgid, signal.SIGKILL)
        _dbg_signal("proctools.kill_shell_group", pgid, "killpg SIGKILL", True)
        return True
    except ProcessLookupError:
        return True
    except Exception as e:  # noqa: BLE001
        _dbg_signal("proctools.kill_shell_group", pgid, "killpg SIGKILL",
                    False, str(e))
        return False


def kill_container_row(row: dict) -> bool:
    """Stop a registered container by name and verify the runtime agrees.

    The name is the identity: it is minted per command (tacit-<ms>-<pid>) and
    the pattern is checked before anything is sent to the runtime, so a
    malformed or foreign row can never name somebody else's container.
    """
    import re
    import shutil
    name = str(row.get("name") or row.get("container") or "")
    if not re.fullmatch(r"tacit-\d+-\d+", name):
        return False
    runtime = shutil.which("docker") or shutil.which("podman")
    if not runtime:
        return False
    try:
        subprocess.run([runtime, "kill", name], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=20)
        probe = subprocess.run([runtime, "inspect", "--format", "{{.State.Running}}", name],
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=20)
        if probe.returncode != 0:
            return True  # the runtime no longer knows it (--rm took it)
        return probe.stdout.strip() != "true"
    except Exception:  # noqa: BLE001
        return False


# ── the container ledger ──────────────────────────────────────────────────
# Containers outlive the client that started them: `docker run` dying leaves
# the container running. The name is registered here BEFORE the run starts,
# keyed by the owning worker, so the parent (or the next boot's sweep) can
# stop what a dead worker left behind. A separate ledger, not the pid
# registry: the container's identity is its name, and its client's pid is
# already registered as a shell row.
CONTAINERS_FILE = "containers.json"


def _containers_path() -> Path:
    return config.HOME / CONTAINERS_FILE


def register_container(name: str, owner: int, session: str = "") -> None:
    with _lock():
        rows = [r for r in _load_file(_containers_path()) if r.get("name") != name]
        rows.append({"name": str(name), "owner": int(owner),
                     "session": str(session or ""), "started": time.time()})
        try:
            config.write_json(_containers_path(), {"rows": rows})
        except Exception:  # noqa: BLE001
            pass


def unregister_container(name: str) -> None:
    with _lock():
        rows = _load_file(_containers_path())
        kept = [r for r in rows if r.get("name") != name]
        if len(kept) != len(rows):
            try:
                config.write_json(_containers_path(), {"rows": kept})
            except Exception:  # noqa: BLE001
                pass


def cleanup_owner_containers(owner_pid: int) -> list[dict]:
    """Stop the containers a dead owner registered; keep what cannot be proven."""
    stopped: list[dict] = []
    with _lock():
        rows = _load_file(_containers_path())
        kept = []
        for row in rows:
            if int(row.get("owner") or 0) != int(owner_pid):
                kept.append(row)
                continue
            if kill_container_row(row):
                stopped.append(row)
            else:
                kept.append(row)
        try:
            if kept:
                config.write_json(_containers_path(), {"rows": kept})
            else:
                _containers_path().unlink(missing_ok=True)
        except OSError:
            pass
    return stopped


def sweep_containers() -> dict:
    """The startup half of the ledger: a container whose owner is dead is an
    orphan; one whose owner lives belongs to that owner. Unprovable rows stay."""
    killed: list[dict] = []
    me = os.getpid()
    with _lock():
        rows = _load_file(_containers_path())
        kept = []
        for row in rows:
            owner = int(row.get("owner") or 0)
            if owner == me or (owner and _pid_alive(owner)):
                kept.append(row)
                continue
            if kill_container_row(row):
                killed.append(row)
            else:
                kept.append(row)
        try:
            if kept:
                config.write_json(_containers_path(), {"rows": kept})
            else:
                _containers_path().unlink(missing_ok=True)
        except OSError:
            pass
    return {"killed": killed, "kept": len(kept)}


def cleanup_owner_shells(owner_pid: int) -> dict:
    """Settle the shell groups and containers a dead owner left behind.

    Called by the parent when its worker dies, by any exit path: a cooperative
    cancel already killed and reaped the shells, so this is normally a no-op;
    a hard kill skips the worker's finally, and the rows it registered are the
    only remaining proof of what it was running. Background-job rows are left
    alone: they are session-owned and deliberately outlive the turn.
    """
    report: dict = {"killed": [], "refused": [], "containers": []}
    report["containers"] = cleanup_owner_containers(owner_pid)
    path = _path(owner_pid)
    rows = _load_file(path)
    if not rows:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return report
    kept = []
    for row in rows:
        role = str(row.get("role") or "")
        if role == ROLE_SHELL:
            if kill_shell_group(row):
                report["killed"].append(row)
            else:
                report["refused"].append(row)
                kept.append(row)
        elif role == ROLE_BG:
            # Background jobs are session-owned and deliberately outlive the
            # turn; the jobs registry under JOBS_DIR is their authoritative
            # record, so the process row is dropped, not killed.
            continue
        else:
            kept.append(row)
    try:
        if kept:
            config.write_json(path, {"rows": kept})
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass
    return report


def sweep() -> dict:
    """On startup: drop dead rows, kill proven orphans, keep live rows.

    Every owner's file is read, plus the legacy single-file registry. A row
    whose owner is still alive belongs to that owner, not to the sweep. An
    orphan is judged by what it is:

    - a marked Tacit process (worker, assistant, watcher, server): its CURRENT
      command line must carry one of our markers (proving it is ours and
      guarding against pid reuse), then kill_tree. Never matches on the exe
      name;
    - a shell row: killed only through its own identity-proof path
      (kill_shell_group), because a shell's argv is the user's command and
      carries no marker;
    - containers are settled from their own ledger (sweep_containers), by
      name, with the runtime confirming each stop;
    - anything else, dead, or alive but unprovable: left alone.
    """
    killed: list[dict] = []
    dropped: list[dict] = []
    kept: list[dict] = []
    me = os.getpid()
    with _lock():
        fresh = set(_ROWS)
        sources = [(p, _load_file(p)) for p in _owner_paths()]
        legacy_rows = _load_file(_legacy_path())
        if legacy_rows:
            sources.append((_legacy_path(), legacy_rows))
        for path, rows in sources:
            remaining: list[dict] = []
            for row in rows:
                pid = int(row.get("pid") or 0)
                owner = int(row.get("owner") or 0)
                if pid == me:
                    # This boot's own registration from a previous life (the
                    # restart flow writes the replacement server's row before
                    # the server boots). Killing ourselves here would make
                    # every restart a crash loop.
                    remaining.append(row)
                    continue
                if pid in fresh:
                    remaining.append(row)  # registered this boot; not an orphan
                    continue
                if owner and owner != me and _pid_alive(owner):
                    remaining.append(row)  # its owner is alive and manages it
                    continue
                role = str(row.get("role") or "")
                if role == ROLE_BG:
                    # The jobs registry under JOBS_DIR owns background jobs;
                    # a dead owner's process row is bookkeeping, not an orphan.
                    dropped.append(row)
                    continue
                if not _row_alive(row):
                    dropped.append(row)  # died with its parent; bookkeeping
                    continue
                if role == ROLE_SHELL:
                    if kill_shell_group(row):
                        killed.append(row)
                    else:
                        remaining.append(row)  # unproven: leave it alone
                    continue
                cmdline = _pid_cmdline(pid)
                if any(m in cmdline for m in MARKERS):
                    kill_tree(pid)
                    killed.append(row)
                else:
                    remaining.append(row)
            if path == _path():
                for row in remaining:
                    _ROWS.setdefault(int(row["pid"]), row)
            else:
                try:
                    if remaining or path == _legacy_path():
                        config.write_json(path, {"rows": remaining})
                    else:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
                kept.extend(remaining)
        _persist()
    containers = sweep_containers()
    return {"killed": killed + containers["killed"], "dropped": dropped,
            "kept": len(kept) + containers["kept"]}


# ── spawn ─────────────────────────────────────────────────────────────────
def _marked_argv(argv: list[str], role: str) -> list[str]:
    """Insert the role marker where Python's grammar allows it.

    NOT argv[:1] + [role] + argv[1:]: for `python -m backend.turn_worker`
    that puts the marker before `-m`, and Python reads `tacit-turn-worker` as
    a script file to run, the worker dies instantly with "can't open file"
    and the turn produces nothing. And NOT between `-m` and the module name:
    there it would BE the module name. The marker goes after the complete
    interpreter spec (`-m module` or `-c code`) as the first argument the
    script itself sees, which is still the front of the command line a human
    scans, and is ignored by everything Tacit runs.
    """
    argv = list(argv)
    if len(argv) >= 3 and argv[1] == "-m":
        # python -m backend.turn_worker ...  ->  python -m backend.turn_worker tacit-turn-worker ...
        return argv[:3] + [role] + argv[3:]
    if len(argv) >= 3 and argv[1] == "-c":
        # python -c "<code>" tacit-turn-worker ...  - the marker rides as
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
    by design, the restart watcher and the server it starts. A kill-on-close
    job would kill them the moment the spawner's handle closed, which is the
    opposite of what a restart needs. They keep the marker and the registry
    row, so they stay identifiable and sweepable.

    ``mark=False`` is for children whose argv is the user's own command,
    agent-launched background jobs. A marker token would corrupt that command,
    so they are identified by the env vars and the registry row alone, and the
    sweep deliberately leaves them alone (it only ever kills a pid whose
    command line proves it is ours). With ``mark=False`` the argv may be a raw
    string: passing a list to ``shell=True`` on Windows re-quotes it through
    list2cmdline, which misparses commands that contain their own quotes,
    the string form goes to cmd.exe untouched, exactly as the old bg_start
    passed it.
    """
    env = dict(popen_kwargs.pop("env", None) or os.environ)
    env["TACIT_ROLE"] = role
    if session:
        env["TACIT_SESSION_ID"] = str(session)
    else:
        env.pop("TACIT_SESSION_ID", None)
    # A POSIX child started with start_new_session leads a group named by its
    # own pid; that group is the durable kill boundary, so the row records it.
    leads_group = os.name != "nt" and bool(popen_kwargs.get("start_new_session"))
    if not mark and isinstance(argv, str):
        # The user's command, verbatim.
        proc = subprocess.Popen(argv, cwd=cwd, env=env,
                          stdin=popen_kwargs.pop("stdin", subprocess.DEVNULL),
                          **popen_kwargs)
        proc._tacit_role = role
        if contain:
            _assign_job(proc)
        register(role, proc.pid, session=session, cmdline=argv,
                 pgid=proc.pid if leads_group else None,
                 job=bool(getattr(proc, "_tacit_job", None)) if os.name == "nt" else None)
        _log(f"spawned {role} pid={proc.pid} session={session or '-'}")
        return proc
    argv = list(argv)
    if not argv:
        raise ValueError("empty argv")
    marked = _marked_argv(argv, role) if mark else list(argv)
    proc = subprocess.Popen(marked, cwd=cwd, env=env,
                            stdin=popen_kwargs.pop("stdin", subprocess.DEVNULL),
                            **popen_kwargs)
    proc._tacit_role = role
    if contain:
        _assign_job(proc)
    register(role, proc.pid, session=session, cmdline=" ".join(marked),
             pgid=proc.pid if leads_group else None,
             job=bool(getattr(proc, "_tacit_job", None)) if os.name == "nt" else None)
    _log(f"spawned {role} pid={proc.pid} session={session or '-'}")
    return proc


def _log(text: str) -> None:
    try:
        from . import debuglog
        debuglog.line(text)
    except Exception:  # noqa: BLE001
        pass
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
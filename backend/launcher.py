"""The launch path — the same one for the Restart button and for a person.

Item 4's complaint in one line: the button started the server *itself*
(`python -m backend.main`, inheriting whatever environment the dying process
happened to have, with no console), while a human started it with run.bat /
run.sh. Two launch paths means the one you can read is not the one that runs,
and section 0.4 of the deleted plan recorded what that costs: four Tacit processes on
two interpreters in one chain, the pid file naming a process that is not the one
holding the port. So the scripts are authoritative now: they stop every Tacit
they can *prove* is theirs, wait for the port to free, then start the server in
the foreground. The button hands the watcher the same script.

Why a module instead of batch logic in run.bat: proving ownership means the argv
marker, and reading a command line is a PowerShell round trip per pid plus the
registry file, code that has to behave identically on Windows, macOS and Linux,
and that a test can actually run. The scripts stay three lines of intent.

The rule that keeps a kill safe: a candidate comes from the registry, and it is
touched only when its *current* command line still carries one of Tacit's own
markers. Never the exe name, that is how `restart.ps1` used to be able to hit
an unrelated `python.exe`, and it is why this module can be pointed at a machine
running other people's Python.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import config, proctools

# The token a launcher passes to the server, and the token `proctools.spawn`
# inserts for anything it starts. `_marked_argv` puts it where the interpreter
# ignores it (after `-m backend.main`) so a server started by a script looks
# exactly like one started by the watcher, and one proof covers both.
SERVER_MARKER = proctools.ROLE_SERVER

# How long a launcher waits for a stopped server to let go of the port. Longer
# than the watcher's own wait would be a lie: the process was killed, not asked.
PORT_FREE_TIMEOUT = 20.0


def launcher_path(root: Path | None = None) -> Path | None:
    """The launcher script for this platform, or None when there is not one.

    Derived from config.ROOT, never hardcoded: the deleted plan's incident had a
    hand-written restart.ps1 naming C:\\Projects\\Tacit in two places, which is a
    mechanism that silently stops working the day the project moves.
    """
    root = Path(root if root is not None else config.ROOT)
    script = "run.bat" if config.OS == "Windows" else "run.sh"
    path = root / script
    return path if path.exists() else None


def find_running(roles: tuple[str, ...] = (proctools.ROLE_SERVER,),
                 exclude_pid: int | None = None) -> list[dict]:
    """Tacit processes we can prove are ours, and may therefore stop.

    Candidates are the registry rows, a process that never registered is not
    ours to hunt through the OS process list for. Each one is then *proved* by
    its current command line carrying a marker, which is what guards against pid
    reuse: a row whose pid now belongs to someone else's program reads as
    nothing of ours and is left alone.
    """
    found: list[dict] = []
    for row in proctools.rows():
        pid = int(row.get("pid") or 0)
        role = str(row.get("role") or "")
        if role not in roles or pid <= 0 or pid == (exclude_pid or os.getpid()):
            continue
        if not proctools._pid_alive(pid):
            continue
        cmdline = proctools._pid_cmdline(pid)
        if not any(m in cmdline for m in proctools.MARKERS):
            # Alive, registered, and unprovable. That is the case the sweep also
            # refuses to act on; saying so is the point of returning the row's
            # fields rather than a bare pid.
            continue
        found.append({"pid": pid, "role": role, "cmdline": cmdline})
    return found


def stop_all(roles: tuple[str, ...] = (proctools.ROLE_SERVER,)) -> dict:
    """Kill every provably-ours process in `roles`, tree, and report what ran."""
    stopped, failed = [], []
    for proc in find_running(roles=roles):
        ok = proctools.kill_tree(proc["pid"])
        (stopped if ok else failed).append(proc)
        if ok:
            # The row goes with the process: a dead pid in the registry is what
            # the next boot's sweep has to reason about for nothing.
            proctools.unregister(proc["pid"])
    return {"stopped": stopped, "failed": failed}


def port_free(host: str, port: int) -> bool:
    """Can a fresh listener take this port right now?

    The probe lives in extras (`_can_bind`) and is reused rather than copied:
    binding is the exact test the new server will face, and a connect against a
    live listener would fill its backlog and read as a free port that is not.
    Importing lazily because extras imports *this* module — a top-level import
    would be a cycle.
    """
    from . import extras
    return extras._can_bind(host, port)


def wait_port_free(host: str, port: int, timeout: float = PORT_FREE_TIMEOUT) -> bool:
    """Wait for the port to come free. False means it did not, not 'probably fine'."""
    end = time.time() + timeout
    while not port_free(host, port):
        if time.time() >= end:
            return False
        time.sleep(0.25)
    return True


def prelaunch(host: str | None = None, port: int | None = None,
              timeout: float = PORT_FREE_TIMEOUT) -> dict:
    """What a launcher script runs before it starts anything.

    Stop what is ours, then wait for the port. The result says which of the two
    happened and, when the port is still held, names the processes holding the
    registry, the information a person standing at a console needs, because the
    alternative is a second server that loses the bind race and vanishes.
    """
    host = config.HOST if host is None else host
    port = config.PORT if port is None else port
    out = stop_all()
    out["host"], out["port"] = host, port
    if not port:
        out["port_free"] = True      # no port named: nothing to wait for
    else:
        out["port_free"] = wait_port_free(host, port, timeout)
        if not out["port_free"]:
            out["waited_s"] = timeout
    out["ok"] = bool(out["port_free"])
    return out


# ── the scripts' entry point ──────────────────────────────────────────────
# run.bat / run.sh call `python -m backend.launcher prelaunch`. A batch file that
# tried to do this itself would need WMI queries and a platform branch per
# statement; this way the logic is in one place, is importable, and is testable.

USAGE = "usage: python -m backend.launcher [prelaunch|status]"


def main(argv: list[str] | None = None) -> int:
    """Run the launcher step and print what happened. 0 = safe to start."""
    argv = list(sys.argv[1:] if argv is None else argv)
    # The marker token rides in argv from the script line; ignore our own identity.
    args = [a for a in argv if a not in proctools.MARKERS]
    step = args[0] if args else "prelaunch"
    config.load_env()
    if step == "prelaunch":
        result = prelaunch()
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            print(f"a Tacit server is still holding port {result['port']}; "
                  "nothing was started", file=sys.stderr)
            return 1
        return 0
    if step == "status":
        print(json.dumps({"running": find_running(),
                          "launcher": str(launcher_path() or "")}, indent=2))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

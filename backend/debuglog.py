"""The process-activity log: `TACIT_DEBUG=on` answers who killed what.

A `Program '/bin/fish' crashed` warning with no coredump means a signal, not
a segfault - and the honest question is whose. This module writes a plain
line-a-time log to ~/.tacit/debug.log with everything the harness does that
can reach a process: every signal it sends (or refuses to send) through a
kill path, with the site that asked for it; every spawn and reap it records;
the identity of the shell that launched the server, and the exact moment
that shell died, with the last events around it. Off unless TACIT_DEBUG says
on (or ~/.tacit/debug.json carries {"on": true}); the log is capped so a
long-lived server cannot fill a disk with it.
"""

import atexit
import json
import os
import sys
import threading
import time

MAX_LINES = 20000

_ON = None
_lock = threading.Lock()
_buf: list = []
_file = None
_watch_started = False
_seq = 0


def enabled() -> bool:
    global _ON
    if _ON is None:
        raw = os.environ.get("TACIT_DEBUG", "").strip().lower()
        if raw in ("on", "1", "true", "yes"):
            _ON = True
        else:
            _ON = False
            try:
                from . import config
                data = json.loads((config.HOME / "debug.json")
                                  .read_text(encoding="utf-8"))
                _ON = bool(data.get("on"))
            except Exception:  # noqa: BLE001
                pass
    return bool(_ON)


def reset() -> None:
    """Forget the cached decision: tests, and a settings change at runtime."""
    global _ON, _file, _buf, _watch_started
    with _lock:
        if _file:
            try:
                _file.close()
            except Exception:  # noqa: BLE001
                pass
        _ON = None
        _file = None
        _buf = []
        _watch_started = False


def _target_info(pid) -> dict:
    out: dict = {}
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return out
    out["pid"] = pid
    try:
        out["pgid"] = os.getpgid(pid)
    except Exception:  # noqa: BLE001
        pass
    if os.name != "nt":
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                data = fh.read()
            fields = data[data.rfind(b")") + 2:].split()
            out["comm"] = data[data.find(b"(") + 1:data.rfind(b")")].decode(
                "utf-8", "replace")
            out["state"] = fields[0].decode("ascii", "replace")
        except Exception:  # noqa: BLE001
            pass
    return out


def log(event: str, **fields) -> None:
    """Record one process fact. Never raises: debugging must not break the
    thing it watches."""
    if not enabled():
        return
    global _file, _seq
    try:
        with _lock:
            _seq += 1
            try:
                from . import autoupdater
                version = autoupdater.local_version()
            except Exception:  # noqa: BLE001
                version = ""
            line = {"seq": _seq, "t": round(time.time(), 3), "event": event,
                    "server": os.getpid(), "version": version, **fields}
            _buf.append(line)
            if _file is None:
                try:
                    from . import config
                    config.HOME.mkdir(parents=True, exist_ok=True)
                    _file = open(config.HOME / "debug.log", "a",
                                 encoding="utf-8")
                except Exception:  # noqa: BLE001
                    _file = False
            if _file:
                _file.write(json.dumps(line, default=str) + "\n")
                _file.flush()
            if len(_buf) > MAX_LINES:
                del _buf[:len(_buf) - MAX_LINES]
    except Exception:  # noqa: BLE001
        pass


def signal_sent(path: str, target, how: str, ok: bool, error: str = "") -> None:
    """One signal actually sent (or attempted) by one named code path."""
    log("signal", site=path, **_target_info(target), how=how,
        ok=bool(ok), error=(error or "")[:120])


def signal_refused(path: str, target, reason: str) -> None:
    """One kill a code path decided against - and why."""
    log("signal-refused", site=path, **_target_info(target), reason=reason)


def line(text: str) -> None:
    """Mirror one proctools lifecycle line (spawns, reaps) into the log."""
    log("proc", line=str(text or "")[:200])


def _proc_state(pid: int):
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        fields = data[data.rfind(b")") + 2:].split()
        return (fields[0].decode("ascii", "replace"), int(fields[19]))
    except Exception:  # noqa: BLE001
        return None


def _watch(pid: int) -> None:
    """Poll the launching shell until it is gone, then write the moment and
    what the server was doing around it. Identity (starttime) guards pid
    reuse: a stranger wearing the old pid is also 'gone' for our purposes."""
    ident = None
    comm = ""
    try:
        st = _proc_state(pid)
        ident = st[1] if st else None
        with open(f"/proc/{pid}/comm", "rb") as fh:
            comm = fh.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        pass
    log("launch-watch", ppid=pid, comm=comm,
        identity=ident, our_pgid=_safe_pgid(0))
    last = "alive"
    while True:
        time.sleep(0.5)
        st = _proc_state(pid) if os.name != "nt" else (
            "alive" if _nt_alive(pid) else None)
        # A zombie is gone: the shell that exited but was never reaped is
        # exactly what a terminal whose child died leaves behind.
        if st is None or st[0] == "Z" or (
                ident and os.name != "nt" and st[1] != ident):
            with _lock:
                recent = list(_buf[-50:])
            log("launch-shell-gone", ppid=pid, comm=comm, last_state=last,
                our_pgid=_safe_pgid(0))
            for e in recent:
                if e["event"] in ("signal", "signal-refused"):
                    log("around-death", **{k: v for k, v in e.items()
                                           if k not in ("seq", "t", "event")})
            return
        last = st[0] if isinstance(st, tuple) else "alive"


def _nt_alive(pid: int) -> bool:
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok and code.value == 259)  # STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return True


def _safe_pgid(pid: int) -> int:
    try:
        return os.getpgid(pid)
    except Exception:  # noqa: BLE001
        return -1


def start() -> dict:
    """Begin logging; watch the shell that launched us. Idempotent."""
    global _watch_started
    if not enabled():
        return {"ok": False, "reason": "TACIT_DEBUG is not on"}
    if _watch_started:
        return {"ok": True, "path": str(_path_or_none())}
    _watch_started = True
    try:
        from . import config
    except Exception:  # noqa: BLE001
        config = None
    log("debug-boot", shell=(getattr(config, "SHELL", "") if config else ""),
        platform=os.name, pid=os.getpid(),
        pgid=_safe_pgid(0), ppid=os.getppid())
    if os.name != "nt":
        threading.Thread(target=_watch, args=(os.getppid(),),
                         daemon=True, name="tacit-launch-watch").start()
    return {"ok": True, "path": str(_path_or_none())}


def _path_or_none():
    try:
        from . import config
        return config.HOME / "debug.log"
    except Exception:  # noqa: BLE001
        return None


def _note_exit() -> None:
    try:
        log("server-exit", pid=os.getpid())
    except Exception:  # noqa: BLE001
        pass


atexit.register(reset)
atexit.register(_note_exit)  # runs BEFORE reset (LIFO): the note lands

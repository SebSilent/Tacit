"""The turn worker: one process per running turn, owned by its session.

The decision the plan records (Decision 2): a turn is a unit of work a person
must be able to kill, and a thread cannot be killed in Python under any
circumstance. A turn that is blocked in a model read, a stuck shell command or
a runaway regex holds nothing of the server hostage, because it is not running
in the server.

Lifecycle:

1. The session's socket handler spawns this module's worker as a subprocess
   (``python -m backend.turn_worker``) with the turn's parameters on argv and
   nothing secret anywhere in the environment it did not already have.
2. The worker runs the same ``agent.run_turn`` the in-process path ran, and
   streams its events to the parent as JSON lines on stdout.
3. Stop is two-stage. The parent first sends ``{"cancel": true}`` on the
   worker's stdin: the worker stops between events, closes its model stream,
   kills its own shell trees and exits cleanly, so the partial transcript is
   preserved. If it has not exited within the grace period, the parent kills
   the process tree outright — ``taskkill /T /F`` on Windows, ``killpg`` on
   POSIX. Stop is prompt in the normal case and guaranteed in the worst one.

The thread path (``TACIT_TURN_WORKER=thread``) is the old in-process
behaviour, kept as an escape hatch: the tests that fake the model engine
patch it into this process, which a worker subprocess cannot see.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, proctools

# Project root where the backend package lives
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# How long the parent waits for a clean cooperative exit before killing the
# tree. One salvage round is bounded by the same reasoning as guidance's
# EMERGENCY_S: long enough for a stream to notice its socket closed, short
# enough that "stop" never reads as "eventually".
CANCEL_GRACE_S = 5.0

# The worker's own hard ceiling, as a backstop for a cancel that never
# arrives. Generous: the turn's own step budget and shell timeouts are the
# real bounds; this only stops a worker that has lost its parent entirely.
WORKER_MAX_S = 3600.0


def worker_mode() -> str:
    return config.turn_worker_mode()


def _worker_command() -> list[str] | None:
    """This interpreter running this module, so the worker has the packages."""
    exe = Path(sys.executable)
    if not exe.name.lower().startswith("python"):
        return None
    return [str(exe), "-m", "backend.turn_worker"]


class TurnWorker:
    """One running turn, in its own process, killable at any moment."""

    def __init__(self, sid: str):
        self.sid = sid
        self.proc: subprocess.Popen | None = None
        self._cancel_sent = False
        self._killed = False
        self._lock = threading.Lock()

    # ── spawn ─────────────────────────────────────────────────────────────
    def start(self, *, project: str | None, ref: str | None, chat: bool,
              session: str, has_instructions: bool, reasoning: str | None,
              max_steps: int | None, messages: list[dict], trace: list) -> bool:
        """Spawn the worker process. False when the mode is not process."""
        if worker_mode() != "process":
            return False
        cmd = _worker_command()
        if not cmd:
            return False
        argv = cmd + [
            "--project", project or "", "--ref", ref or "",
            "--chat", "1" if chat else "0",
            "--session", session or "",
            "--instructions", "1" if has_instructions else "0",
            "--reasoning", reasoning or "",
            "--max-steps", str(max_steps or 0),
        ]
        try:
            # Spawned through proctools: the `tacit-turn-worker` argv marker
            # makes the process identifiable on a machine full of python.exe,
            # the job object takes it down if this parent dies hard, and the
            # registry row is what /api/processes shows and the startup sweep
            # sweeps. The tree-kill flags stay: the job contains orphans, the
            # group is what the cooperative kill reaches first.
            self.proc = proctools.spawn(
                proctools.ROLE_WORKER, argv, session=session,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                cwd=str(PROJECT_ROOT),
                env=os.environ.copy(),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                               if os.name == "nt" else 0),
                **({"start_new_session": True} if os.name != "nt" else {}))
        except Exception:
            return False
        self._pump_stderr()
        self._messages = messages
        self._trace = trace
        # The transcript handshake goes out HERE, not on the first events()
        # read. The worker's stdin watcher treats the first line as the
        # handshake and everything after it as control lines; a steer written
        # before the parent's worker thread first called events() would be
        # consumed AS the handshake and the turn would run without it —
        # measured in test_steer.py, which steers immediately after spawn.
        # Writing it at spawn closes that race: from this moment on, every
        # line on stdin is a control line.
        try:
            self.proc.stdin.write(json.dumps({"transcript": messages}) + "\n")
            self.proc.stdin.flush()
        except Exception:
            return False
        return True

    def _pump_stderr(self):
        """The worker's stderr to the server's, so a crash is visible somewhere."""
        proc = self.proc

        def run():
            try:
                for line in (proc.stderr or []):
                    sys.stderr.write("[turn-worker] " + str(line))
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    # ── events ────────────────────────────────────────────────────────────
    def events(self):
        """Yield the worker's events.

        The transcript handshake is written by start() at spawn time, not
        here: the worker's stdin watcher consumes the first line as the
        handshake, so a steer written before the first events() read would
        otherwise be eaten as the handshake and the turn would run without
        it. From start() on, every line on stdin is a control line.
        """
        proc = self.proc
        if proc is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "trace":
                    # Kill-safe: the worker re-sends its cumulative trace after
                    # every step, so the LAST one seen is the most complete. A
                    # hard-killed worker's finally never runs; this per-step copy
                    # is what the parent keeps.
                    self._trace[:] = list(ev.get("steps") or [])
                    continue
                yield ev
            # If we reach here, stdout closed (worker exited).
            # Check if worker crashed vs clean exit.
            if proc.poll() is not None:
                exit_code = proc.returncode
                if exit_code != 0:
                    yield {"type": "error", "message": f"turn worker exited with code {exit_code}"}
                else:
                    yield {"type": "error", "message": "turn worker exited unexpectedly (clean exit but no done event)"}
        finally:
            # The registry row dies with the process, however consumption ends.
            # The reap used to live after the read loop, so a parent that
            # abandoned the generator mid-turn — an exception in the pump, a
            # browser gone mid-turn — left the row behind forever: the listing
            # then showed workers that had exited, which is exactly the lie
            # that sent an operator hunting for zombies that were not there.
            # The row is dropped before the error events above are consumed,
            # which is fine: the row describes the process, not the events.
            # `self.proc` stays: a consumed worker is an exited worker, and
            # the caller still owns it — `wait()`, `cancel()`'s honest
            # already-exited answer and `alive()`'s poll() all read it. Nulling
            # it here turned every clean exhaustion into a lost handle (the
            # Popen fell to the GC with its pipes unclosed) and broke every
            # lifecycle test that waits on the process after the stream ends.
            proctools.reap(proc)

    # ── steering ──────────────────────────────────────────────────────────
    def steer(self, text: str) -> bool:
        """Carry a mid-turn steer to the worker over its stdin.

        The thread path hands `run_turn` the socket's steer list directly;
        across a process boundary the list cannot be shared, so the same
        line protocol that carries the cancel carries the steer. False when
        there is no live worker — the caller then leaves the steer in the
        socket's list, which the thread path still reads.
        """
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.stdin.write(json.dumps({"steer": str(text or "")}) + "\n")
            proc.stdin.flush()
            return True
        except Exception:  # noqa: BLE001
            return False

    # ── stop ──────────────────────────────────────────────────────────────
    def cancel(self, grace: float = CANCEL_GRACE_S) -> dict:
        """Two-stage stop: cooperative cancel, then the tree kill.

        Returns what actually happened, because the interface says which one
        ran — the same honesty the sandbox layer applies to isolation.
        """
        proc = self.proc
        if proc is None or proc.poll() is not None:
            if proc is not None:
                proctools.reap(proc)
            return {"ok": True, "how": "already-exited"}
        with self._lock:
            if not self._cancel_sent:
                self._cancel_sent = True
                try:
                    proc.stdin.write(json.dumps({"cancel": True}) + "\n")
                    proc.stdin.flush()
                except Exception:
                    pass
        try:
            proc.wait(timeout=grace)
            proctools.reap(proc)
            return {"ok": True, "how": "cancelled"}
        except subprocess.TimeoutExpired:
            pass
        self.kill()
        proctools.reap(proc)
        return {"ok": True, "how": "killed"}

    def kill(self) -> None:
        """The hard stop: the worker's whole process tree, right now."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        self._killed = True
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True, timeout=15)
            else:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def spawn(sid: str) -> TurnWorker:
    return TurnWorker(sid)


# ── the worker process itself ─────────────────────────────────────────────
def _run_worker(argv: list[str]) -> int:
    """The subprocess entry: run one turn, stream events as JSON lines."""
    # Windows pipes default to cp1252, not UTF-8. The worker writes model
    # output — reasoning full of em dashes and curly quotes — straight onto
    # this pipe, and the parent reads it as UTF-8, so every non-ASCII
    # character came back as a decode error that killed the turn mid-thought
    # on ANY model, reading exactly like an abort. Reconfigure both ends of
    # this process to UTF-8 before a single event is written.
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8",
                                 errors="replace", line_buffering=True)

    from . import agent, audit, config
    from .ai import prompts

    def arg(name: str, default: str = "") -> str:
        return argv[argv.index(name) + 1] if name in argv else default

    project = arg("--project") or None
    ref = arg("--ref") or None
    chat = arg("--chat") == "1"
    session = arg("--session")
    has_instructions = arg("--instructions") == "1"
    reasoning = arg("--reasoning") or None
    max_steps = int(arg("--max-steps", "0") or 0) or None

    config.ensure_home()
    config.load_env()

    # The transcript arrives on stdin as one JSON line.
    try:
        raw = sys.stdin.readline()
        handshake = json.loads(raw or "{}")
    except Exception:
        handshake = {}
    messages = handshake.get("transcript") or []

    stop = threading.Event()
    cancelled = {"flag": False}
    steer: list[str] = []

    def watch_stdin():
        """Lines on stdin, checked between events: cancel and steer.

        The thread path shares the socket's steer list with `run_turn`
        directly; across the process boundary the same line protocol that
        carries the cancel carries the steer, appended to the list handed
        to `run_turn` below.
        """
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("cancel"):
                    cancelled["flag"] = True
                    stop.set()
                    break
                if "steer" in msg:
                    text = str(msg.get("steer") or "")
                    if text:
                        steer.append(text)
        except Exception:
            pass
    threading.Thread(target=watch_stdin, daemon=True).start()

    out = sys.stdout
    started = time.time()
    crashed = {"flag": False}
    trace: list[dict] = []
    try:
        for ev in agent.run_turn(messages, project=project, ref=ref, chat=chat,
                                 session=session,
                                 has_instructions=has_instructions,
                                 stop=stop, steer=steer,
                                 reasoning=config.reasoning_for(reasoning) if reasoning else None,
                                 max_steps=max_steps, trace=trace):
            # No accounting here. The parent owns the usage meter, the tool
            # counter and the metrics for both paths — doing it in the worker
            # too would bill every turn twice. The worker's only job is to
            # run the turn and stream what happened.
            kind = ev.get("type")
            out.write(json.dumps(ev, ensure_ascii=False) + "\n")
            out.flush()
            # Kill-safe trace: after every step, the cumulative trace rides
            # along as its own event. A hard kill cannot reach the finally
            # below, so this per-step copy is what lets a killed turn keep
            # the steps it completed — the parent takes the last one it saw.
            if kind in ("tool_end", "done", "error"):
                out.write(json.dumps({"type": "trace", "steps": trace}) + "\n")
                out.flush()
            if cancelled["flag"]:
                break
    except BaseException as exc:  # noqa: BLE001
        # Catch BaseException to handle KeyboardInterrupt, SystemExit, etc.
        out.write(json.dumps({"type": "error", "message": f"worker crashed: {exc}"[:400]}) + "\n")
        out.flush()
        crashed["flag"] = True
    finally:
        # The trace goes back even on a clean cancel. A hard kill cannot
        # reach the finally — which is why the parent also accepts the
        # cumulative trace after every step (see the per-step event below),
        # so a killed turn keeps what streamed to the interface.
        out.write(json.dumps({"type": "trace", "steps": trace}) + "\n")
        out.flush()
        audit.record("turn_worker_exit", session=session, backend="turn_worker",
                     status="error" if crashed["flag"] else "ok",
                     wall_s=round(time.time() - started, 1))
    return 0


def main() -> int:
    return _run_worker(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
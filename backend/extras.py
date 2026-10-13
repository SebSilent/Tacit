import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

from . import config, launcher, proctools

BG: dict[str, dict] = {}
TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
BLOCK_RE = re.compile(r"</(p|div|li|h[1-6]|tr|section|article)>", re.I)
ANY_TAG = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t]+")


def _new_id() -> str:
    return uuid.uuid4().hex[:6]


def _drain(proc, buf: list, cap: int):
    def run(stream):
        try:
            for line in iter(stream.readline, ""):
                buf.append(line)
                if len(buf) > 4000:
                    del buf[:1000]
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            threading.Thread(target=run, args=(stream,), daemon=True).start()


def bg_start(command: str, project: str | None = None, cwd: str | None = None,
             session: str = "") -> str:
    cmd = str(command or "").strip()
    if not cmd:
        return "ERROR: empty command"
    work = cwd or project or str(config.USER_HOME)
    try:
        # Registry-only identity: the user's command must not be corrupted by
        # an argv marker, so the child is identified by TACIT_ROLE /
        # TACIT_SESSION_ID in its environment and by its registry row — not by
        # its command line. The job object still contains it: a crashed parent
        # takes its bg children down too.
        proc = proctools.spawn(
            "tacit-bg", cmd, session=str(session or ""), cwd=work,
            contain=True, mark=False, shell=True, text=True,
            # DEVNULL, not inherited: a background job must never sit on the
            # worker's control pipe, nor hold a handle on it after the turn ends.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
    except Exception as e:
        return f"ERROR: {e}"
    bid = _new_id()
    out: list[str] = []
    # The session tag is what makes a turn's background jobs stoppable with
    # the turn (stage 3): call_tool injects it, and stop_session uses it.
    BG[bid] = {"proc": proc, "cmd": cmd, "cwd": work, "out": out,
               "started": time.time(), "session": str(session or "")}
    _drain(proc, out, 4000)
    return f"started [{bid}] in {work}: {cmd}\nread it with bg_output(id='{bid}')"


def bg_output(bid: str, tail: int = 4000) -> str:
    rec = BG.get(str(bid or "").strip())
    if not rec:
        names = ", ".join(BG) or "none"
        return f"ERROR: no background job '{bid}'. Running: {names}"
    body = "".join(rec["out"])
    if len(body) > tail:
        body = "...(truncated)\n" + body[-tail:]
    code = rec["proc"].poll()
    state = "running" if code is None else f"exited {code}"
    return f"[{bid}] {state} | {round(time.time() - rec['started'], 1)}s\n{body.strip() or '(no output yet)'}"


def bg_stop(bid: str) -> str:
    key = str(bid or "").strip()
    rec = BG.get(key)
    if not rec:
        return f"ERROR: no background job '{key}'"
    if rec["proc"].poll() is None:
        try:
            rec["proc"].terminate()
        except Exception:
            pass
    BG.pop(key, None)
    return f"stopped [{key}]"


def stop_session_jobs(session: str) -> int:
    """Stop every background job a session started. Returns how many.

    Part of the kill contract (stage 3): a turn's work does not outlive the
    turn. Jobs started outside any session are nobody's to stop here.
    """
    key = str(session or "")
    if not key:
        return 0
    n = 0
    for bid, rec in list(BG.items()):
        if rec.get("session") != key:
            continue
        if rec["proc"].poll() is None:
            try:
                rec["proc"].terminate()
            except Exception:  # noqa: BLE001
                pass
        BG.pop(bid, None)
        n += 1
    return n


def fetch(url: str, max_chars: int | None = None) -> str:
    target = str(url or "").strip()
    if not re.match(r"^https?://", target, re.I):
        return "ERROR: url must start with http:// or https://"
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, read=40.0), follow_redirects=True) as client:
            r = client.get(target, headers={"User-Agent": "Tacit"})
    except httpx.HTTPError as e:
        return f"ERROR: {e}"
    if r.status_code >= 400:
        return f"ERROR: HTTP {r.status_code}"
    ctype = r.headers.get("content-type", "")
    body = r.text
    if "html" in ctype.lower():
        body = TAG_RE.sub(" ", body)
        body = BLOCK_RE.sub("\n", body)
        body = ANY_TAG.sub(" ", body)
        body = html.unescape(body)
        body = "\n".join(WS_RE.sub(" ", ln).strip() for ln in body.splitlines())
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
    limit = max_chars or config.FETCH_LIMIT
    head = f"{target} -> {r.status_code}, {len(body)} chars\n"
    return head + (body[:limit] + f"\n...(truncated, {len(body)} chars total)" if len(body) > limit else body)


def snapshot(root: str, label: str = "", session: str = "") -> str:
    src = Path(str(root or "")).expanduser()
    if not src.is_dir():
        return f"ERROR: {root} is not a directory"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = config.CHECKPOINT_DIR / f"{stamp}-{_new_id()}"
    count = 0
    try:
        for path in src.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel_parts = path.relative_to(src).parts
            except ValueError:
                continue
            # Only the path *inside* the project counts. Testing absolute parts
            # would skip everything whenever the project itself sat in a folder
            # named build/ dist/ checkpoints/ …
            if any(part in config.SNAPSHOT_SKIP for part in rel_parts):
                continue
            if path.stat().st_size > config.SNAPSHOT_MAX_FILE:
                continue
            rel = path.relative_to(src)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(path.read_bytes())
            count += 1
            if count >= config.SNAPSHOT_MAX_FILES:
                break
    except Exception as e:
        return f"ERROR: {e}"
    if label:
        try:
            (dest / ".label").write_text(label, encoding="utf-8")
        except Exception:
            pass
    if session:
        try:
            (dest / ".session").write_text(str(session), encoding="utf-8")
        except Exception:
            pass
    return f"snapshot {dest.name}: {count} file(s)" + (f" - {label}" if label else "")


def list_snapshots() -> str:
    root = config.CHECKPOINT_DIR
    if not root.is_dir():
        return "no snapshots yet"
    rows = sorted([p for p in root.iterdir() if p.is_dir()], reverse=True)[:20]
    if not rows:
        return "no snapshots yet"
    out = []
    for p in rows:
        label = ""
        try:
            label = (p / ".label").read_text(encoding="utf-8").strip()
        except Exception:
            pass
        n = sum(1 for f in p.rglob("*") if f.is_file())
        out.append(f"{p.name}  {n} file(s)" + (f"  - {label}" if label else ""))
    return "\n".join(out)


def snapshot_index(limit: int = 200, session: str = "") -> list[dict]:
    """Structured snapshots for the UI timeline (the string helpers below stay
    for the agent tools).

    Each row carries what the timeline needs without re-stat'ing the tree: the
    id, when it was taken, its label, and the files it holds. A session filter
    keeps only the snapshots that session took; snapshots with no session
    marker (the agent's own tool, or anything from before this change) show
    under the empty filter only, so nothing old disappears from the global
    view.
    """
    root = config.CHECKPOINT_DIR
    if not root.is_dir():
        return []
    dirs = sorted([p for p in root.iterdir() if p.is_dir()], reverse=True)
    rows = []
    for path in dirs[:max(1, min(int(limit or 200), 1000))]:
        label = ""
        try:
            label = (path / ".label").read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            label = ""
        marker = ""
        try:
            marker = (path / ".session").read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            marker = ""
        if session and marker != session:
            continue
        files, total = [], 0
        for f in path.rglob("*"):
            if f.is_file() and f.name not in (".label", ".session"):
                files.append(str(f.relative_to(path)))
                try:
                    total += f.stat().st_size
                except Exception:  # noqa: BLE001
                    pass
        created = ""
        m = re.match(r"^(\d{8})-(\d{6})-", path.name)
        if m:
            d, t = m.group(1), m.group(2)
            created = (f"{d[0:4]}-{d[4:6]}-{d[6:8]} "
                       f"{t[0:2]}:{t[2:4]}:{t[4:6]}")
        rows.append({
            "name": path.name,
            "label": label,
            "session": marker,
            "created": created,
            "file_count": len(files),
            "files": sorted(files)[:200],
            "truncated": len(files) > 200,
            "bytes": total,
        })
    return rows


def snapshot_detail(name: str) -> dict | None:
    return next((s for s in snapshot_index(1000) if s["name"] == str(name or "").strip()), None)


def snapshot_compare(name: str, root: str, limit: int = 500) -> dict:
    """Compare a snapshot with the project as it stands now.

    Uses size-then-content so a cheap comparison answers most of the time and a
    full read only happens on a size match. Capped so a huge tree cannot stall
    the UI.
    """
    src = config.CHECKPOINT_DIR / str(name or "").strip()
    if not src.is_dir():
        return {"ok": False, "error": f"no snapshot '{name}'"}
    dest = Path(str(root or "")).expanduser()
    if not dest.is_dir():
        return {"ok": False, "error": f"{root} is not a directory"}

    def collect(base: Path) -> dict:
        out = {}
        for path in base.rglob("*"):
            if not path.is_file() or path.name == ".label":
                continue
            try:
                rel = path.relative_to(base)
            except ValueError:
                continue
            # relative parts only — see the note in snapshot()
            if any(part in config.SNAPSHOT_SKIP for part in rel.parts):
                continue
            rel_str = str(rel)
            try:
                out[rel_str] = path.stat().st_size
            except OSError:
                continue
            if len(out) >= limit:
                break
        return out

    old, new = collect(src), collect(dest)
    changed, added, removed = [], [], []
    for rel, size in old.items():
        if rel not in new:
            removed.append(rel)
        elif new[rel] != size:
            changed.append(rel)
        else:
            try:
                if (src / rel).read_bytes() != (dest / rel).read_bytes():
                    changed.append(rel)
            except OSError:
                changed.append(rel)
    for rel in new:
        if rel not in old:
            added.append(rel)
    return {
        "ok": True,
        "name": src.name,
        "project": str(dest),
        "changed": sorted(changed),
        "added": sorted(added),
        "removed": sorted(removed),
        "count": len(changed) + len(added) + len(removed),
        "truncated": len(old) >= limit or len(new) >= limit,
    }


def restore(name: str, root: str) -> str:
    src = config.CHECKPOINT_DIR / str(name or "").strip()
    if not src.is_dir():
        return f"ERROR: no snapshot '{name}'"
    dest = Path(str(root or "")).expanduser()
    if not dest.is_dir():
        return f"ERROR: {root} is not a directory"
    restored = 0
    for path in src.rglob("*"):
        if not path.is_file() or path.name == ".label":
            continue
        rel = path.relative_to(src)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(path.read_bytes())
        restored += 1
    return f"restored {restored} file(s) from {src.name} into {dest}"


# ── self-restart ─────────────────────────────────────────────────────────
# Settings > Tools > Restart server. The dying process cannot start its own
# replacement — a child started before exit dies with the parent on every
# platform this runs on — so the work is split: this process asks uvicorn to
# exit through its own shutdown (lifespan hooks included), and a small
# detached watcher waits for the port to free, then starts the server once
# and blocks on it, which is what keeps the new process alive.

HANDSHAKE_FILE = "restart.json"
RESTART_LOG = "restart.log"
RESTART_STATE_FILE = "restart_state.json"
PORT_FREE_TIMEOUT = 30.0     # seconds to wait for the old listener to drop
COMEBACK_TIMEOUT = 20.0      # seconds to wait for the new listener to appear
# How long a finished restart's outcome stays worth reporting. A failure is news
# for the next few minutes, not for ever: once the server is back and has been
# serving for a while, `last_restart` is history, and history belongs in
# audit.jsonl rather than in the Settings panel.
RESTART_STATE_TTL = 900.0


def _server_command() -> list[str] | None:
    """The FALLBACK start command: this interpreter, as a module.

    Only for a server with no launcher script to hand the job to — an embedded
    run, a test client, a checkout that never ran install. Reusing this
    interpreter is right there (it is where the packages are) and the environment
    carries over.

    It is the fallback and not the rule. Starting the server this way is what
    made the Restart button a second launch path nobody could read: no console,
    the environment of a process already on its way out, and none of what
    run.bat sets. See `_start_command`.

    The marker token rides as the script's first argument, exactly where
    `proctools._marked_argv` puts it, so a server started by the fallback and a
    server started by the launcher are proved by the same command-line check.
    """
    exe = Path(sys.executable)
    if not exe.name.lower().startswith("python"):
        return None
    return [str(exe), "-m", "backend.main", launcher.SERVER_MARKER]


def _start_command() -> tuple[list[str] | None, str]:
    """(command, which path) — the launcher script if there is one, else this interpreter.

    Decided when the button is pressed and written into the handshake, so the
    watcher runs the path the response reported instead of re-picking one after
    the fact. Re-resolving it in the watcher is how plan section 0.4 got two
    interpreters in one chain: `python` was looked up off PATH by a process whose
    parent had been started from a `.venv`.
    """
    script = launcher.launcher_path()
    if script is not None:
        # `/bin/sh script` rather than the script alone: the +x bit is not
        # guaranteed on a checkout, and sh does not need it.
        if config.OS == "Windows":
            return ["cmd.exe", "/c", str(script)], "launcher"
        return ["/bin/sh", str(script)], "launcher"
    cmd = _server_command()
    return (cmd, "interpreter") if cmd else (None, "none")


def restart_server() -> dict:
    """Ask this server to exit and leave a watcher behind to start the next one."""
    from . import audit

    server = getattr(config, "SERVER", None)
    if server is None:
        # No handle on the uvicorn.Server means Tacit was started some other way
        # (an embedded server, a test client). Killing the process from here
        # would be a guess about who owns it, so the honest answer is no.
        return {"ok": False,
                "error": "restart is only available when Tacit runs as its own server "
                         "(start it with run.bat / run.sh)"}
    if getattr(server, "should_exit", False):
        # A second press while the first exit is still in flight would spawn a
        # second watcher, and two starters on one port is a bind race.
        return {"ok": False, "error": "a restart is already in progress"}

    # Which path will start the next server, decided NOW and carried in the
    # handshake: the watcher must run the one this response reports, not
    # re-resolve a `python` off PATH after the parent is gone.
    start_cmd, via = _start_command()
    if not start_cmd:
        return {"ok": False, "error": "could not identify the interpreter to restart with, "
                                      "and no launcher script was found"}
    handshake = {"host": config.HOST, "port": config.PORT,
                 "pid": os.getpid(), "ts": time.time(),
                 "cmd": start_cmd, "via": via}
    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        (config.HOME / HANDSHAKE_FILE).write_text(json.dumps(handshake), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not write the restart handshake: {e}"}
    # `starting` before anything else happens, so a reader during the gap sees an
    # attempt in flight rather than the outcome of some earlier restart.
    record_restart_state("starting", via=via, cmd=start_cmd, port=config.PORT)

    # Detached: the watcher must outlive this process, and on Windows that
    # takes DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP; the POSIX side gets
    # start_new_session. Output goes to a log in ~/.tacit, not to a console
    # this process is about to lose.
    flags = 0
    kwargs: dict = {}
    if config.OS == "Windows":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    watcher_cmd = [str(sys.executable), "-c",
                   f"import sys; sys.path.insert(0, r'{config.ROOT}'); "
                   "from backend import extras; extras.restart_watch()"]
    watcher = None
    try:
        with open(config.HOME / RESTART_LOG, "ab") as log:
            # Spawned through proctools: the watcher is the one Tacit child
            # that MUST outlive its parent by design, so it gets no job object
            # (containment would kill it at restart) but keeps the argv marker
            # and the registry row — it is identifiable, and the next boot's
            # sweep reaps it if the restart never came back.
            watcher = proctools.spawn(
                proctools.ROLE_WATCHER, watcher_cmd, cwd=str(config.ROOT),
                contain=False,
                stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
                **kwargs)
    except Exception as e:  # noqa: BLE001
        (config.HOME / HANDSHAKE_FILE).unlink(missing_ok=True)
        # This server stays up — the exit is only requested after the watcher is
        # proved running — but the attempt and its reason are still recorded.
        record_restart_state("failed", stage="watcher", detail=str(e),
                             via=via, cmd=start_cmd, port=config.PORT)
        return {"ok": False, "error": f"could not start the restart watcher: {e}"}
    finally:
        # Recorded even when the spawn failed: the attempt is the fact.
        audit.record("server_restart", backend="server", mode="restart",
                     pid=os.getpid(), port=config.PORT, host=config.HOST,
                     watcher=watcher.pid if watcher else 0, via=via)

    # Not os._exit: the point is that uvicorn's own shutdown runs, which stops
    # the analyzer, stops the MCP servers and removes the pid file.
    server.should_exit = True
    # `via` is the point of item 4: the response says which path starts the next
    # server — the launcher script a person runs, or this interpreter when there
    # is no script to run — instead of the caller having to wonder.
    return {"ok": True, "pid": os.getpid(), "watcher": watcher.pid,
            "port": config.PORT, "log": str(config.HOME / RESTART_LOG),
            "via": via, "cmd": start_cmd}


def _can_bind(host: str, port: int) -> bool:
    """True if a fresh listener could take host:port right now.

    Probed by binding, not connecting. A connect against a live listener fills
    its backlog — nothing accepts it — and the next connect is refused, which
    reads as a free port that is not free; measured here against a real
    listener. Binding is the exact test the new server will face. On POSIX the
    probe carries SO_REUSEADDR, which is what asyncio's create_server sets by
    default, so a port in TIME_WAIT from the old server's connections does not
    read as held; on Windows SO_REUSEADDR means something else entirely (it
    would let the probe share a live listener's port) and is left off, where a
    closed listener rebinds cleanly anyway.
    """
    import socket
    probe = socket.socket()
    try:
        if config.OS != "Windows":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host if host != "0.0.0.0" else "127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except OSError:
            pass


def _port_busy(host: str, port: int, timeout: float) -> bool:
    """True if something accepts on host:port within the timeout."""
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host or "127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _restart_state_file() -> Path:
    return config.HOME / RESTART_STATE_FILE


def record_restart_state(state: str, *, stage: str = "", detail: str = "",
                         via: str = "", cmd: list[str] | None = None,
                         port: int = 0) -> None:
    """Leave the restart's outcome on disk for the next process to read.

    The awkward part about a failed restart is that nobody is left holding the
    fact: the watcher that knows it failed has no server to report through, and
    the browser has nothing to ask. `restart.log` records the running commentary,
    but a log file nobody opens is not making failure visible.

    So the outcome is written where whichever server comes back can read it —
    including one a person started by hand after the failure — and Settings >
    Server reports it. Written atomically because a reader must never see half a
    restart; a failed write is not worth failing the restart over.
    """
    payload = {"state": state, "stage": stage, "detail": detail, "via": via,
               "cmd": [str(p) for p in (cmd or [])], "port": port,
               "watcher": os.getpid(), "attempted_at": time.time()}
    path = _restart_state_file()
    try:
        tmp = Path(str(path) + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def restart_state(max_age: float = RESTART_STATE_TTL) -> dict:
    """The last restart's outcome, or {} when there is nothing recent to report.

    Age-gated on purpose: a failure is news for the next few minutes. After that
    it belongs in the audit log, not in the panel a person opens to check a port.
    """
    try:
        raw = json.loads(_restart_state_file().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    try:
        age = time.time() - float(raw.get("attempted_at") or 0)
    except (TypeError, ValueError):
        return {}
    if not 0 <= age <= max_age:
        return {}
    if raw.get("state") == "starting":
        # A watcher that died mid-flight leaves `starting` behind and nothing
        # corrects it. Past the point where every branch of restart_watch would
        # have reported, silence IS the failure — so say that instead of letting
        # the panel show a restart in flight forever.
        stall = PORT_FREE_TIMEOUT + COMEBACK_TIMEOUT + 60.0
        if age > stall:
            return dict(raw, state="failed", stage="stalled",
                        detail="the restart watcher never reported an outcome")
    return raw


def restart_watch() -> None:
    """The detached watcher: wait for the port to free, start the server, block.

    Blocking on the child is what keeps the replacement alive after the old
    process is gone; exiting instead would orphan nothing only because there
    would be nothing left running.
    """
    import socket

    def log_line(text: str) -> None:
        try:
            with open(config.HOME / RESTART_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"[restart] {text}\n")
        except Exception:
            pass

    try:
        handshake = json.loads((config.HOME / HANDSHAKE_FILE).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log_line(f"no handshake ({e}); nothing to do")
        record_restart_state("failed", stage="handshake", detail=str(e))
        return
    (config.HOME / HANDSHAKE_FILE).unlink(missing_ok=True)

    host = str(handshake.get("host") or "127.0.0.1")
    port = int(handshake.get("port") or 0)
    if not port:
        log_line("handshake named no port; nothing to do")
        record_restart_state("failed", stage="handshake",
                             detail="the handshake named no port")
        return

    # The old process was asked to exit, not killed, so the listener drops on
    # its own. If it is still there after the timeout, starting a replacement
    # would only produce a second process that loses the bind race and exits —
    # say so in the log and stop.
    end = time.time() + PORT_FREE_TIMEOUT
    while not _can_bind(host, port):
        if time.time() >= end:
            log_line(f"port {port} still held after {PORT_FREE_TIMEOUT:.0f}s; not starting "
                     "a replacement that would lose the bind race")
            record_restart_state("failed", stage="port", port=port,
                                 detail=f"port {port} was still held {PORT_FREE_TIMEOUT:.0f}s "
                                        "after the old server was asked to exit, so nothing "
                                        "was started")
            return
        time.sleep(0.25)

    # The path the button chose, out of the handshake — so the launcher script
    # that answered the request is the launcher script that runs. A handshake
    # written before this existed has no command in it, and only then does the
    # watcher decide for itself.
    cmd = handshake.get("cmd") or None
    via = str(handshake.get("via") or "")
    if not cmd:
        cmd, via = _start_command()
    if not cmd:
        log_line("could not identify the interpreter to restart with")
        record_restart_state("failed", stage="start", port=port,
                             detail="no launcher script was found and this interpreter could "
                                    "not be reused — start Tacit with run.bat / run.sh")
        return
    log_line(f"port {port} is free; starting via {via}: {' '.join(cmd)}")
    spawn_kwargs: dict = {}
    if via == "launcher":
        # Two things the launcher path needs that the interpreter path does not.
        # A marker token would be injected into `cmd.exe /c run.bat` and break
        # it, so the argv goes through untouched — the server the script starts
        # registers itself, marker and all. And on Windows the console is the
        # feature: a new window, which run.bat's `pause` keeps open if the
        # server dies. That is the difference between a failed restart a person
        # can read and one that only exists in restart.log.
        spawn_kwargs["mark"] = False
        if config.OS == "Windows":
            spawn_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    elif any(m in cmd for m in proctools.MARKERS):
        # The fallback command already carries its marker: `_server_command` puts
        # it in argv so a server started by a script and one started here are
        # proved by the same check. Spawning it with the default `mark=True`
        # injected a second token and left the child reading
        # `backend.main tacit-server tacit-server`. Anything that arrives without
        # a marker — an older handshake, or one written by a caller that forgot —
        # still gets it added, because an unmarked server is one the next stop
        # step cannot see.
        spawn_kwargs["mark"] = False
    try:
        # The replacement server is marked (identifiable) but deliberately NOT
        # job-contained: it must outlive the watcher that started it, and a
        # kill-on-close job would take it down when the watcher exits. The
        # registry row is what the next boot's sweep uses to tell a live
        # server from an orphan.
        proc = proctools.spawn(proctools.ROLE_SERVER, cmd, cwd=str(config.ROOT),
                               contain=False, **spawn_kwargs)
    except Exception as e:  # noqa: BLE001
        log_line(f"failed to start the server: {e}")
        record_restart_state("failed", stage="spawn", detail=str(e),
                             via=via, cmd=cmd, port=port)
        return
    # The watcher's registry row comes off IMMEDIATELY, not once the server is
    # confirmed back: the replacement's own startup sweep runs before its port
    # is bound, so a watcher still registered at that moment looks exactly like
    # an orphan from a previous crash — and tree-killing it would kill the
    # server it just started. The log and the argv marker keep it identifiable
    # regardless; it exits on its own when the server it babysits exits.
    proctools.unregister(os.getpid())
    if _port_busy("127.0.0.1", port, COMEBACK_TIMEOUT):
        log_line(f"server is back on port {port} (pid {proc.pid})")
        record_restart_state("ok", stage="bind", via=via, cmd=cmd, port=port,
                             detail=f"pid {proc.pid} is listening on port {port}")
    else:
        # The one failure the plan asked to make visible: started, never bound.
        # The console the launcher opened is what a person reads; this is what
        # Settings says once any server is answering again.
        detail = (f"the server started by {via or 'the watcher'} (pid {proc.pid}) never "
                  f"accepted on port {port} within {COMEBACK_TIMEOUT:.0f}s")
        log_line(f"server (pid {proc.pid}) did not accept on port {port} within "
                 f"{COMEBACK_TIMEOUT:.0f}s; it may have failed to start — see the log above")
        record_restart_state("failed", stage="bind", detail=detail,
                             via=via, cmd=cmd, port=port)
    code = proc.wait()
    log_line(f"server (pid {proc.pid}) exited with {code}")
    if code not in (0, None):
        # It bound, then died. Reporting `ok` after that would be a lie by the
        # time anyone reads it, and a crash-on-boot is the failure worth naming.
        record_restart_state("failed", stage="exit", via=via, cmd=cmd, port=port,
                             detail=f"the replacement server (pid {proc.pid}) exited with {code}")

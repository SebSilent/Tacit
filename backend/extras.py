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

from . import config

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


def bg_start(command: str, project: str | None = None, cwd: str | None = None) -> str:
    cmd = str(command or "").strip()
    if not cmd:
        return "ERROR: empty command"
    work = cwd or project or str(config.USER_HOME)
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=work, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                bufsize=1)
    except Exception as e:
        return f"ERROR: {e}"
    bid = _new_id()
    out: list[str] = []
    BG[bid] = {"proc": proc, "cmd": cmd, "cwd": work, "out": out, "started": time.time()}
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
PORT_FREE_TIMEOUT = 30.0     # seconds to wait for the old listener to drop
COMEBACK_TIMEOUT = 20.0      # seconds to wait for the new listener to appear


def _server_command() -> list[str] | None:
    """The command that starts the server: this interpreter, as a module.

    The interpreter running Tacit is the right one to run it again — it is
    where the packages are, and run.bat would work but ends in a `pause`.
    The environment is inherited, so TACIT_PORT, TACIT_HOST and every other
    setting the running server had, the new one has too.
    """
    exe = Path(sys.executable)
    if not exe.name.lower().startswith("python"):
        return None
    return [str(exe), "-m", "backend.main"]


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

    handshake = {"host": config.HOST, "port": config.PORT,
                 "pid": os.getpid(), "ts": time.time()}
    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        (config.HOME / HANDSHAKE_FILE).write_text(json.dumps(handshake), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not write the restart handshake: {e}"}

    cmd = _server_command()
    if not cmd:
        (config.HOME / HANDSHAKE_FILE).unlink(missing_ok=True)
        return {"ok": False, "error": "could not identify the interpreter to restart with"}

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
            watcher = subprocess.Popen(watcher_cmd, cwd=str(config.ROOT), stdout=log,
                                       stderr=subprocess.STDOUT, creationflags=flags,
                                       **kwargs)
    except Exception as e:  # noqa: BLE001
        (config.HOME / HANDSHAKE_FILE).unlink(missing_ok=True)
        return {"ok": False, "error": f"could not start the restart watcher: {e}"}
    finally:
        # Recorded even when the spawn failed: the attempt is the fact.
        audit.record("server_restart", backend="server", mode="restart",
                     pid=os.getpid(), port=config.PORT, host=config.HOST,
                     watcher=watcher.pid if watcher else 0)

    # Not os._exit: the point is that uvicorn's own shutdown runs, which stops
    # the analyzer, stops the MCP servers and removes the pid file.
    server.should_exit = True
    return {"ok": True, "pid": os.getpid(), "watcher": watcher.pid,
            "port": config.PORT, "log": str(config.HOME / RESTART_LOG)}


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
        return
    (config.HOME / HANDSHAKE_FILE).unlink(missing_ok=True)

    host = str(handshake.get("host") or "127.0.0.1")
    port = int(handshake.get("port") or 0)
    if not port:
        log_line("handshake named no port; nothing to do")
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
            return
        time.sleep(0.25)

    cmd = _server_command()
    if not cmd:
        log_line("could not identify the interpreter to restart with")
        return
    log_line(f"port {port} is free; starting {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(cmd, cwd=str(config.ROOT))
    except Exception as e:  # noqa: BLE001
        log_line(f"failed to start the server: {e}")
        return
    if _port_busy("127.0.0.1", port, COMEBACK_TIMEOUT):
        log_line(f"server is back on port {port} (pid {proc.pid})")
    else:
        log_line(f"server (pid {proc.pid}) did not accept on port {port} within "
                 f"{COMEBACK_TIMEOUT:.0f}s; it may have failed to start — see the log above")
    proc.wait()
    log_line(f"server (pid {proc.pid}) exited with {proc.returncode}")

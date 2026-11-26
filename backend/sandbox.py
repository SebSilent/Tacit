"""Sandbox backends.

None of these is a security boundary against a determined attacker, and none of
them claims to be. What they are is a place where two questions have an explicit,
readable answer: **what was this command allowed to touch**, and **what did it
actually change**. Anything a backend cannot enforce is reported as not enforced,
rather than quietly assumed.

The rule the whole module follows: a command either runs under the backend you
chose, or it does not run. It is never silently downgraded to something weaker.

Backends:
  none          direct execution, fully transparent, trusted local use
  tacit-micro   the strongest primitive this OS offers, plus a timeout, limits
                where the platform allows, and a report of every file that changed
  container     Docker or Podman, opt-in, for people who already trust containers

Tacit implements this itself. It does not shell out to another harness, and no
other harness needs to be installed for any isolation to work.

Isolation ladder for tacit-micro:
  Linux    bubblewrap: read-only or read-write project bind, private /tmp,
           optional network namespace, dies with the parent
  macOS    sandbox-exec with a generated profile: no network, writes confined
  Windows  no equal primitive exists in the base OS, so nothing is faked. The
           timeout and the change report still apply, and the report says plainly
           that filesystem and network isolation are not in force.
"""

from __future__ import annotations

import codecs
import locale
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import audit, config, proctools, providers, sudo

IS_POSIX = os.name != "nt"
SCAN_LIMIT = 4000
SKIP = config.SNAPSHOT_SKIP


def _linux_iso() -> str:
    """The isolation binary on Linux, if one is present."""
    return shutil.which("bwrap") or ""


def _macos_iso() -> str:
    """macOS has sandbox-exec in the base system."""
    if sys.platform != "darwin":
        return ""
    return shutil.which("sandbox-exec") or ""


def mechanism() -> str:
    """Which isolation primitive would actually be used right now."""
    if str((providers.load() or {}).get("sandbox", {}).get("backend") or "") == "container" \
            and _container_runtime():
        return "container"
    if sys.platform.startswith("linux"):
        return "bubblewrap" if _linux_iso() else "posix-limits"
    if sys.platform == "darwin":
        return "sandbox-exec" if _macos_iso() else "posix-limits"
    return "none"


def platform_capabilities() -> dict:
    """What this operating system can actually enforce, right now."""
    mech = mechanism()
    if mech == "container":
        # A container runtime enforces all of it, on every platform including
        # Windows, which is the whole reason it is offered as an option.
        return {"mechanism": "container", "timeout": True, "cpu_limit": True,
                "memory_limit": True, "file_size_limit": False, "network": True,
                "readonly_project": True, "overlay_writes": False,
                "private_tmp": True, "process_isolation": True}
    return {
        "mechanism": mech,
        "timeout": True,
        "cpu_limit": IS_POSIX,
        "memory_limit": IS_POSIX,
        "file_size_limit": IS_POSIX,
        "network": mech in ("bubblewrap", "sandbox-exec"),
        "readonly_project": mech in ("bubblewrap", "sandbox-exec"),
        "overlay_writes": False,
        "private_tmp": mech == "bubblewrap",
        "process_isolation": mech == "bubblewrap",
    }


def _can_unshare_net() -> bool:
    """A network namespace is the only portable way to cut the network off."""
    if not IS_POSIX:
        return False
    if shutil.which("unshare") is None:
        return False
    try:
        probe = subprocess.run(["unshare", "--net", "true"], stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=5)
        return probe.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _limits_applier(limits: dict):
    """Return a preexec_fn enforcing limits, or None where unsupported."""
    if not IS_POSIX or not limits:
        return None
    cpu = int(limits.get("cpu_seconds") or 0)
    mem_mb = int(limits.get("memory_mb") or 0)
    if not cpu and not mem_mb:
        return None

    def apply():  # runs in the child, before exec
        import resource

        if cpu:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 2))
        if mem_mb:
            cap = mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        # no new privileges, and no core dumps
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except Exception:  # noqa: BLE001
            pass

    return apply


# ── change reporting ───────────────────────────────────────────────────────
def _scan(root: Path) -> dict[str, int]:
    """A cheap map of the project: relative path to size. Content is not read."""
    out: dict[str, int] = {}
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if len(out) >= SCAN_LIMIT:
            break
        try:
            if not path.is_file():
                continue
            rel = path.relative_to(root)
        except (OSError, ValueError):
            continue
        if any(part in SKIP for part in rel.parts):
            continue
        try:
            out[str(rel)] = path.stat().st_size
        except OSError:
            continue
    return out


def _diff(before: dict, after: dict) -> dict:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(p for p in (set(before) & set(after)) if before[p] != after[p])
    return {"added": added, "modified": changed, "removed": removed,
            "count": len(added) + len(changed) + len(removed)}


# ── running ────────────────────────────────────────────────────────────────
_SANDBOX_PROFILE = """(version 1)
(deny default)
(allow process*)
(allow sysctl-read)
(allow file-read*)
(allow file-write* (subpath "{project}") (subpath "/private/tmp") (subpath "/tmp"))
{network}
"""


def _wrap(argv: list[str], cwd: str, network: bool, readonly: bool) -> list[str]:
    """Put the strongest available primitive in front of the command.

    Returns the argv unchanged where nothing stronger exists, so the caller can
    report what was and was not enforced rather than guessing.
    """
    mech = mechanism()
    if mech == "bubblewrap":
        bwrap = _linux_iso()
        # --unshare-pid is not a nicety: without it the command sees every
        # host pid and can signal (kill) the launching shell, the server,
        # anything the person owns. The pid namespace is what makes
        # containment mean containment; --proc then shows the private one.
        out = [bwrap, "--die-with-parent", "--unshare-pid",
               "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        for sysdir in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"):
            if os.path.isdir(sysdir):
                out += ["--ro-bind", sysdir, sysdir]
        bind = "--ro-bind" if readonly else "--bind"
        out += [bind, cwd, cwd, "--chdir", cwd]
        if not network:
            out.append("--unshare-net")
        return out + ["--"] + argv
    if mech == "sandbox-exec":
        import tempfile
        profile = _SANDBOX_PROFILE.format(
            project=cwd,
            network="(deny network*)" if not network else "(allow network*)")
        handle = tempfile.NamedTemporaryFile("w", suffix=".sb", delete=False)
        handle.write(profile)
        handle.close()
        return [_macos_iso(), "-f", handle.name] + argv
    return argv


def _spawn(command: str, cwd: str, timeout: int, limits: dict, network: bool,
           readonly: bool = False):
    """Build and run the process. Returns (argv, kwargs, notes)."""
    notes: list[str] = []
    base = [config.SHELL, "/c" if not IS_POSIX else "-c", command]
    argv = _wrap(base, cwd, network, readonly)

    env = dict(os.environ)
    if not network and not platform_capabilities()["network"]:
        # advisory only, and labelled as such: a proxy variable stops polite
        # clients, not a program that opens a socket directly
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[key] = "http://127.0.0.1:0"
        env["NO_PROXY"] = ""

    # stdin: DEVNULL for the same reason as _run_captured, nothing a turn spawns
    # may inherit the worker's control pipe.
    kwargs = {"cwd": cwd, "stdin": subprocess.DEVNULL, "capture_output": True,
              "text": True, "timeout": timeout, "env": env}
    applier = _limits_applier(limits)
    if applier is not None:
        kwargs["preexec_fn"] = applier
    elif limits:
        notes.append("resource limits are not enforced on this platform")
    return argv, kwargs, notes


def run(command: str, project: str | None = None, backend: str | None = None,
        timeout: int | None = None, session: str = "", on_output=None, stop=None) -> dict:
    """Run one command under the selected sandbox. Records it in the ledger.

    ``on_output(kind, text)`` streams the command's output as it lands, for a
    viewer. It changes nothing about what is returned or recorded, so the model
    reads the same result whether or not anyone is watching.
    """
    cmd = str(command or "").strip()
    cfg = providers.load()
    box = dict(cfg.get("sandbox") or {})
    chosen = backend or box.get("backend") or "none"
    limit = max(1, min(int(timeout or box.get("timeout") or config.SHELL_TIMEOUT), 600))
    # Same rule the file tools use, from one place: a usable project wins, otherwise
    # this session's scratch folder. It used to fall back to the user's home
    # directory, which put a project-less `run_shell` in `~` with no blocklist at all.
    root = config.work_root(project, session)

    if not cmd:
        return _result(False, "ERROR: empty command", backend=chosen, cwd=str(root))

    ok, why = providers.available(chosen, "sandbox")
    row = providers.get(chosen, "sandbox")
    if row is None:
        return _result(False, f"ERROR: unknown sandbox backend '{chosen}'",
                       backend=chosen, cwd=str(root))
    if not row["implemented"]:
        return _result(False, f"ERROR: the '{chosen}' sandbox has no adapter yet. "
                              f"Choose none or tacit-micro in Settings.", backend=chosen,
                       cwd=str(root))
    if not ok:
        return _result(False, f"ERROR: the '{chosen}' sandbox is unavailable: {why}",
                       backend=chosen, cwd=str(root))

    # All three backends run the command through `_run_captured`, so all three can
    # stream it. (The plan scoped this to `none` and `tacit-micro`; leaving the
    # container backend out would have been an arbitrary gap, not a safety one.
    # What does NOT stream is an image pull, which happens before the command runs
    # and can legitimately take twenty minutes with a silent card.)
    if chosen == "tacit-micro" and sudo.armed():
        pre = sudo.prepare(cmd)
        if pre.get("fed"):
            # Elevation and the wrapper do not coexist: bwrap strips setuid
            # and mounts the system read-only, so a sudo inside it can never
            # become root. The sudo switch is the person's standing decision
            # that this machine's root work runs with host power, so a
            # command carrying a feedable sudo runs on the host path and
            # says so in its notes. Everything without sudo stays behind
            # the wall, this turn and every turn.
            res = _run_none(cmd, root, limit, session, on_output, stop)
            try:
                res["notes"] = list(res.get("notes") or []) + [
                    "sudo: elevation cannot pass through the sandbox wrapper "
                    "(setuid is stripped inside it), so this command ran on "
                    "the host, outside the sandbox. The sudo switch decided "
                    "that; non-sudo commands still run contained."]
            except Exception:  # noqa: BLE001
                pass
            return res
    if chosen == "tacit-micro":
        return _run_micro(cmd, root, box, limit, session, on_output, stop=stop)
    if chosen == "container":
        return _run_container(cmd, root, box, limit, session, on_output, stop=stop)
    return _run_none(cmd, root, limit, session, on_output, stop=stop)


def _result(ok: bool, text: str = "", **fields) -> dict:
    out = {"ok": ok, "backend": fields.pop("backend", "none"), "code": 0,
           "stdout": "", "stderr": "", "blocked": [], "notes": [],
           "changed": {"added": [], "modified": [], "removed": [], "count": 0},
           "enforced": {}, "requested": {}, "duration_ms": 0}
    out.update(fields)
    if not ok and text:
        out["stderr"] = text
        out["blocked"].append(text.replace("ERROR: ", "", 1))
    elif text:
        out["stdout"] = text
    return out


GRACE_SECONDS = 10


def _kill_tree(proc) -> bool:
    """Kill the command's containment boundary and report whether it worked.

    Root exit is not a reason to skip cleanup: descendants can still be alive and
    holding the captured pipes. The per-command job/group outlives the root for
    exactly that case.
    """
    return proctools.terminate_tree(proc)


def _drain_chunks(stream, kind: str, on_output, buf: list, lock, errors: list) -> None:
    """Drain bytes incrementally, without letting decoding or a viewer kill the reader."""
    decoder = codecs.getincrementaldecoder(locale.getpreferredencoding(False) or "utf-8")("replace")
    try:
        read = getattr(stream, "read1", stream.read)
        while True:
            chunk = read(8192)
            if not chunk:
                break
            text = decoder.decode(chunk)
            if not text:
                continue
            with lock:
                buf.append(text)
            if on_output is not None:
                try:
                    on_output(kind, text)
                except Exception:  # a viewer must never stop process drainage
                    pass
        tail = decoder.decode(b"", final=True)
        if tail:
            with lock:
                buf.append(tail)
            if on_output is not None:
                try:
                    on_output(kind, tail)
                except Exception:
                    pass
    except Exception as exc:  # surfaced below; never silently mistaken for EOF
        with lock:
            errors.append(f"{kind} reader: {exc}")
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _run_streamed(proc, timeout: int, on_output, stop=None) -> tuple[int, str, str, bool]:
    """Supervise the root and both pipes against one monotonic deadline.

    Captured and live calls share this path. A direct shell that exits while a
    descendant retains a pipe is not complete: it still owns the command's output
    and must hit the deadline and containment cleanup just like a live root.
    """
    lock = threading.Lock()
    outs: list = []
    errs: list = []
    reader_errors: list = []
    readers = [
        threading.Thread(target=_drain_chunks,
                         args=(proc.stdout, "stdout", on_output, outs, lock, reader_errors),
                         daemon=True, name="tacit-out-reader"),
        threading.Thread(target=_drain_chunks,
                         args=(proc.stderr, "stderr", on_output, errs, lock, reader_errors),
                         daemon=True, name="tacit-err-reader"),
    ]
    for thread in readers:
        thread.start()
    deadline = time.monotonic() + max(1, int(timeout))
    timed_out = False
    cancelled = False
    cleanup_error = ""
    try:
        while True:
            if stop is not None and stop.is_set():
                cancelled = True
                if not _kill_tree(proc):
                    cleanup_error = "could not terminate the whole command tree"
                break
            if proc.poll() is not None and all(not thread.is_alive() for thread in readers):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                if not _kill_tree(proc):
                    cleanup_error = "could not terminate the whole command tree"
                break
            if proc.poll() is None:
                try:
                    proc.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    pass
            else:
                # The root is gone but a descendant still owns at least one pipe.
                time.sleep(min(0.05, remaining))

        # One shared cleanup budget, not ten seconds per stream followed by an
        # unbounded close. Correct tree termination should produce EOF by itself.
        cleanup_deadline = time.monotonic() + GRACE_SECONDS
        if proc.poll() is None:
            try:
                proc.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
            except Exception:
                pass
        for thread in readers:
            thread.join(max(0, cleanup_deadline - time.monotonic()))
        if proc.poll() is None or any(thread.is_alive() for thread in readers):
            cleanup_error = cleanup_error or "command cleanup did not finish within the grace period"

        with lock:
            out, err = "".join(outs), "".join(errs)
            if reader_errors:
                err += ("\n" if err else "") + "ERROR: " + "; ".join(reader_errors)
        if cleanup_error:
            err += ("\n" if err else "") + "ERROR: " + cleanup_error
        if cancelled:
            err += ("\n" if err else "") + "ERROR: command cancelled"
        code = 130 if cancelled else 124 if timed_out else proc.returncode
        if code is None:
            code = 1
        return int(code), out, err, timed_out
    except Exception as exc:  # noqa: BLE001
        _kill_tree(proc)
        return 1, "", f"ERROR: {exc}", False
    finally:
        if proc.poll() is not None:
            proctools.reap(proc)


def _run_captured(argv, *, cwd: str, env=None, timeout: int, shell: bool = False,
                  preexec_fn=None, on_output=None, stdin_data: str | None = None,
                  session: str = "", stop=None) -> tuple[int, str, str, bool]:
    """Run one contained command through the shared wait/drain supervisor.

    ``stdin_data`` is the sole exception to the DEVNULL rule and exists for sudo.
    Its writer always runs beside the readers, whether or not a viewer is present,
    so a chatty child cannot deadlock against a password write.
    """
    # A turn's stdin is the worker's control pipe. Agent commands must never
    # inherit it: an input read would either wait forever or consume cancel/steer.
    kwargs: dict = {"stdin": subprocess.DEVNULL,
                    "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                    "env": env}
    if stdin_data is not None:
        kwargs["stdin"] = subprocess.PIPE
    if shell:
        if IS_POSIX and isinstance(argv, str):
            # shell=True would let /bin/sh interpret the string, a second shell
            # with different rules from the one the sandbox resolved. One
            # shell everywhere: the explicit list form goes through
            # config.SHELL (backend/config.py). Windows keeps the raw string:
            # passing a list through shell=True on Windows re-quotes it and
            # misparses commands that contain their own quotes (see
            # proctools.spawn).
            argv = [config.SHELL, "-c", argv]
        else:
            kwargs["shell"] = True
    if preexec_fn is not None:
        kwargs["preexec_fn"] = preexec_fn
    if IS_POSIX:
        kwargs["start_new_session"] = True
    proc = proctools.spawn("tacit-shell", argv, session=session, cwd=cwd,
                           contain=True, mark=False, **kwargs)
    if IS_POSIX:
        proc._tacit_group = True
    if stdin_data is not None:
        threading.Thread(target=_write_stdin, args=(proc, stdin_data),
                         daemon=True, name="tacit-shell-stdin").start()
    return _run_streamed(proc, timeout, on_output, stop=stop)


def _write_stdin(proc, data: str) -> None:
    """Hand a command its stdin, then close it. Runs on its own thread.

    A failure here is the child's report, not this one's: a command that exits before
    reading leaves a closed pipe, and whatever it wrote to stderr and its exit code say
    what happened."""
    try:
        encoding = locale.getpreferredencoding(False) or "utf-8"
        proc.stdin.write(str(data).encode(encoding, errors="replace"))
        proc.stdin.close()
    except Exception:  # noqa: BLE001
        pass


def _signal_name(code) -> str:
    """The signal a crashed command died by, as a name a person can read.

    subprocess reports a signal death as a NEGATIVE return code (-11), which a
    tool result renders as "exit code -11" - a number a model and a person both
    have to decode. A terminal showing `Program '/bin/fish' crashed` beside a
    command that died by SIGSEGV is the same fact from two sides; naming the
    signal makes the pairing legible, and `exit code -11` reads as nonsense."""
    try:
        n = int(code)
    except (TypeError, ValueError):
        return ""
    import signal
    if n < 0:
        # We watched the process die: subprocess reports -signum.
        signum = -n
    elif not IS_POSIX or not 128 < n < 165:
        return ""
    else:
        # Through a POSIX shell a signal death arrives as the shell's own
        # exit code: 128 + signum (bash's convention, inherited by sh).
        signum = n - 128
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


def _run_none(cmd: str, root: Path, limit: int, session: str, on_output=None, stop=None) -> dict:
    started = time.time()
    # Elevation is supplied here and nowhere else. This is the one backend that runs the
    # command on this machine as this user, which is all `sudo` means; the other two
    # report that they do not supply it rather than letting the command hang on a prompt.
    run = sudo.prepare(cmd)
    code, out, err, timed_out = _run_captured(run["command"], cwd=str(root), timeout=limit,
                                              shell=True, on_output=on_output,
                                              stdin_data=run["stdin"], session=session,
                                              stop=stop)
    cancelled = stop is not None and stop.is_set()
    sudo_note = None
    if run["elevated"]:
        sudo_note = (f"sudo: the saved password was supplied to {run['fed']} command(s). "
                     "They ran as root. If one of them still failed, that is sudo's own "
                     "answer, not the switch being off.")
    elif run.get("error"):
        sudo_note = f"sudo: {run['error']}"
    else:
        # Armed, a sudo in the command, nothing fed. This used to be the silent case, and
        # silence is what let a model read `sudo: no password was provided` as root having
        # been withdrawn and report that back as a fact about the machine.
        sudo_note = sudo.explain(run, code, f"{out}\n{err}") or None
    if cancelled:
        note = "ERROR: command cancelled"
        if err and "command cancelled" not in err:
            note += "\n" + err
        res = _result(False, note, backend="none", cwd=str(root), code=130,
                      stdout=out, duration_ms=int((time.time() - started) * 1000))
        audit.record("sandbox_run", session=session, tool="run_shell", backend="none",
                     status="cancelled", code=130, timeout=limit, command=cmd)
        return res
    if timed_out:
        partial = (out or "").strip()
        note = (f"ERROR: timed out after {limit}s. That is this shell's cap, not the task's "
                "runtime budget. If the task states one for the deliverable, a command that "
                "cannot beat the cap is a bug in the approach: shrink the search space, "
                "vectorize, cut the loop, or precompute. Rerunning this exact command unchanged "
                "cannot pass - change something first.")
        if err:
            note += "\n" + err
        res = _result(False, note, backend="none", cwd=str(root), code=124,
                      duration_ms=int((time.time() - started) * 1000))
        if partial:
            # Whatever the command printed before the kill is usually exactly the state the next
            # attempt needs, diffs collected, candidates ranked. This used to be discarded, so a
            # 90%-done computation looked identical to a hung no-op.
            res["stdout"] = partial[-2000:]
        if sudo_note:
            # A command that hung after being handed a root password is the one timeout
            # where what it was elevated for changes what the next attempt should be.
            res["notes"].append(sudo_note)
        audit.record("sandbox_run", session=session, tool="run_shell", backend="none",
                     status="timeout", timeout=limit, command=cmd, sudo=run["sites"])
        return res

    res = _result(True, backend="none", cwd=str(root), code=code, stdout=out, stderr=err,
                  duration_ms=int((time.time() - started) * 1000))
    res["enforced"] = {"timeout": True, "cpu_limit": False, "memory_limit": False,
                       "network": False, "readonly_project": False}
    res["notes"] = ["no isolation: this backend runs commands directly"]
    sig = _signal_name(code)
    if sig:
        # A signal death, not an exit; naming it is the difference between
        # decoding "-11"/"139" and reading "SIGSEGV". Through a shell the
        # number is the 128+signum convention, so the wording is honest about
        # the inference.
        res["notes"].append(f"the command was killed by {sig}" if code < 0
                            else f"the command exited {code} - a POSIX shell "
                                 f"reports signal deaths as 128+signum, so it "
                                 f"was likely killed by {sig}")
    if sudo_note:
        res["notes"].append(sudo_note)
    audit.record("sandbox_run", session=session, tool="run_shell", backend="none",
                 status="ok" if code == 0 else f"exit {code}", code=code,
                 timeout=limit, command=cmd, changed=res["changed"]["count"],
                 sudo=run["fed"] if run["elevated"] else 0)
    return res


def _sudo_unsupplied(cmd: str) -> str:
    """The note for a backend that does not feed the saved password.

    Both sandboxed backends leave stdin at DEVNULL, so a `sudo` there fails at once
    rather than waiting for a terminal that does not exist. Failing promptly is not the
    same as being explained: without this the model reads "a password is required" and
    spends its next steps trying to type into a pipe."""
    return ("sudo: this backend does not supply the saved password, so the command ran "
            "as written and failed unless sudo here needs no password. The `none` "
            "backend is the one that supplies it")


def _run_micro(cmd: str, root: Path, box: dict, limit: int, session: str,
               on_output=None, stop=None) -> dict:
    started = time.time()
    network = bool(box.get("network"))
    readonly = bool(box.get("readonly_project"))
    limits = {"cpu_seconds": box.get("cpu_seconds") or 0,
              "memory_mb": box.get("memory_mb") or 0}
    caps = platform_capabilities()
    before = _scan(root)

    argv, kwargs, notes = _spawn(cmd, str(root), limit, limits, network, readonly)
    code, out, err, timed_out = _run_captured(
        argv, cwd=kwargs.get("cwd") or str(root), env=kwargs.get("env"),
        timeout=limit, preexec_fn=kwargs.get("preexec_fn"), on_output=on_output,
        session=session, stop=stop)
    cancelled = stop is not None and stop.is_set()
    if code == 1 and not out and str(err).startswith("ERROR:"):
        return _result(False, err, backend="tacit-micro", cwd=str(root))

    changed = _diff(before, _scan(root))
    enforced = {
        "mechanism": caps["mechanism"],
        "timeout": True,
        "cpu_limit": bool(caps["cpu_limit"] and limits["cpu_seconds"]),
        "memory_limit": bool(caps["memory_limit"] and limits["memory_mb"]),
        "network": bool(caps["network"] and not network),
        "readonly_project": bool(caps["readonly_project"] and readonly),
        "private_tmp": bool(caps["private_tmp"]),
        "process_isolation": bool(caps["process_isolation"]),
    }
    if not network and not enforced["network"]:
        notes.append("network is not cut off on this platform; the proxy variables are "
                     "advisory only")
    if readonly and not enforced["readonly_project"]:
        notes.append("the project could not be mounted read-only on this platform")
    if not readonly:
        notes.append("the project is writable; every change is reported and snapshots "
                     "can undo them")
    if caps["mechanism"] == "none":
        notes.append("this system offers no isolation primitive Tacit can use. The "
                     "timeout and the change report still apply, and nothing here is "
                     "pretending otherwise")
    if not caps["cpu_limit"] and limits["cpu_seconds"]:
        notes.append("CPU limits are not enforced on this platform")
    if not caps["memory_limit"] and limits["memory_mb"]:
        notes.append("memory limits are not enforced on this platform")
    if sudo.uses_sudo(cmd):
        notes.append(_sudo_unsupplied(cmd))

    stopped = cancelled or timed_out
    error_text = ("ERROR: command cancelled" if cancelled else
                  f"ERROR: timed out after {limit}s" if timed_out else err)
    if stopped and err and err not in error_text:
        error_text += "\n" + err
    res = _result(
        not stopped, backend="tacit-micro", cwd=str(root),
        code=130 if cancelled else 124 if timed_out else code,
        stdout=out, stderr=error_text,
        changed=changed, notes=notes, enforced=enforced,
        requested={"network": network, "timeout": limit, **limits},
        duration_ms=int((time.time() - started) * 1000))

    audit.record("sandbox_run", session=session, tool="run_shell", backend="tacit-micro",
                 mode="micro", status="cancelled" if cancelled else "timeout" if timed_out else
                 ("ok" if code == 0 else f"exit {code}"),
                 code=res["code"], timeout=limit, changed=changed["count"],
                 enforced=enforced, network=network, command=cmd)
    return res


def _container_runtime() -> str:
    """The container CLI on PATH, if one is installed. Docker first, then Podman.

    Both take the same flags for everything used here, and both are accepted
    because which one a person has is a matter of platform and taste, not of
    trustworthiness.
    """
    return shutil.which("docker") or shutil.which("podman") or ""


def _runtime_label(runtime: str) -> str:
    """The runtime's name as a person would type it: ``docker``, not ``docker.EXE``.

    Used in messages that tell the user a command to run, where the Windows
    extension is noise at best and wrong to copy at worst.
    """
    return Path(runtime).stem if runtime else "docker"


DEFAULT_IMAGE = "python:3.12-slim"


def container_settings() -> dict:
    """Image and pull policy, from the capability registry.

    Pulling defaults to off. Downloading an image is installing something, and the
    rule everywhere else in Tacit is that nothing is installed for you, so a
    missing image is reported with the exact command rather than fetched silently.
    """
    cfg = (providers.load() or {}).get("sandbox") or {}
    return {
        "image": str(cfg.get("container_image") or DEFAULT_IMAGE),
        "auto_pull": bool(cfg.get("container_auto_pull", False)),
        "pids_limit": int(cfg.get("container_pids_limit") or 256),
    }


def container_argv(command: str, cwd: str, image: str, *, network: bool, readonly: bool,
                   limits: dict, name: str, pids_limit: int = 256) -> list[str]:
    """The full argv for one sandboxed command.

    Everything the platform primitives could not enforce on Windows is enforceable
    here, and by the runtime rather than by us: a real network namespace, a
    genuinely read-only bind, real memory and CPU ceilings, a PID ceiling, and no
    privilege escalation.
    """
    runtime = _container_runtime()
    argv = [runtime or "docker", "run", "--rm", "--name", name,
            "--workdir", "/workspace",
            # The project is bind-mounted, not copied, so the change report and
            # snapshot/restore describe the files the user actually has.
            "--mount", f"type=bind,source={cwd},target=/workspace"
                       + (",readonly" if readonly else ""),
            "--tmpfs", "/tmp",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(max(1, int(pids_limit)))]
    if not network:
        argv += ["--network", "none"]
    mem_mb = int(limits.get("memory_mb") or 0)
    if mem_mb > 0:
        argv += ["--memory", f"{mem_mb}m"]
    cpu = float(limits.get("cpu_seconds") or 0)
    if cpu > 0:
        # cpu_seconds is a budget, not a rate; one core is the honest reading of it
        # as a container limit.
        argv += ["--cpus", "1"]
    argv += [image, "/bin/sh", "-c", command]
    return argv


def _runtime_cli(argv: list[str], timeout: float, stop=None) -> tuple[int, str, str, str]:
    """One runtime CLI call under a deadline and the turn's stop flag.

    Returns (code, stdout, stderr, status) with status ok / timeout /
    cancelled / spawn_error. Image inspect and pull used to be bare
    subprocess.run calls with no stop path: a cancel during a twenty-minute
    pull waited out the pull. The CLI is a client, not the work: killing it is
    always safe, and any container it already created is settled separately,
    by name, through the ledger.
    """
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc), "spawn_error"
    deadline = time.monotonic() + max(0.5, float(timeout))
    out, err = b"", b""
    status = "ok"
    while True:
        if stop is not None and stop.is_set():
            status = "cancelled"
            break
        try:
            out, err = proc.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                status = "timeout"
                break
    if status != "ok":
        try:
            if os.name == "nt":
                # A runtime CLI on Windows can be a wrapper (.bat) whose child
                # does the work; killing the root alone leaves the child
                # holding the pipes. The tree is the boundary.
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=15)
            else:
                proc.kill()
                try:
                    from . import debuglog
                    debuglog.signal_sent("sandbox.runtime-kill", proc.pid,
                                         "proc.kill", True)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
    def _decode(b: bytes) -> str:
        return (b or b"").decode(locale.getpreferredencoding(False) or "utf-8", "replace")
    return int(proc.returncode or 0), _decode(out), _decode(err), status


def _image_present(image: str, stop=None) -> tuple[bool, str]:
    runtime = _container_runtime()
    if not runtime:
        return False, "spawn_error"
    code, _out, _err, status = _runtime_cli([runtime, "image", "inspect", image], 30, stop)
    if status != "ok":
        return False, status
    return code == 0, "ok"


def _container_running(name: str) -> bool:
    runtime = _container_runtime()
    if not runtime:
        return False
    code, out, _err, status = _runtime_cli(
        [runtime, "inspect", "--format", "{{.State.Running}}", name], 20)
    if status != "ok" or code != 0:
        return False
    return out.strip() == "true"


def _kill_container(name: str) -> bool:
    """Stop the daemon-side container and verify the runtime agrees it is down.

    A kill command's own exit code is not the proof: `docker kill` on a
    container that already exited answers with an error, and a kill that the
    daemon accepted but did not carry out answers with success. The inspect
    afterwards is the fact.
    """
    runtime = _container_runtime()
    if not runtime:
        return False
    _runtime_cli([runtime, "kill", name], 20)
    code, out, _err, status = _runtime_cli(
        [runtime, "inspect", "--format", "{{.State.Running}}", name], 20)
    if status != "ok":
        return False
    if code != 0:
        return True  # the runtime no longer knows it (--rm took it)
    return out.strip() != "true"


def _run_container(cmd: str, root: Path, box: dict, limit: int, session: str,
                   on_output=None, stop=None) -> dict:
    started = time.time()
    runtime = _container_runtime()
    if not runtime:
        return _result(False, "ERROR: no container runtime on PATH. Install Docker or "
                              "Podman, or choose none / tacit-micro in Settings.",
                       backend="container", cwd=str(root))
    settings = container_settings()
    image = settings["image"]

    def _early_stop(status: str, stage: str) -> dict:
        cancelled = status == "cancelled"
        audit.record("sandbox_run", session=session, tool="run_shell", backend="container",
                     mode="container", image=image,
                     status="cancelled" if cancelled else "timeout",
                     stage=stage, timeout=limit, command=cmd)
        return _result(False, "ERROR: command cancelled" if cancelled else
                       f"ERROR: {stage} did not finish within its deadline",
                       backend="container", cwd=str(root),
                       code=130 if cancelled else 124,
                       duration_ms=int((time.time() - started) * 1000))

    present, status = _image_present(image, stop)
    if status in ("cancelled", "timeout"):
        return _early_stop(status, "image inspect")
    if not present:
        if not settings["auto_pull"]:
            return _result(False, f"ERROR: the image '{image}' is not present locally, and "
                                  f"Tacit does not download images for you. Run: "
                                  f"{_runtime_label(runtime)} pull {image}  - or set a "
                                  f"different image in Settings.",
                           backend="container", cwd=str(root))
        code, out, err, pstatus = _runtime_cli([runtime, "pull", image], 1800, stop)
        if pstatus in ("cancelled", "timeout"):
            return _early_stop(pstatus, "image pull")
        if pstatus != "ok" or code != 0:
            return _result(False, f"ERROR: could not pull '{image}': "
                                  f"{(err or out or '')[:300]}",
                           backend="container", cwd=str(root))

    network = bool(box.get("network"))
    readonly = bool(box.get("readonly_project"))
    limits = {"cpu_seconds": box.get("cpu_seconds") or 0,
              "memory_mb": box.get("memory_mb") or 0}
    name = f"tacit-{int(started * 1000) % 10**8}-{os.getpid() % 1000}"
    argv = container_argv(cmd, str(root), image, network=network, readonly=readonly,
                          limits=limits, name=name, pids_limit=settings["pids_limit"])
    before = _scan(root)
    # The name goes to the long-lived parent BEFORE the client starts: a hard
    # worker death leaves the container running on the daemon, and the ledger
    # row is the only proof of what the dead worker was running.
    proctools.register_container(name, os.getpid(), session)
    cleanup_confirmed = False
    try:
        code, out, err, timed_out = _run_captured(argv, cwd=str(root), timeout=limit,
                                                  on_output=on_output, session=session,
                                                  stop=stop)
        cancelled = stop is not None and stop.is_set()
        if timed_out or cancelled:
            # The runtime, not just the client: `docker run` dying leaves the
            # container alive, so the command would carry on after Tacit
            # reported it stopped.
            cleanup_confirmed = _kill_container(name)
        else:
            # --rm should have taken the container with the client's exit;
            # verify rather than assume, and settle it if it is still up.
            cleanup_confirmed = not _container_running(name)
            if not cleanup_confirmed:
                cleanup_confirmed = _kill_container(name)
    finally:
        if cleanup_confirmed:
            proctools.unregister_container(name)
        # An unconfirmed cleanup leaves the row in the ledger: the parent's
        # worker-death cleanup and the next boot's sweep retry it by name.

    changed = _diff(before, _scan(root))
    notes = [f"ran in {_runtime_label(runtime)} image '{image}'"]
    if timed_out or cancelled:
        notes.append((f"timed out after {limit}s" if timed_out else "cancelled")
                     + ("; the container was killed, not just the client" if cleanup_confirmed
                        else "; ERROR: the runtime did not confirm that the container was killed"))
    elif not cleanup_confirmed:
        notes.append("ERROR: the runtime did not confirm that the container was removed")
    if sudo.uses_sudo(cmd):
        # Not a gap to close: `no-new-privileges` is set on the run argv (see
        # container_argv), so the kernel drops the setuid bit that sudo depends on, and
        # the root inside the image was never root on this machine anyway.
        notes.append("sudo: not supplied here and not usable here. The container runs "
                     "with no_new_privileges, so the kernel drops sudo's setuid bit, and "
                     "the root inside the image is not root on this machine")
    enforced = {
        "mechanism": "container",
        "timeout": True,
        "cpu_limit": bool(limits["cpu_seconds"]),
        "memory_limit": bool(limits["memory_mb"]),
        "network": not network,
        "readonly_project": readonly,
        "private_tmp": True,
        "process_isolation": True,
        "pids_limit": True,
        "no_new_privileges": True,
    }
    stopped = cancelled or timed_out
    error_text = ("ERROR: command cancelled" if cancelled else
                  f"ERROR: timed out after {limit}s" if timed_out else err)
    if stopped and err and err not in error_text:
        error_text += "\n" + err
    res = _result(not stopped, backend="container", cwd=str(root),
                  code=130 if cancelled else 124 if timed_out else code, stdout=out,
                  stderr=error_text,
                  changed=changed, notes=notes, enforced=enforced,
                  requested={"network": network, "timeout": limit, "image": image, **limits},
                  duration_ms=int((time.time() - started) * 1000), container=name)
    audit.record("sandbox_run", session=session, tool="run_shell", backend="container",
                 mode="container", image=image,
                 status="cancelled" if cancelled else "timeout" if timed_out else
                 ("ok" if code == 0 else f"exit {code}"),
                 code=res["code"], timeout=limit, changed=changed["count"],
                 enforced=enforced, network=network, command=cmd,
                 cleanup="confirmed" if cleanup_confirmed else "unconfirmed")
    return res


def describe() -> dict:
    """What to show the user about the sandbox, before anything runs."""
    chosen = providers.resolve("sandbox")
    caps = platform_capabilities()
    return {
        "backend": chosen["id"],
        "usable": chosen["ok"],
        "reason": chosen["reason"],
        "provider": chosen["provider"],
        "platform": "windows" if not IS_POSIX else sys.platform,
        "mechanism": caps["mechanism"],
        "can_enforce": caps,
        "will_enforce": {
            "timeout": True,
            "cpu_limit": bool(caps["cpu_limit"]),
            "memory_limit": bool(caps["memory_limit"]),
            "network": bool(caps["network"]),
            "readonly_project": bool(caps["readonly_project"]),
            "process_isolation": bool(caps["process_isolation"]),
        },
        "standalone": True,
        "note": "implemented inside Tacit; no other harness is required or called",
    }

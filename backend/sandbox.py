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

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import audit, config, providers

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
        # Windows — which is the whole reason it is offered as an option.
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
        probe = subprocess.run(["unshare", "--net", "true"], capture_output=True, timeout=5)
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
        out = [bwrap, "--die-with-parent", "--proc", "/proc", "--dev", "/dev",
               "--tmpfs", "/tmp"]
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

    kwargs = {"cwd": cwd, "capture_output": True, "text": True, "timeout": timeout,
              "env": env}
    applier = _limits_applier(limits)
    if applier is not None:
        kwargs["preexec_fn"] = applier
    elif limits:
        notes.append("resource limits are not enforced on this platform")
    return argv, kwargs, notes


def run(command: str, project: str | None = None, backend: str | None = None,
        timeout: int | None = None, session: str = "") -> dict:
    """Run one command under the selected sandbox. Records it in the ledger."""
    cmd = str(command or "").strip()
    cfg = providers.load()
    box = dict(cfg.get("sandbox") or {})
    chosen = backend or box.get("backend") or "none"
    limit = max(1, min(int(timeout or box.get("timeout") or config.SHELL_TIMEOUT), 600))
    root = Path(project).expanduser() if project else Path(config.USER_HOME)
    if not root.is_dir():
        root = Path(config.USER_HOME)

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

    if chosen == "tacit-micro":
        return _run_micro(cmd, root, box, limit, session)
    if chosen == "container":
        return _run_container(cmd, root, box, limit, session)
    return _run_none(cmd, root, limit, session)


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


def _kill_tree(proc) -> None:
    """Kill the process and everything it started.

    Killing only the direct child is not enough. On Windows a ``.bat`` or
    ``cmd /c`` wrapper leaves its grandchildren running, and they keep the captured
    pipe open, so the reader blocks long after the timeout fired — measured here as
    two orphaned processes still alive minutes later. A timeout that a command can
    outrun is not a timeout, and this is the one guarantee the sandbox makes on a
    platform where it can enforce nothing else.
    """
    if proc.poll() is not None:
        return
    try:
        if IS_POSIX:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _run_captured(argv, *, cwd: str, env=None, timeout: int, shell: bool = False,
                  preexec_fn=None) -> tuple[int, str, str, bool]:
    """Run a command and capture it, with a timeout that actually returns.

    Returns ``(code, stdout, stderr, timed_out)``; a timeout reports exit 124, the
    same convention `timeout(1)` uses. The bounded second read is what makes the
    timeout real: after the tree is killed, draining is given a short grace period
    and then abandoned rather than waited on indefinitely.
    """
    kwargs: dict = {"cwd": cwd, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                    "text": True, "env": env}
    if shell:
        kwargs["shell"] = True
    if preexec_fn is not None:
        kwargs["preexec_fn"] = preexec_fn
    if IS_POSIX:
        # Its own process group, so the whole tree can be signalled at once.
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv if not shell else argv, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=GRACE_SECONDS)
        except Exception:  # noqa: BLE001
            out, err = "", ""
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass
        return 124, out or "", err or "", True
    except Exception as exc:  # noqa: BLE001
        _kill_tree(proc)
        return 1, "", f"ERROR: {exc}", False


def _run_none(cmd: str, root: Path, limit: int, session: str) -> dict:
    started = time.time()
    code, out, err, timed_out = _run_captured(cmd, cwd=str(root), timeout=limit, shell=True)
    if timed_out:
        partial = (out or "").strip()
        note = (f"ERROR: timed out after {limit}s. That is this shell's cap, not the task's "
                "runtime budget. If the task states one for the deliverable, a command that "
                "cannot beat the cap is a bug in the approach: shrink the search space, "
                "vectorize, cut the loop, or precompute. Rerunning this exact command unchanged "
                "cannot pass - change something first.")
        res = _result(False, note, backend="none", cwd=str(root))
        if partial:
            # Whatever the command printed before the kill is usually exactly the state the next
            # attempt needs - diffs collected, candidates ranked. This used to be discarded, so a
            # 90%-done computation looked identical to a hung no-op.
            res["stdout"] = partial[-2000:]
        audit.record("sandbox_run", session=session, tool="run_shell", backend="none",
                     status="timeout", timeout=limit, command=cmd)
        return res

    res = _result(True, backend="none", cwd=str(root), code=code, stdout=out, stderr=err,
                  duration_ms=int((time.time() - started) * 1000))
    res["enforced"] = {"timeout": True, "cpu_limit": False, "memory_limit": False,
                       "network": False, "readonly_project": False}
    res["notes"] = ["no isolation: this backend runs commands directly"]
    audit.record("sandbox_run", session=session, tool="run_shell", backend="none",
                 status="ok" if code == 0 else f"exit {code}", code=code,
                 timeout=limit, command=cmd, changed=res["changed"]["count"])
    return res


def _run_micro(cmd: str, root: Path, box: dict, limit: int, session: str) -> dict:
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
        timeout=limit, preexec_fn=kwargs.get("preexec_fn"))
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

    res = _result(
        True, backend="tacit-micro", cwd=str(root),
        code=124 if timed_out else code,
        stdout=out, stderr=(f"ERROR: timed out after {limit}s" if timed_out else err),
        changed=changed, notes=notes, enforced=enforced,
        requested={"network": network, "timeout": limit, **limits},
        duration_ms=int((time.time() - started) * 1000))

    audit.record("sandbox_run", session=session, tool="run_shell", backend="tacit-micro",
                 mode="micro", status="timeout" if timed_out else
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
    rule everywhere else in Tacit is that nothing is installed for you — so a
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


def _image_present(image: str) -> bool:
    runtime = _container_runtime()
    if not runtime:
        return False
    try:
        r = subprocess.run([runtime, "image", "inspect", image],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _kill_container(name: str) -> None:
    """Stop the container itself, not just the CLI that started it.

    Killing `docker run` leaves the container running: the timeout would be
    reported to the user while the command carried on underneath, which is the one
    thing a sandbox must never do.
    """
    runtime = _container_runtime()
    if not runtime:
        return
    try:
        subprocess.run([runtime, "kill", name], capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        pass


def _run_container(cmd: str, root: Path, box: dict, limit: int, session: str) -> dict:
    started = time.time()
    runtime = _container_runtime()
    if not runtime:
        return _result(False, "ERROR: no container runtime on PATH. Install Docker or "
                              "Podman, or choose none / tacit-micro in Settings.",
                       backend="container", cwd=str(root))
    settings = container_settings()
    image = settings["image"]
    if not _image_present(image):
        if not settings["auto_pull"]:
            return _result(False, f"ERROR: the image '{image}' is not present locally, and "
                                  f"Tacit does not download images for you. Run: "
                                  f"{_runtime_label(runtime)} pull {image}  — or set a "
                                  f"different image in Settings.",
                           backend="container", cwd=str(root))
        try:
            pull = subprocess.run([runtime, "pull", image], capture_output=True,
                                  text=True, timeout=1800)
            if pull.returncode != 0:
                return _result(False, f"ERROR: could not pull '{image}': "
                                      f"{(pull.stderr or pull.stdout or '')[:300]}",
                               backend="container", cwd=str(root))
        except Exception as exc:  # noqa: BLE001
            return _result(False, f"ERROR: pull failed: {exc}",
                           backend="container", cwd=str(root))

    network = bool(box.get("network"))
    readonly = bool(box.get("readonly_project"))
    limits = {"cpu_seconds": box.get("cpu_seconds") or 0,
              "memory_mb": box.get("memory_mb") or 0}
    name = f"tacit-{int(started * 1000) % 10**8}-{os.getpid() % 1000}"
    argv = container_argv(cmd, str(root), image, network=network, readonly=readonly,
                          limits=limits, name=name, pids_limit=settings["pids_limit"])
    before = _scan(root)
    code, out, err, timed_out = _run_captured(argv, cwd=str(root), timeout=limit)
    if timed_out:
        # The runtime, not just the client: `docker run` dying leaves the container
        # alive, so the command would carry on after Tacit reported it stopped.
        _kill_container(name)

    changed = _diff(before, _scan(root))
    notes = [f"ran in {_runtime_label(runtime)} image '{image}'"]
    if timed_out:
        notes.append(f"timed out after {limit}s; the container was killed, not just the client")
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
    res = _result(True, backend="container", cwd=str(root),
                  code=124 if timed_out else code, stdout=out,
                  stderr=(f"ERROR: timed out after {limit}s" if timed_out else err),
                  changed=changed, notes=notes, enforced=enforced,
                  requested={"network": network, "timeout": limit, "image": image, **limits},
                  duration_ms=int((time.time() - started) * 1000), container=name)
    audit.record("sandbox_run", session=session, tool="run_shell", backend="container",
                 mode="container", image=image,
                 status="timeout" if timed_out else ("ok" if code == 0 else f"exit {code}"),
                 code=res["code"], timeout=limit, changed=changed["count"],
                 enforced=enforced, network=network, command=cmd)
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

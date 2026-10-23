"""Item 4: one launch path, and a stop step that can only touch what it can prove.

The bug these tests exist for was not that restarting failed; it was that the
Restart button and a person at a console used two different mechanisms, so the
path you could read was not the path that ran, and the stop step that came with
the readable path (`restart.ps1`) selected processes by executable name and then
narrowed that list with a substring test against the command line. Every Python
on the machine was a candidate.

The replacement rule: a process is touchable only when its *current* command line
carries one of Tacit's own argv markers (the same proof `proctools.sweep()`
already uses), and a launcher that cannot free the port says so with a nonzero
exit instead of starting a second server that loses the bind race and vanishes.

Two shapes of test here. Behavioural ones drive the real functions against a fake
process table and a temporary registry. Source ones read run.bat, run.sh,
restart.ps1, install.ps1 and install.sh, because "there is one launch mechanism"
is a property *of those files*: a fourth copy of the start command, added in a
script nobody imports, is exactly how the button and the console drifted apart.
"""

import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import config, extras, launcher, proctools  # noqa: E402
from tests.helpers import state_paths  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class Home(unittest.TestCase):
    """A temporary state folder, plus the two values the launcher decides from."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {key: getattr(config, key) for key in self._keys}
        self._orig_home, self._orig_os = config.HOME, config.OS
        self._orig_root = config.ROOT
        self.addCleanup(self._restore)
        config.HOME = home
        for key in self._keys:
            target = home / Path(self._orig[key]).name
            setattr(config, key, target)
            if key.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        self.home = home
        self.root = home / "repo"
        self.root.mkdir()
        config.ROOT = self.root

    def _restore(self):
        config.HOME, config.OS, config.ROOT = self._orig_home, self._orig_os, self._orig_root
        for key, value in self._orig.items():
            setattr(config, key, value)

    # ── fixtures ──────────────────────────────────────────────────────────

    def platform(self, os_name: str) -> None:
        config.OS = os_name

    def touch(self, name: str) -> Path:
        path = self.root / name
        path.write_text(f"echo {name}\n", encoding="utf-8")
        return path

    def process_table(self, alive=(), cmdlines=None) -> None:
        """The OS process list, as far as the launcher can know it.

        `_pid_alive` and `_pid_cmdline` are the module's only two senses, so
        replacing them replaces every fact it can act on; including the one that
        matters most: a registered pid that has been reused and now reports a
        command line belonging to somebody else's program.
        """
        alive_set = {int(p) for p in alive}
        table = {int(p): str(c) for p, c in (cmdlines or {}).items()}
        orig_alive, orig_cmd = proctools._pid_alive, proctools._pid_cmdline
        self.addCleanup(setattr, proctools, "_pid_alive", orig_alive)
        self.addCleanup(setattr, proctools, "_pid_cmdline", orig_cmd)
        proctools._pid_alive = lambda pid: pid in alive_set
        proctools._pid_cmdline = lambda pid: table.get(pid, "")

    def register(self, role: str, pid: int) -> None:
        # Through the real proctools.register, so the rows are the ones a running
        # server actually writes.
        proctools.register(role, pid, session="t")

    def kill_recorder(self):
        killed: list[int] = []
        orig = proctools.kill_tree
        self.addCleanup(setattr, proctools, "kill_tree", orig)
        proctools.kill_tree = lambda pid: (killed.append(pid), True)[1]
        return killed

    def free_port(self) -> int:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def fake_popen(self) -> list:
        """Catch every child spawn, including the ones made inside proctools.spawn."""
        spawned: list = []
        orig = extras.subprocess.Popen
        self.addCleanup(setattr, extras.subprocess, "Popen", orig)

        def fake(cmd, **kwargs):
            spawned.append({"cmd": list(cmd), "kwargs": kwargs})
            return types.SimpleNamespace(pid=9001, wait=lambda: None, returncode=0)
        extras.subprocess.Popen = fake
        return spawned


# ── who may be stopped ────────────────────────────────────────────────────

class TestFindRunning(Home):
    """Ownership is proved by the argv marker, never by the executable name."""

    def test_a_registered_marked_server_is_found(self):
        pid = 4001
        self.process_table(alive=[pid], cmdlines={
            pid: f"{sys.executable} -m backend.main {proctools.ROLE_SERVER}"})
        self.register(proctools.ROLE_SERVER, pid)
        found = launcher.find_running()
        self.assertEqual([p["pid"] for p in found], [pid])
        self.assertEqual(found[0]["role"], proctools.ROLE_SERVER)

    def test_an_unrelated_python_of_the_same_name_is_not_found(self):
        # The shape the old script could not tell apart: a venv python.exe, run
        # from a folder whose path mentions Tacit, serving something else.
        pid = 4002
        self.process_table(alive=[pid], cmdlines={
            pid: r"C:\Users\dev\Tacit-sideproject\.venv\Scripts\python.exe serve.py"})
        self.register(proctools.ROLE_SERVER, pid)
        self.assertEqual(launcher.find_running(), [])

    def test_a_reused_pid_now_running_something_else_is_not_found(self):
        pid = 4003
        self.process_table(alive=[pid], cmdlines={pid: "C:\\Windows\\notepad.exe"})
        self.register(proctools.ROLE_SERVER, pid)
        self.assertEqual(launcher.find_running(), [])

    def test_other_tacit_roles_are_left_alone_by_default(self):
        # A worker and a watcher are provably ours, but "make the port free" does
        # not mean "kill everything with our name on it": the watcher is the
        # process that starts the next server, and a worker holds a turn.
        worker, watcher = 4004, 4005
        self.process_table(alive=[worker, watcher], cmdlines={
            worker: f"{sys.executable} -m backend.turn_worker {proctools.ROLE_WORKER}",
            watcher: f"{sys.executable} -m backend.extras {proctools.ROLE_WATCHER}"})
        self.register(proctools.ROLE_WORKER, worker)
        self.register(proctools.ROLE_WATCHER, watcher)
        self.assertEqual(launcher.find_running(), [])
        self.assertEqual([p["role"] for p in
                          launcher.find_running(roles=(proctools.ROLE_WORKER,))],
                         [proctools.ROLE_WORKER])

    def test_a_dead_pid_is_not_found(self):
        pid = 4006
        self.process_table(alive=[], cmdlines={
            pid: f"{sys.executable} -m backend.main {proctools.ROLE_SERVER}"})
        self.register(proctools.ROLE_SERVER, pid)
        self.assertEqual(launcher.find_running(), [])

    def test_an_unreadable_command_line_is_not_found(self):
        # Alive, registered, unknowable. "Could not read" must never become
        # "assume it is ours", that assumption is a tree kill on a stranger.
        pid = 4007
        self.process_table(alive=[pid], cmdlines={})
        self.register(proctools.ROLE_SERVER, pid)
        self.assertEqual(launcher.find_running(), [])

    def test_this_process_is_not_a_candidate(self):
        pid = os.getpid()
        self.process_table(alive=[pid], cmdlines={
            pid: f"{sys.executable} -m backend.main {proctools.ROLE_SERVER}"})
        self.register(proctools.ROLE_SERVER, pid)
        self.assertEqual(launcher.find_running(), [])

    def test_stop_all_kills_only_the_provable_server_and_clears_its_row(self):
        good, stranger, worker = 4101, 4102, 4103
        self.process_table(alive=[good, stranger, worker], cmdlines={
            good: f"{sys.executable} -m backend.main {proctools.ROLE_SERVER}",
            stranger: r"C:\Python312\python.exe other_app.py",
            worker: f"{sys.executable} -m backend.turn_worker {proctools.ROLE_WORKER}"})
        for role, pid in ((proctools.ROLE_SERVER, good), (proctools.ROLE_SERVER, stranger),
                          (proctools.ROLE_WORKER, worker)):
            self.register(role, pid)
        killed = self.kill_recorder()
        out = launcher.stop_all()
        self.assertEqual(killed, [good])
        self.assertEqual([p["pid"] for p in out["stopped"]], [good])
        self.assertEqual(out["failed"], [])
        self.assertNotIn(good, [r["pid"] for r in proctools.rows()],
                         "a stopped process must not leave a row for the next sweep")

    def test_stop_all_reports_a_kill_that_did_not_take(self):
        pid = 4104
        self.process_table(alive=[pid], cmdlines={
            pid: f"{sys.executable} -m backend.main {proctools.ROLE_SERVER}"})
        self.register(proctools.ROLE_SERVER, pid)
        orig = proctools.kill_tree
        self.addCleanup(setattr, proctools, "kill_tree", orig)
        proctools.kill_tree = lambda p: False
        out = launcher.stop_all()
        self.assertEqual(out["stopped"], [])
        self.assertEqual([p["pid"] for p in out["failed"]], [pid])
        # The row stays: the process is still alive as far as we know, and a
        # registry that forgets live servers is how orphans get made.
        self.assertIn(pid, [r["pid"] for r in proctools.rows()])


# ── waiting for the port ──────────────────────────────────────────────────

class TestWaitPortFree(Home):
    def patch_port_free(self, results):
        orig = launcher.port_free
        self.addCleanup(setattr, launcher, "port_free", orig)
        seen: list[int] = []

        def fake(host, port):
            seen.append(port)
            return results.pop(0) if len(results) > 1 else results[0]

        launcher.port_free = fake
        return seen

    def test_returns_at_once_when_the_port_is_already_free(self):
        seen = self.patch_port_free([True])
        self.assertTrue(launcher.wait_port_free("127.0.0.1", 8550, timeout=5))
        self.assertEqual(len(seen), 1)

    def test_waits_and_retries_until_it_frees(self):
        # A killed server lets go of the port a moment after the kill. One probe
        # would read that moment as "still held" and refuse a restart that was
        # about to work.
        seen = self.patch_port_free([False, False, True])
        started = time.time()
        self.assertTrue(launcher.wait_port_free("127.0.0.1", 8550, timeout=5))
        self.assertEqual(len(seen), 3)
        self.assertGreaterEqual(time.time() - started, 0.4)

    def test_gives_up_at_the_timeout_and_returns_false(self):
        seen = self.patch_port_free([False])
        started = time.time()
        self.assertFalse(launcher.wait_port_free("127.0.0.1", 8550, timeout=0.5))
        self.assertLess(time.time() - started, 5)
        self.assertGreaterEqual(len(seen), 2)

    def test_prelaunch_calls_a_port_that_stayed_held_not_ok(self):
        self.patch_port_free([False])
        out = launcher.prelaunch(host="127.0.0.1", port=self.free_port(), timeout=0.25)
        self.assertFalse(out["ok"])
        self.assertFalse(out["port_free"])
        self.assertEqual(out["waited_s"], 0.25)

    def test_prelaunch_stops_before_it_waits(self):
        calls: list[str] = []
        orig_stop = launcher.stop_all
        self.addCleanup(setattr, launcher, "stop_all", orig_stop)
        launcher.stop_all = lambda *a, **k: (calls.append("stop"),
                                             {"stopped": [], "failed": []})[1]
        self.patch_port_free([True])
        out = launcher.prelaunch(host="127.0.0.1", port=self.free_port(), timeout=1)
        self.assertEqual(calls, ["stop"])
        self.assertTrue(out["ok"])

    def test_the_cli_exit_code_is_what_the_scripts_branch_on(self):
        # run.bat tests `if errorlevel 1`. "The port is still held" therefore has
        # to be a nonzero exit, not a printed warning a person scrolls past.
        orig_load = config.load_env
        self.addCleanup(setattr, config, "load_env", orig_load)
        config.load_env = lambda *a, **k: None
        orig_pre = launcher.prelaunch
        self.addCleanup(setattr, launcher, "prelaunch", orig_pre)
        results: list = []
        launcher.prelaunch = lambda **k: dict(results[-1])
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            results.append({"ok": True, "port": 8550, "port_free": True,
                            "stopped": [], "failed": []})
            self.assertEqual(launcher.main(["prelaunch"]), 0)
            results.append({"ok": False, "port": 8550, "port_free": False,
                            "stopped": [], "failed": []})
            self.assertEqual(launcher.main(["prelaunch"]), 1)
            self.assertEqual(launcher.main(["nonsense"]), 2)
        self.assertIn("still holding port 8550", out.getvalue())


# ── which path starts the next server ─────────────────────────────────────

class TestStartCommand(Home):
    def test_prefers_run_bat_on_windows(self):
        script = self.touch("run.bat")
        self.platform("Windows")
        cmd, via = extras._start_command()
        self.assertEqual(via, "launcher")
        self.assertEqual(cmd, ["cmd.exe", "/c", str(script)])

    def test_prefers_run_sh_on_posix(self):
        script = self.touch("run.sh")
        self.platform("Linux")
        cmd, via = extras._start_command()
        self.assertEqual(via, "launcher")
        self.assertEqual(cmd, ["/bin/sh", str(script)])

    def test_ignores_the_other_platforms_script(self):
        self.touch("run.sh")
        self.platform("Windows")
        cmd, via = extras._start_command()
        self.assertEqual(via, "interpreter")
        self.assertEqual(cmd[:3], [sys.executable, "-m", "backend.main"])

    def test_falls_back_to_this_interpreter_when_there_is_no_script(self):
        # Not a convenience: an embedded run, a test client, or a checkout that
        # never ran install has no launcher, and "no path at all" is the answer
        # that would let the button promise a restart it cannot perform.
        self.platform("Windows")
        cmd, via = extras._start_command()
        self.assertEqual(via, "interpreter")
        self.assertEqual(cmd, [sys.executable, "-m", "backend.main",
                               launcher.SERVER_MARKER])

    def test_both_paths_end_up_with_exactly_one_marker(self):
        # The script puts the token on its own start line; the fallback puts it in
        # the argv it hands the watcher. Either way the running server carries it
        # once; once is what the stop step looks for, and twice is what the
        # watcher used to produce.
        self.platform("Windows")
        self.touch("run.bat")
        self.assertIn(launcher.SERVER_MARKER, self.read_run_bat())
        empty = self.home / "empty"
        empty.mkdir()
        config.ROOT = empty
        cmd, via = extras._start_command()
        self.assertEqual(via, "interpreter")
        self.assertEqual(cmd.count(launcher.SERVER_MARKER), 1)
        spawned = self.fake_popen()
        (config.HOME / extras.HANDSHAKE_FILE).write_text(
            json.dumps({"host": "127.0.0.1", "port": self.free_port(), "pid": 1,
                        "ts": 0, "via": via, "cmd": cmd}), encoding="utf-8")
        orig_busy = extras._port_busy
        self.addCleanup(setattr, extras, "_port_busy", orig_busy)
        extras._port_busy = lambda *a, **k: True
        extras.restart_watch()
        self.assertEqual(spawned[0]["cmd"].count(launcher.SERVER_MARKER), 1)

    def read_run_bat(self) -> str:
        return (REPO / "run.bat").read_text(encoding="utf-8")

    def test_launcher_path_follows_config_root_rather_than_a_hardcoded_one(self):
        self.platform("Windows")
        self.assertIsNone(launcher.launcher_path())
        script = self.touch("run.bat")
        self.assertEqual(launcher.launcher_path(), script)
        other = self.home / "elsewhere"
        other.mkdir()
        (other / "run.bat").write_text("echo x\n", encoding="utf-8")
        config.ROOT = other
        self.assertEqual(launcher.launcher_path(), other / "run.bat")

    def test_restart_server_hands_the_chosen_path_to_the_handshake(self):
        self.platform("Windows")
        script = self.touch("run.bat")
        config.SERVER = types.SimpleNamespace(should_exit=False)
        spawned = self.fake_popen()
        out = io.StringIO()
        with redirect_stdout(out):
            res = extras.restart_server()
        self.assertTrue(res.get("ok"), res)
        hs = json.loads((config.HOME / extras.HANDSHAKE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(hs["via"], "launcher")
        self.assertEqual(hs["cmd"], ["cmd.exe", "/c", str(script)])
        self.assertEqual(res["via"], "launcher")
        self.assertEqual(len(spawned), 1)

    def test_restart_server_refuses_when_there_is_no_path_at_all(self):
        self.platform("Windows")
        orig = extras._server_command
        self.addCleanup(setattr, extras, "_server_command", orig)
        extras._server_command = lambda: None
        config.SERVER = types.SimpleNamespace(should_exit=False)
        res = extras.restart_server()
        self.assertFalse(res["ok"])
        self.assertIn("launcher", res["error"])
        self.assertFalse((config.HOME / extras.HANDSHAKE_FILE).exists(),
                         "a refused restart must not leave a handshake behind")


# ── the watcher runs the path it was handed ───────────────────────────────

class TestWatcherRunsTheChosenPath(Home):
    def drive(self, handshake: dict) -> list:
        (config.HOME / extras.HANDSHAKE_FILE).write_text(
            json.dumps(handshake), encoding="utf-8")
        spawned = self.fake_popen()
        orig_busy = extras._port_busy
        self.addCleanup(setattr, extras, "_port_busy", orig_busy)
        extras._port_busy = lambda *a, **k: True
        extras.restart_watch()
        return spawned

    def test_a_launcher_spawn_runs_the_handshake_command_untouched(self):
        # `proctools._marked_argv` inserts the token after `-m backend.main`.
        # There is no such slot in `cmd.exe /c run.bat` (an injected argument
        # becomes a batch parameter), so the launcher path spawns exactly the
        # argv it was given.
        script = self.touch("run.bat")
        self.platform("Windows")
        cmd = ["cmd.exe", "/c", str(script)]
        spawned = self.drive({"host": "127.0.0.1", "port": self.free_port(),
                              "pid": 1, "ts": 0, "via": "launcher", "cmd": cmd})
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0]["cmd"], cmd)
        self.assertNotIn(proctools.ROLE_SERVER, spawned[0]["cmd"])
        self.assertEqual(spawned[0]["kwargs"].get("cwd"), str(config.ROOT))

    def test_a_launcher_spawn_gets_its_own_console(self):
        # The point of running the script instead of the interpreter: when it
        # fails, run.bat's `pause` leaves a window holding the reason. A hidden
        # child leaves only restart.log, which nobody is standing at.
        script = self.touch("run.bat")
        self.platform("Windows")
        spawned = self.drive({"host": "127.0.0.1", "port": self.free_port(),
                              "pid": 1, "ts": 0, "via": "launcher",
                              "cmd": ["cmd.exe", "/c", str(script)]})
        self.assertEqual(spawned[0]["kwargs"].get("creationflags"),
                         getattr(subprocess, "CREATE_NEW_CONSOLE", 0))

    def test_an_interpreter_spawn_keeps_the_marker_and_stays_headless(self):
        self.platform("Windows")
        cmd = [sys.executable, "-m", "backend.main", proctools.ROLE_SERVER]
        spawned = self.drive({"host": "127.0.0.1", "port": self.free_port(),
                              "pid": 1, "ts": 0, "via": "interpreter", "cmd": cmd})
        self.assertEqual(spawned[0]["cmd"], cmd)
        self.assertFalse(spawned[0]["kwargs"].get("creationflags"))

    def test_an_unmarked_command_still_gets_the_marker(self):
        # The stop step can only protect what it can see, so `mark=False` is for
        # commands that already carry the token, not a blanket. A handshake
        # without one gets it added rather than starting an invisible server.
        self.platform("Windows")
        cmd = [sys.executable, "-m", "backend.main"]
        spawned = self.drive({"host": "127.0.0.1", "port": self.free_port(),
                              "pid": 1, "ts": 0, "via": "interpreter", "cmd": cmd})
        self.assertIn(proctools.ROLE_SERVER, spawned[0]["cmd"])

    def test_an_old_handshake_chooses_through_the_same_function(self):
        # Handshakes written before the path travelled in the file still restart,
        # but they choose with `_start_command` rather than resolving a `python`
        # off PATH in a process whose parent came from a .venv.
        script = self.touch("run.bat")
        self.platform("Windows")
        spawned = self.drive({"host": "127.0.0.1", "port": self.free_port(),
                              "pid": 1, "ts": 0})
        self.assertEqual(spawned[0]["cmd"], ["cmd.exe", "/c", str(script)])

    def test_the_log_names_the_path_that_ran(self):
        script = self.touch("run.bat")
        self.platform("Windows")
        self.drive({"host": "127.0.0.1", "port": self.free_port(), "pid": 1,
                    "ts": 0, "via": "launcher",
                    "cmd": ["cmd.exe", "/c", str(script)]})
        log = (config.HOME / extras.RESTART_LOG).read_text(encoding="utf-8")
        self.assertIn("via launcher", log)


# ── the scripts themselves ────────────────────────────────────────────────

class TestLaunchScripts(unittest.TestCase):
    """One mechanism, and the files that hold it are checkable.

    These read the launcher scripts instead of importing them because the claim
    is about the files. Nobody imports run.bat.
    """

    def read(self, name: str) -> str:
        return (REPO / name).read_text(encoding="utf-8", errors="replace")

    def invoked_modules(self, name: str) -> set:
        """Every module the script runs with `-m`."""
        return set(re.findall(r"-m\s+([A-Za-z_][\w.]*)", self.read(name)))

    def assert_preflight_module(self, name: str) -> None:
        """The preflight step must name a module that exists.

        This is the check that was missing. Every launcher said `-m backend.launch`
        while the module is `backend.launcher`, and `assertIn("backend.launch", text)`
        could not see it because the wrong name is a prefix of the right one, so
        `run.sh` refused to start Tacit at all (its preflight exits nonzero, and the
        script treats that as "a server still holds the port") with this file green.
        Parse the invocation instead of grepping for a word, then prove the module
        resolves.
        """
        mods = self.invoked_modules(name)
        self.assertIn("backend.launcher", mods, f"{name} has no preflight step")
        for mod in sorted(mods):
            if mod.startswith("backend."):
                self.assertIsNotNone(importlib.util.find_spec(mod),
                                     f"{name} runs -m {mod}, which does not exist")

    def test_scripts_preflight_through_the_shared_module(self):
        for name in ("run.bat", "run.sh", "restart.ps1"):
            text = self.read(name)
            self.assertIn("prelaunch", text, name)
            self.assert_preflight_module(name)

    def test_every_line_that_starts_the_server_carries_the_marker(self):
        for name in ("run.bat", "run.sh", "restart.ps1", "install.ps1", "install.sh"):
            for line in self.read(name).splitlines():
                if "-m backend.main" in line:
                    self.assertIn(launcher.SERVER_MARKER, line,
                                  f"{name}: {line.strip()!r} starts an unprovable server")

    def test_no_launcher_script_selects_processes_by_exe_name(self):
        for name in ("run.bat", "run.sh", "restart.ps1"):
            text = self.read(name).lower()
            for banned in ("taskkill", "get-ciminstance", "get-process", "pkill",
                           "name='python.exe'", "wmiclass", "wmic "):
                self.assertNotIn(banned, text, f"{name} stops processes by name")

    def test_restart_ps1_has_no_hardcoded_checkout_path(self):
        # It named this checkout twice: a mechanism that stops working the day
        # the project moves, and the reason a watcher could start a server in the
        # wrong tree.
        text = self.read("restart.ps1")
        self.assertNotIn("C:\\Projects\\Tacit", text)
        self.assertIn("Split-Path", text)

    def test_the_installed_launchers_also_preflight_and_mark(self):
        # ~/.tacit/bin/tacit is the launcher most people actually type, so a
        # server started there has to be as provable as one started by run.bat.
        for name in ("install.ps1", "install.sh"):
            text = self.read(name)
            self.assertIn("prelaunch", text, name)
            self.assert_preflight_module(name)

    def test_the_repo_has_the_scripts_the_launcher_names(self):
        for name in ("run.bat", "run.sh"):
            self.assertTrue((REPO / name).exists(), name)

    def test_no_launcher_file_starts_the_server_without_the_preflight(self):
        # The order is the guarantee: stop what is ours, wait for the port, only
        # then start. A script that starts first is a bind race with extra steps.
        for name in ("run.bat", "run.sh"):
            text = self.read(name)
            self.assertLess(text.index("prelaunch"), text.index("-m backend.main"),
                            f"{name} starts before it checks the port")


class TestRestartState(Home):
    """The outcome of a restart has to survive the process that knows it.

    the deleted plan item 4's last bullet: if the replacement never binds, that needs to
    surface in Settings > Server status, not only in a log file nobody opens. The
    difficulty is *who* reports it. The detached watcher is the only process that
    learns the truth, and when it learns it there is often no server left to
    report through, so the fact is written to `~/.tacit/restart_state.json` and
    handed to whichever server comes back next, including one a person started by
    hand after the failure.

    These tests drive both halves: the recorder and reader, and the watcher's
    terminal branches that call it.
    """

    def state(self, **kwargs) -> dict:
        return extras.restart_state(**kwargs)

    def aged(self, seconds: float) -> None:
        """Rewrite the record's timestamp, so age is a test input not a wait."""
        path = config.HOME / extras.RESTART_STATE_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
        data["attempted_at"] = time.time() - seconds
        path.write_text(json.dumps(data), encoding="utf-8")

    def watch(self, *, busy=True, bindable=True, hold=False, spawn=None,
              handshake_over=None):
        """Run the real watcher against a fake spawn and a fake port."""
        data = {"host": "127.0.0.1", "port": self.free_port(), "pid": 1,
                "ts": time.time(), "via": "interpreter",
                "cmd": [sys.executable, "-m", "backend.main", proctools.ROLE_SERVER]}
        data.update(handshake_over or {})
        (config.HOME / extras.HANDSHAKE_FILE).write_text(json.dumps(data), encoding="utf-8")

        for name, value in (("_port_busy", lambda *a, **k: busy),
                            ("_can_bind", lambda *a, **k: bindable and not hold)):
            orig = getattr(extras, name)
            self.addCleanup(setattr, extras, name, orig)
            setattr(extras, name, value)
        if hold:
            orig = extras.PORT_FREE_TIMEOUT
            self.addCleanup(setattr, extras, "PORT_FREE_TIMEOUT", orig)
            extras.PORT_FREE_TIMEOUT = 0.1   # the real wait is 30s; the branch is the same
        if spawn is not None:
            orig = proctools.spawn
            self.addCleanup(setattr, proctools, "spawn", orig)
            proctools.spawn = spawn
        else:
            self.fake_popen()
        extras.restart_watch()
        return data

    # ── the record itself ─────────────────────────────────────────────────

    def test_an_outcome_is_written_and_read_back(self):
        extras.record_restart_state("failed", stage="bind", detail="never accepted",
                                    via="launcher", cmd=["cmd.exe", "/c", "run.bat"],
                                    port=8756)
        got = self.state()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["stage"], "bind")
        self.assertEqual(got["detail"], "never accepted")
        self.assertEqual(got["via"], "launcher")
        self.assertEqual(got["cmd"], ["cmd.exe", "/c", "run.bat"])
        self.assertEqual(got["port"], 8756)
        self.assertEqual(got["watcher"], os.getpid())
        # Written through a temp file and renamed, so a reader never sees half a
        # restart, and the temp file must not be left beside it.
        self.assertFalse((config.HOME / (extras.RESTART_STATE_FILE + ".tmp")).exists())

    def test_nothing_recorded_reads_as_nothing(self):
        self.assertEqual(self.state(), {})

    def test_an_unreadable_record_reads_as_nothing(self):
        # A panel that throws on a hand-edited or half-written file is worse than
        # one that says nothing.
        path = config.HOME / extras.RESTART_STATE_FILE
        path.write_text("not json at all", encoding="utf-8")
        self.assertEqual(self.state(), {})
        path.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(self.state(), {})
        extras.record_restart_state("ok", port=1)
        self.assertEqual(self.state()["state"], "ok")

    def test_a_timestamp_that_is_not_a_time_reads_as_nothing(self):
        extras.record_restart_state("failed", stage="bind")
        path = config.HOME / extras.RESTART_STATE_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
        data["attempted_at"] = "yesterday"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.state(), {})

    def test_an_old_outcome_stops_being_news(self):
        # A failure is reported for minutes, not for ever; after that it belongs
        # in the audit log, not in the panel someone opens to check a port.
        extras.record_restart_state("failed", stage="bind")
        self.aged(extras.RESTART_STATE_TTL + 60)
        self.assertEqual(self.state(), {})
        self.assertEqual(self.state(max_age=extras.RESTART_STATE_TTL + 120)["state"], "failed")

    def test_a_future_timestamp_reads_as_nothing(self):
        # A clock that jumped backwards must not make the panel report a restart
        # that has not been attempted yet.
        extras.record_restart_state("failed", stage="bind")
        self.aged(-3600)
        self.assertEqual(self.state(), {})

    def test_a_fresh_attempt_reads_as_in_flight(self):
        extras.record_restart_state("starting", via="launcher", port=8756)
        self.assertEqual(self.state()["state"], "starting")

    def test_a_watcher_that_never_reported_reads_as_stalled(self):
        # The failure mode with no process left to record it: the watcher died
        # mid-flight and `starting` stands uncorrected. Past the point where every
        # branch of restart_watch would have reported, silence IS the failure.
        extras.record_restart_state("starting", via="launcher", port=8756)
        self.aged(extras.PORT_FREE_TIMEOUT + extras.COMEBACK_TIMEOUT + 120)
        got = self.state()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["stage"], "stalled")
        self.assertEqual(got["via"], "launcher")   # the hint a person needs survives

    # ── the watcher's terminal branches ───────────────────────────────────

    def test_a_server_that_comes_back_is_recorded_as_ok(self):
        data = self.watch()
        got = self.state()
        self.assertEqual(got["state"], "ok")
        self.assertEqual(got["via"], data["via"])
        self.assertEqual(got["port"], data["port"])
        self.assertIn("9001", got["detail"])

    def test_the_failure_the_plan_named_is_recorded(self):
        # Started, never bound: the exact case that used to exist only in
        # restart.log.
        data = self.watch(busy=False)
        got = self.state()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["stage"], "bind")
        self.assertIn(str(data["port"]), got["detail"])
        self.assertEqual(got["cmd"], data["cmd"])
        self.assertEqual(got["via"], "interpreter")

    def test_a_server_that_binds_then_crashes_is_not_reported_as_ok(self):
        # It got as far as listening, so the bind branch said `ok`, and then died.
        # Reporting ok after that would be a lie by the time anyone reads it.
        crasher = lambda *a, **k: types.SimpleNamespace(
            pid=9002, wait=lambda: 1, returncode=1)
        self.watch(spawn=crasher)
        got = self.state()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["stage"], "exit")
        self.assertIn("9002", got["detail"])

    def test_a_clean_exit_leaves_the_bind_outcome_alone(self):
        # The watcher blocks on its child for the life of the server. When that
        # server is later stopped on purpose (exit 0), rewriting the record to
        # `failed` would report a restart failure that never happened.
        calm = lambda *a, **k: types.SimpleNamespace(
            pid=9003, wait=lambda: 0, returncode=0)
        self.watch(spawn=calm)
        self.assertEqual(self.state()["state"], "ok")

    def test_a_spawn_that_throws_is_recorded_with_its_reason(self):
        def boom(*a, **k):
            raise OSError("no such interpreter")
        self.watch(spawn=boom)
        got = self.state()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["stage"], "spawn")
        self.assertIn("no such interpreter", got["detail"])

    def test_a_port_that_never_frees_is_recorded(self):
        # Nothing was started, so there is no pid and no console; the only trace
        # that this restart happened at all would be a log line.
        self.watch(hold=True)
        got = self.state()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["stage"], "port")
        self.assertIn("held", got["detail"])

    def test_a_handshake_with_no_port_is_recorded(self):
        self.watch(handshake_over={"port": 0})
        got = self.state()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["stage"], "handshake")

    def test_a_missing_handshake_is_recorded(self):
        # The watcher was spawned but the handshake file is gone (deleted by a
        # sweep, or never written). It gives up; the panel must not then claim a
        # restart is in flight forever.
        orig = extras._port_busy
        self.addCleanup(setattr, extras, "_port_busy", orig)
        extras._port_busy = lambda *a, **k: True
        extras.restart_watch()
        self.assertEqual(self.state()["stage"], "handshake")

    def test_no_command_left_behind_is_recorded(self):
        # Patched BEFORE the watcher runs: restart_watch resolves the start command
        # synchronously, so a patch applied after the call would be recording the
        # outcome of a restart that already succeeded.
        orig = extras._server_command
        self.addCleanup(setattr, extras, "_server_command", orig)
        extras._server_command = lambda: []
        self.watch(handshake_over={"cmd": []})
        got = self.state()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["stage"], "start")

    # ── the side that asks for the restart ────────────────────────────────

    def test_asking_for_a_restart_records_an_attempt_in_flight(self):
        self.platform("Windows")
        self.touch("run.bat")
        config.SERVER = types.SimpleNamespace(should_exit=False)
        self.fake_popen()
        with redirect_stdout(io.StringIO()):
            res = extras.restart_server()
        self.assertTrue(res.get("ok"), res)
        got = self.state()
        self.assertEqual(got["state"], "starting")
        self.assertEqual(got["via"], "launcher")
        self.assertEqual(got["port"], config.PORT)

    def test_a_watcher_that_could_not_be_spawned_is_recorded(self):
        self.platform("Windows")
        self.touch("run.bat")
        config.SERVER = types.SimpleNamespace(should_exit=False)
        orig = extras.subprocess.Popen
        self.addCleanup(setattr, extras.subprocess, "Popen", orig)

        def boom(*a, **k):
            raise OSError("cannot spawn")
        extras.subprocess.Popen = boom
        with redirect_stdout(io.StringIO()):
            res = extras.restart_server()
        self.assertFalse(res["ok"])
        got = self.state()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["stage"], "watcher")
        self.assertIn("cannot spawn", got["detail"])
        self.assertFalse(getattr(config.SERVER, "should_exit", False),
                         "a restart that could not be watched must not exit the server")

    # ── what the Settings panel actually gets ─────────────────────────────

    def test_server_status_carries_the_last_restart(self):
        # `server_status` is a plain `def` route. Calling it through asyncio.run
        # raises "a coroutine was expected" before any assertion can speak, so the
        # sync call is made directly.
        from backend.routers import api
        self.assertEqual(api.server_status()["last_restart"], {})
        extras.record_restart_state("failed", stage="bind", detail="never accepted",
                                    via="launcher", cmd=["cmd.exe", "/c", "run.bat"],
                                    port=8756)
        out = api.server_status()
        self.assertEqual(out["last_restart"]["state"], "failed")
        self.assertEqual(out["last_restart"]["stage"], "bind")
        self.assertEqual(out["last_restart"]["cmd"], ["cmd.exe", "/c", "run.bat"])


if __name__ == "__main__":
    unittest.main()

"""Cancellation through the DEFAULT server path, not direct sandbox calls.

The unit suites prove `sandbox.run` kills its tree. The guarantee the interface
makes is bigger: a person presses Stop on a session whose turn runs in the
default process worker, and the worker, the shell it started, and that shell's
descendants all die. The process boundary is part of the guarantee, so these
tests drive `session_runtime.start_turn` / `session_runtime.cancel` against a
real worker subprocess and a scripted local model, in both worker modes, and
with Windows job containment forced to fail.

The model is faked at the only seam that exists across a process boundary: a
local HTTP server speaking the OpenAI SSE shape. Its first response calls
`run_shell`; the shell writes its own pid and its child's pid to a file the
test can read, then sleeps. Nothing here needs a real provider.
"""

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from backend import config, proctools, session_runtime, session_state, store, turn_worker
from tests.fake_provider import FakeProvider
from tests.helpers import drain_turns, state_paths

ROOT = Path(__file__).resolve().parents[1]


class ScriptedModel:
    """The cancellation scenario's script: first a run_shell call, then text.

    A thin wrapper over the reusable FakeProvider; ``hang=True`` accepts the
    request and never answers, the shape of a model read a stop flag cannot
    interrupt from inside.
    """

    def __init__(self, command: str = "", timeout: int = 60, hang: bool = False):
        script = ([{"hang": True}] if hang else
                  [{"tool": {"name": "run_shell", "id": "call_shell_1",
                             "arguments": {"command": command, "timeout": timeout}}},
                   {"text": "all done"}])
        self._fp = FakeProvider(script)
        self.requests = self._fp.requests
        self.request_seen = self._fp.request_seen
        self.release = self._fp.release
        self.port = self._fp.port

    def stop(self):
        self._fp.stop()

    def register(self):
        self._fp.register()


class RuntimeFixture(unittest.TestCase):
    """Redirected home, real worker mode, and a session record to drive."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.work = self.home / "work"
        self.work.mkdir()
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        self.addCleanup(self._restore_config)
        config.HOME = self.home
        for k in self._keys:
            target = self.home / Path(self._orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        self._env = {}
        for key in ("TACIT_TURN_WORKER", "TACIT_HOME", "TACIT_DISABLE_JOB_OBJECTS",
                    "TACIT_WORKER_MAX_S", "TACIT_MANIFEST"):
            self._env[key] = os.environ.get(key)
        self.addCleanup(self._restore_env)
        os.environ["TACIT_HOME"] = str(self.home)
        os.environ["TACIT_TURN_WORKER"] = "process"
        session_state.reset()
        self.addCleanup(session_state.reset)
        self.addCleanup(drain_turns)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self._leftover_pids: list[int] = []
        self.addCleanup(self._kill_leftovers)

    def _restore_config(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)

    def _restore_env(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _kill_leftovers(self):
        for pid in self._leftover_pids:
            if pid and proctools._pid_alive(pid):
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                                       stdin=subprocess.DEVNULL, capture_output=True,
                                       timeout=15)
                    else:
                        os.kill(pid, 9)
                except (OSError, subprocess.SubprocessError):
                    pass

    def _session(self, sid: str) -> dict:
        # create(), not save(): save() refuses a record whose file AND index
        # row are both gone (the anti-resurrection guard), so a save-first
        # fixture writes nothing and the transcript never lands on disk.
        rec = store.create(title="cancel test", model="p/m", mode="agent",
                           project=str(self.work), sid=sid)
        self.assertIsNotNone(rec)
        return rec

    def _start(self, sid: str, rec: dict) -> dict:
        out = session_runtime.start_turn(sid, rec, "run the script", loop=self.loop)
        self.assertTrue(out.get("ok"), out)
        return out

    def _wait_pids(self, pidfile: Path, timeout: float = 30.0) -> tuple[int, int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                text = pidfile.read_text(encoding="utf-8").strip()
                if text:
                    root_pid, child_pid = (int(p) for p in text.split())
                    self._leftover_pids += [root_pid, child_pid]
                    return root_pid, child_pid
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        self.fail(f"the shell never reported its pids in {pidfile}")

    def _wait_dead(self, pid: int, timeout: float = 12.0):
        deadline = time.monotonic() + timeout
        while proctools._pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(proctools._pid_alive(pid), f"process {pid} survived")

    def _tree_script(self, pidfile: Path, hold_pipe: bool = False) -> str:
        """A shell command that spawns a sleeper and reports its pids.

        hold_pipe: the root exits at once and the child keeps the captured
        stdout open, the exact shape a job-less timeout cannot contain. The
        child's pid goes to the file as well as stdout, so teardown can find
        it even when the assertion that reads the result fails.
        """
        script = self.work / "tree.py"
        if hold_pipe:
            body = (
                "import subprocess, sys\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(300)'])\n"
                f"with open({str(pidfile)!r}, 'w') as fh:\n"
                "    fh.write(str(child.pid))\n"
                "print(f'CHILD_PID={child.pid}', flush=True)\n")
        else:
            body = (
                "import os, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(300)'], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL)\n"
                f"with open({str(pidfile)!r}, 'w') as fh:\n"
                "    fh.write(f'{os.getpid()} {child.pid}')\n"
                "time.sleep(300)\n")
        script.write_text(body, encoding="utf-8")
        return f'"{sys.executable}" "{script}"'


class TestDefaultWorkerCancel(RuntimeFixture):
    """Stop through the public runtime control, in the default worker mode."""

    def test_cancel_kills_the_worker_the_shell_and_the_descendant(self):
        pidfile = self.work / "pids.txt"
        model = ScriptedModel(command=self._tree_script(pidfile))
        self.addCleanup(model.stop)
        model.register()
        rec = self._session("cancel-proc")
        self._start("cancel-proc", rec)
        root_pid, child_pid = self._wait_pids(pidfile)
        run = session_runtime._turns.get("cancel-proc")
        self.assertIsNotNone(run and run.get("worker"),
                             "the default mode must run a process worker")
        worker_pid = run["worker"].proc.pid
        self._leftover_pids.append(worker_pid)
        self.assertTrue(proctools._pid_alive(root_pid))
        self.assertTrue(proctools._pid_alive(child_pid))
        started = time.monotonic()
        out = session_runtime.cancel("cancel-proc")
        self.assertIn(out["turn"]["how"], ("cancelled", "killed"), out)
        self.assertLess(time.monotonic() - started, 12.0,
                        "stop must not wait out the command")
        self._wait_dead(worker_pid)
        self._wait_dead(root_pid)
        self._wait_dead(child_pid)

    def test_cancel_in_thread_mode_kills_the_same_tree(self):
        os.environ["TACIT_TURN_WORKER"] = "thread"
        pidfile = self.work / "pids.txt"
        model = ScriptedModel(command=self._tree_script(pidfile))
        self.addCleanup(model.stop)
        model.register()
        rec = self._session("cancel-thread")
        self._start("cancel-thread", rec)
        root_pid, child_pid = self._wait_pids(pidfile)
        out = session_runtime.cancel("cancel-thread")
        self.assertEqual(out["turn"]["how"], "no-worker", out)
        self._wait_dead(root_pid)
        self._wait_dead(child_pid)

    @unittest.skipUnless(os.name == "nt", "job objects are a Windows mechanism")
    def test_forced_job_failure_reports_containment_failure(self):
        """With assignment forced to fail, a root-exit/pipe-holding-child
        timeout must report that the tree could not be terminated, not claim a
        guaranteed kill. The child really does survive; the result says so,
        and the test cleans it up by the pid the result carried."""
        os.environ["TACIT_DISABLE_JOB_OBJECTS"] = "1"
        pidfile = self.work / "child.txt"
        model = ScriptedModel(command=self._tree_script(pidfile, hold_pipe=True),
                              timeout=2)
        self.addCleanup(model.stop)
        model.register()
        rec = self._session("cancel-nojob")
        self._start("cancel-nojob", rec)
        # Register the survivor for teardown before anything can fail.
        deadline = time.monotonic() + 30
        child_pid = 0
        while time.monotonic() < deadline and not child_pid:
            try:
                child_pid = int(pidfile.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                pass
            if not child_pid:
                time.sleep(0.05)
        self.assertTrue(child_pid, "the shell never reported its child")
        self._leftover_pids.append(child_pid)
        deadline = time.monotonic() + 60
        result_text = ""
        while time.monotonic() < deadline:
            rows = (store.get("cancel-nojob") or {}).get("messages") or []
            for row in rows:
                for tool in row.get("tools") or []:
                    if tool.get("name") == "run_shell":
                        result_text = str(tool.get("result") or "")
            if result_text:
                break
            time.sleep(0.1)
        self.assertTrue(result_text, "the shell result never reached the transcript")
        self.assertIn("could not terminate the whole command tree", result_text)
        self.assertTrue(result_text.startswith("ERROR:"),
                        "a containment failure must not read as success")
        match = re.search(r"CHILD_PID=(\d+)", result_text)
        self.assertIsNotNone(match, result_text)
        self.assertEqual(int(match.group(1)), child_pid)
        # The honest failure: without a job there is no boundary left to kill
        # once the root exited, so the descendant is still alive.
        self.assertTrue(proctools._pid_alive(child_pid))


class TestWorkerBackstops(RuntimeFixture):
    """The worker bounds itself when no cancel can reach it."""

    def _worker(self, model: ScriptedModel, sid: str) -> turn_worker.TurnWorker:
        model.register()
        w = turn_worker.spawn(sid)
        ok = w.start(project=None, ref="p/m", chat=False, session=sid,
                     has_instructions=False, reasoning=None, max_steps=2,
                     messages=[{"role": "system", "content": "s"},
                               {"role": "user", "content": "go"}],
                     trace=[])
        self.assertTrue(ok)
        self._leftover_pids.append(w.proc.pid)
        return w

    def test_the_watchdog_stops_a_worker_that_outlives_its_ceiling(self):
        os.environ["TACIT_WORKER_MAX_S"] = "2"
        model = ScriptedModel(hang=True)
        self.addCleanup(model.stop)
        w = self._worker(model, "watchdog")
        self.assertTrue(model.request_seen.wait(30), "the worker never called the model")
        events = list(w.events())
        errors = [e.get("message") or "" for e in events if e.get("type") == "error"]
        self.assertTrue(any("hard ceiling" in m for m in errors), errors)
        w.proc.wait(timeout=10)
        self.assertEqual(w.proc.returncode, 3,
                         "a ceiling hit is an infrastructure failure, not a completion")

    def test_parent_loss_winds_the_worker_down(self):
        """EOF on the control pipe without a cancel means the parent is gone.

        The parent process here stays alive (closing stdin simulates the loss
        without the job object taking the worker), so the worker's own
        parent-loss path is what must end it, inside the grace plus margin.
        """
        model = ScriptedModel(hang=True)
        self.addCleanup(model.stop)
        w = self._worker(model, "orphan")
        self.assertTrue(model.request_seen.wait(30), "the worker never called the model")
        w.proc.stdin.close()
        try:
            w.proc.wait(timeout=turn_worker.PARENT_LOSS_GRACE_S + 8)
        except subprocess.TimeoutExpired:
            w.kill()
            self.fail("the worker outlived its parent-loss grace")
        self.assertEqual(w.proc.returncode, 0)
        # Drain the event stream the way the driver does, so the pipes are
        # closed by the same path production uses.
        list(w.events())


if __name__ == "__main__":
    unittest.main()
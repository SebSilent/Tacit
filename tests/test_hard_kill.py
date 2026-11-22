"""Hard-kill ownership: a worker's death must not orphan its shells.

A POSIX shell runs in its own process group, outside the worker's kill group,
so a hard kill that bypasses cooperative cleanup leaves it running unless the
parent can see what the worker registered and settle it. These tests prove
the whole chain: the registry row carries the owner, the group id and the
process's birth identity; the parent settles a dead worker's rows; the startup
sweep settles a dead owner's rows; and a reused pid with a wrong identity is
refused, never killed.

On Windows the nested job object's kill-on-close is the first line of the same
guarantee: the worker's death closes its handle and the kernel takes the shell
tree. The tests assert the outcome on both platforms, and the refusal path is
identical.
"""

import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

from backend import config, proctools, session_runtime
from tests.test_worker_cancel_integration import RuntimeFixture, ScriptedModel

ROOT = Path(__file__).resolve().parents[1]


def _kill_pid(pid: int) -> None:
    if not pid or not proctools._pid_alive(pid):
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        else:
            os.kill(pid, 9)
    except (OSError, subprocess.SubprocessError):
        pass


class TestHardKillOwnership(RuntimeFixture):
    def test_hard_killing_the_worker_leaves_no_shell_group(self):
        """Kill ONLY the worker process, bypassing every cooperative path.

        The driver's event stream hits EOF, and the parent's settle step must
        take the shell root and its descendant with it (POSIX), or verify the
        kernel already did (Windows).
        """
        pidfile = self.work / "pids.txt"
        model = ScriptedModel(command=self._tree_script(pidfile))
        self.addCleanup(model.stop)
        model.register()
        rec = self._session("hard-kill")
        self._start("hard-kill", rec)
        root_pid, child_pid = self._wait_pids(pidfile)
        run = session_runtime._turns.get("hard-kill")
        worker = run.get("worker")
        if worker is None:
            self.skipTest("the default process worker is not in use")
        worker_pid = worker.proc.pid
        self._leftover_pids.append(worker_pid)
        # Hard kill, no /T, no cancel line: the worker's finally never runs.
        _kill_pid(worker_pid)
        self._wait_dead(worker_pid)
        self._wait_dead(root_pid)
        self._wait_dead(child_pid)

    def test_the_sweep_settles_a_dead_owners_shell_group(self):
        """A helper process registers a shell, then dies without cleanup.

        Its registry file is the only proof of what it was running; the next
        boot's sweep reads it, proves the group's identity, and kills it.
        """
        pidfile = self.work / "pids.txt"
        wrapper = self.work / "wrapper.py"
        wrapper.write_text(
            "import os, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(300)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL)\n"
            f"with open({str(pidfile)!r}, 'w') as fh:\n"
            "    fh.write(f'{os.getpid()} {child.pid}')\n"
            "time.sleep(300)\n",
            encoding="utf-8")
        helper = (
            "import os, subprocess, sys, time\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from backend import proctools\n"
            f"proc = proctools.spawn('tacit-shell', [sys.executable, {str(wrapper)!r}],\n"
            "    session='sweep-test', contain=True, mark=False,\n"
            "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            "    **({'start_new_session': True} if os.name != 'nt' else {}))\n"
            "print('HELPER_SPAWNED', proc.pid, flush=True)\n"
            # Wait until the shell has reported before dying: on Windows the
            # helper's exit closes the job handle and takes the shell with it,
            # so an immediate exit would kill the shell before it ever ran.
            f"deadline = time.time() + 20\n"
            f"while time.time() < deadline and not os.path.exists({str(pidfile)!r}):\n"
            "    time.sleep(0.05)\n"
            "os._exit(0)\n")
        env = dict(os.environ)
        env["TACIT_HOME"] = str(self.home)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        res = subprocess.run([sys.executable, "-c", helper], cwd=ROOT, env=env,
                             stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        # The registered root is the spawned process itself. On Windows a venv
        # python.exe is a launcher whose child runs the script, so the pid the
        # script reports is not the root the registry holds.
        match = re.search(r"HELPER_SPAWNED (\d+)", res.stdout)
        self.assertIsNotNone(match, res.stdout)
        registered_pid = int(match.group(1))
        root_pid, child_pid = self._wait_pids(pidfile)
        self._leftover_pids.append(registered_pid)
        if os.name != "nt":
            # POSIX: the group outlives its owner until somebody settles it.
            # Windows: the helper's exit already closed the job handle, so the
            # kernel may have taken the tree before the sweep runs.
            self.assertTrue(proctools._pid_alive(child_pid))
        report = proctools.sweep()
        self.assertTrue(any(r.get("pid") == registered_pid
                            for r in report["killed"] + report["dropped"]),
                        f"the sweep did not settle the shell row: {report}")
        if os.name != "nt":
            self.assertTrue(any(r.get("pid") == registered_pid for r in report["killed"]),
                            f"the sweep did not kill the orphaned group: {report}")
        self._wait_dead(registered_pid)
        self._wait_dead(root_pid)
        self._wait_dead(child_pid)

    def test_the_sweep_refuses_a_reused_pid_with_a_wrong_identity(self):
        """A row whose pid now belongs to a stranger is left alone.

        The identity in the row does not match the live process's birth, so
        the kill is refused and the row kept. The stranger must survive.
        """
        stranger = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **({"start_new_session": True} if os.name != "nt" else {}))

        def _stop_stranger():
            if stranger.poll() is None:
                stranger.kill()
            stranger.wait(timeout=10)
        self.addCleanup(_stop_stranger)
        # A provably dead owner: a short-lived child, waited out.
        dead = subprocess.Popen([sys.executable, "-c", "pass"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        dead.wait(timeout=30)
        row = {"pid": stranger.pid, "role": "tacit-shell", "session": "reuse",
               "started": time.time(), "cmdline": "python stranger.py",
               "owner": dead.pid, "identity": 12345}
        if os.name != "nt":
            row["pgid"] = stranger.pid
        config.write_json(proctools._path(dead.pid), {"rows": [row]})
        report = proctools.sweep()
        self.assertFalse(any(r.get("pid") == stranger.pid for r in report["killed"]),
                         "a wrong-identity row must never produce a kill")
        self.assertTrue(proctools._pid_alive(stranger.pid),
                        "the stranger was killed on an unproven row")
        kept = proctools._load_file(proctools._path(dead.pid))
        self.assertTrue(any(r.get("pid") == stranger.pid for r in kept),
                        "an unproven row is kept, not silently dropped")

    def test_cleanup_drops_background_rows_without_killing_them(self):
        """Background jobs outlive the turn by design; the jobs registry owns
        them, so a dead worker's process row is dropped, not killed."""
        dead = subprocess.Popen([sys.executable, "-c", "pass"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        dead.wait(timeout=30)
        row = {"pid": 999999, "role": "tacit-bg", "session": "bg",
               "started": time.time(), "cmdline": "python job.py",
               "owner": dead.pid}
        config.write_json(proctools._path(dead.pid), {"rows": [row]})
        report = proctools.cleanup_owner_shells(dead.pid)
        self.assertEqual(report["killed"], [])
        self.assertFalse(proctools._path(dead.pid).exists(),
                         "a file with only background rows is settled, not kept")


class TestProcessIdentity(unittest.TestCase):
    def test_identity_is_stable_and_distinct_per_process(self):
        me = proctools.process_identity(os.getpid())
        if me is None:
            self.skipTest("this platform exposes no process birth identity")
        self.assertEqual(proctools.process_identity(os.getpid()), me,
                         "a process's birth identity must not move")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        try:
            other = proctools.process_identity(child.pid)
            self.assertIsNotNone(other)
            self.assertNotEqual(other, me,
                                "two processes must not share a birth identity")
        finally:
            child.kill()
            child.wait(timeout=10)

    def test_a_reused_pid_never_reads_as_the_same_birth(self):
        import gc
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        alive_id = proctools.process_identity(child.pid)
        child_pid_saved = child.pid
        child.kill()
        child.wait(timeout=10)
        # Drop the Popen reference too: on Windows a held process handle keeps
        # the process object (and its creation time) readable after death.
        del child
        gc.collect()
        if alive_id is None:
            self.skipTest("this platform exposes no process birth identity")
        # The pid may be reused by the OS at any moment; the contract is that
        # a gone process reads as no identity, and a reused one reads as a
        # DIFFERENT birth, never as a match.
        after = proctools.process_identity(child_pid_saved)
        self.assertTrue(after is None or after != alive_id,
                        "a dead or reused pid must never match the recorded birth")


if __name__ == "__main__":
    unittest.main()
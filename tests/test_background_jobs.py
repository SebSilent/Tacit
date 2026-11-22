"""Real-process coverage for persistent, session-owned background jobs."""

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backend import config, extras, proctools
from tests.helpers import state_paths

ROOT = Path(__file__).resolve().parents[1]


def command(*args: str) -> str:
    return subprocess.list2cmdline(list(args)) if os.name == "nt" else shlex.join(args)


class TestBackgroundJobs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.work = self.home / "work"
        self.work.mkdir()
        self._keys = state_paths(config)
        self._orig = {key: getattr(config, key) for key in self._keys}
        self._home = config.HOME
        self.addCleanup(self._restore_config)
        config.HOME = self.home
        for key in self._keys:
            target = self.home / Path(self._orig[key]).name
            setattr(config, key, target)
            if key.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        self.env = os.environ.copy()
        self.env["TACIT_HOME"] = str(self.home)
        self.env["PYTHONPATH"] = str(ROOT) + os.pathsep + self.env.get("PYTHONPATH", "")
        self.jobs: dict[str, dict] = {}
        self.descendants: set[int] = set()
        self._old_bg = extras.BG.copy()
        extras.BG.clear()
        self.addCleanup(self._cleanup_jobs)

    def _restore_config(self):
        config.HOME = self._home
        for key, value in self._orig.items():
            setattr(config, key, value)

    @staticmethod
    def _alive(pid: int) -> bool:
        return proctools._pid_alive(pid)

    def _wait_dead(self, pid: int):
        deadline = time.monotonic() + 10
        while self._alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self._alive(pid), f"process {pid} is still running")

    def _force_stop(self, pid: int):
        if not pid or not self._alive(pid):
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
            else:
                os.kill(pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass

    def _cleanup_jobs(self):
        # Discover a descendant even when the assertion that would have parsed it failed.
        for rec in self.jobs.values():
            try:
                text = Path(rec["output"]).read_text(encoding="utf-8", errors="replace")
                self.descendants.update(int(pid) for pid in re.findall(r"DESCENDANT=(\d+)", text))
            except OSError:
                pass
        for bid in list(self.jobs):
            try:
                extras.bg_stop(bid)
            except Exception:
                pass
        for rec in self.jobs.values():
            self._force_stop(int(rec["pid"]))
        for pid in self.descendants:
            self._force_stop(pid)
        extras.BG.clear()
        extras.BG.update(self._old_bg)

    def _remember(self, bid: str) -> dict:
        rec = json.loads((config.JOBS_DIR / f"{bid}.json").read_text(encoding="utf-8"))
        self.assertTrue({"id", "pid", "pgid", "session", "command", "cwd", "started",
                         "output"}.issubset(rec))
        self.assertEqual(rec["id"], bid)
        self.jobs[bid] = rec
        return rec

    def _start(self, cmd: str, session: str) -> tuple[str, dict]:
        result = extras.bg_start(cmd, cwd=str(self.work), session=session)
        match = re.search(r"started \[([0-9a-f]+)\]", result)
        self.assertIsNotNone(match, result)
        bid = match.group(1)
        return bid, self._remember(bid)

    def _remote(self, code: str, *args: str) -> str:
        result = subprocess.run(
            [sys.executable, "-c", code, *args], cwd=ROOT, env=self.env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout.strip()

    def _remote_start(self, cmd: str, session: str) -> tuple[str, dict]:
        result = self._remote(
            "import sys; from backend import extras; "
            "print(extras.bg_start(sys.argv[1], cwd=sys.argv[2], session=sys.argv[3]))",
            cmd, str(self.work), session)
        match = re.search(r"started \[([0-9a-f]+)\]", result)
        self.assertIsNotNone(match, result)
        bid = match.group(1)
        return bid, self._remember(bid)

    def _output_with(self, bid: str, token: str, tail: int = 4000) -> str:
        deadline = time.monotonic() + 15
        output = ""
        while time.monotonic() < deadline:
            output = extras.bg_output(bid, tail=tail)
            if token in output:
                return output
            time.sleep(0.05)
        self.fail(f"{token!r} missing from {bid}: {output}")

    def test_cross_process_output_survives_starter_and_cleared_cache(self):
        cmd = command(
            sys.executable, "-c",
            "import time; print('prefix-should-be-clipped'); print('x' * 10000); "
            "print('cross-process-ready', flush=True); time.sleep(120)")
        bid, rec = self._remote_start(cmd, "reader-session")
        # The process which owned Popen has exited, and this process has no cache row.
        extras.BG.clear()
        self.assertTrue(self._alive(rec["pid"]))
        output = self._output_with(bid, "cross-process-ready", tail=80)
        self.assertIn("running", output)
        self.assertIn("...(truncated)", output)
        self.assertNotIn("prefix-should-be-clipped", output)
        remote = self._remote(
            "import sys; from backend import extras; print(extras.bg_output(sys.argv[1]))", bid)
        self.assertIn("cross-process-ready", remote)

    def test_cross_process_stop_uses_the_persistent_record(self):
        cmd = command(sys.executable, "-c",
                      "import time; print('stop-me', flush=True); time.sleep(120)")
        bid, rec = self._start(cmd, "stop-session")
        self._output_with(bid, "stop-me")
        extras.BG.clear()
        result = self._remote(
            "import sys; from backend import extras; print(extras.bg_stop(sys.argv[1]))", bid)
        self.assertEqual(result, f"stopped [{bid}]")
        self._wait_dead(rec["pid"])
        self.assertIn("stopped", extras.bg_output(bid))
        self.assertEqual(extras.bg_stop(bid), f"already stopped [{bid}]")

    def test_failed_termination_is_not_reported_as_stopped(self):
        cmd = command(sys.executable, "-c", "import time; time.sleep(120)")
        bid, rec = self._start(cmd, "failure-session")
        with (mock.patch.object(extras, "_signal_tree", return_value=(False, "injected failure")),
              mock.patch.object(extras, "JOB_STOP_TIMEOUT", 0.1)):
            result = extras.bg_stop(bid)
        self.assertIn("ERROR: could not stop", result)
        self.assertNotIn(f"stopped [{bid}]", result)
        self.assertTrue(self._alive(rec["pid"]))
        stored = json.loads((config.JOBS_DIR / f"{bid}.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["state"], "running")

    def test_stop_terminates_the_root_and_its_descendant(self):
        parent = self.work / "job_parent.py"
        parent.write_text(
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(120)'], stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "print(f'DESCENDANT={child.pid}', flush=True)\n"
            "time.sleep(120)\n",
            encoding="utf-8")
        bid, rec = self._start(command(sys.executable, str(parent)), "tree-session")
        output = self._output_with(bid, "DESCENDANT=")
        child = int(re.search(r"DESCENDANT=(\d+)", output).group(1))
        self.descendants.add(child)
        self.assertTrue(self._alive(rec["pid"]))
        self.assertTrue(self._alive(child))
        self.assertEqual(extras.bg_stop(bid), f"stopped [{bid}]")
        self._wait_dead(rec["pid"])
        self._wait_dead(child)

    def test_session_stop_is_persistent_and_isolated(self):
        cmd_a = command(sys.executable, "-c",
                        "import time; print('job-a', flush=True); time.sleep(120)")
        cmd_b = command(sys.executable, "-c",
                        "import time; print('job-b', flush=True); time.sleep(120)")
        bid_a, rec_a = self._start(cmd_a, "session-a")
        bid_b, rec_b = self._start(cmd_b, "session-b")
        self._output_with(bid_a, "job-a")
        self._output_with(bid_b, "job-b")
        extras.BG.clear()
        self.assertEqual(extras.stop_session_jobs("session-a"), 1)
        self._wait_dead(rec_a["pid"])
        self.assertTrue(self._alive(rec_b["pid"]))
        self.assertIn("running", extras.bg_output(bid_b))
        self.assertEqual(extras.stop_session_jobs("session-a"), 0)
        self.assertEqual(extras.stop_session_jobs(""), 0)
        self.assertEqual(extras.stop_session_jobs("session-b"), 1)
        self._wait_dead(rec_b["pid"])

    def test_server_process_exit_stops_persisted_jobs(self):
        cmd = command(sys.executable, "-c", "import time; time.sleep(120)")
        result = self._remote(
            "import sys; from backend import config, extras; "
            "print(extras.bg_start(sys.argv[1], cwd=sys.argv[2], session='server-session')); "
            "config.SERVER = object()",
            cmd, str(self.work))
        match = re.search(r"started \[([0-9a-f]+)\]", result)
        self.assertIsNotNone(match, result)
        bid = match.group(1)
        rec = self._remember(bid)
        # _remote waits for the helper's normal exit, including its atexit hook.
        self.assertEqual(rec["state"], "stopped")
        self._wait_dead(rec["pid"])

    def test_exited_job_keeps_output_and_is_not_reported_stopped(self):
        cmd = command(sys.executable, "-c", "print('finished-output', flush=True)")
        bid, rec = self._start(cmd, "finished-session")
        deadline = time.monotonic() + 15
        output = ""
        while time.monotonic() < deadline:
            output = extras.bg_output(bid)
            if "exited" in output:
                break
            time.sleep(0.05)
        self.assertIn("exited 0", output)
        self.assertIn("finished-output", output)
        extras.BG.clear()
        remote = self._remote(
            "import sys; from backend import extras; print(extras.bg_output(sys.argv[1]))", bid)
        self.assertIn("exited", remote)
        self.assertIn("finished-output", remote)
        self.assertEqual(extras.bg_stop(bid), f"already exited [{bid}]")
        self.assertEqual(extras.stop_session_jobs("finished-session"), 0)
        self.assertTrue(Path(rec["output"]).is_file())
        self.assertIn("ERROR: no background job", extras.bg_output("missing"))

    def test_ensure_home_creates_the_job_state_directory(self):
        self.assertEqual(config.JOBS_DIR, self.home / "jobs")
        self.assertTrue(config.JOBS_DIR.is_dir())
        self.assertIn("JOBS_DIR", state_paths(config))


if __name__ == "__main__":
    unittest.main()

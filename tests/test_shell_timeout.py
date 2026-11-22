import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from backend import config, proctools


class TestShellTimeoutFeedBack(unittest.TestCase):
    """A 90%-done computation used to look identical to a hung no-op: the partial output was
    discarded and the message didn't say whose cap it was. feal's attacks die this way - the
    model reruns the same >30s command five times and burns the cell."""

    def setUp(self):
        self.orig = config.SHELL_TIMEOUT
        config.SHELL_TIMEOUT = 2
        slow = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        slow.write("import sys, time\n"
                   "print('diffs-collected')\n"
                   "print('candidates=17\tpartial: 4294901760', flush=True)\n"
                   "time.sleep(30)\n")
        slow.close()
        self.cmd = f'"{sys.executable}" "{slow.name}"'
        self.tmp = slow.name

    def tearDown(self):
        config.SHELL_TIMEOUT = self.orig
        os.unlink(self.tmp)

    def test_a_timeout_carries_the_partial_output_and_the_cap_is_named(self):
        from backend.sandbox import run
        res = run(self.cmd, project=None, timeout=2)
        self.assertFalse(res["ok"])
        self.assertIn("timed out after 2s", res["stderr"])
        self.assertIn("shell's cap", res["stderr"],
                      "the model has to know the kill is ours, not the task's budget")
        self.assertIn("cannot pass", res["stderr"],
                      "and that rerunning it unchanged is not a plan")
        self.assertIn("candidates=17", res["stdout"],
                      "the progress printed before the kill is kept")
        self.assertIn("partial: 4294901760", res["stdout"])

    def test_agent_result_keeps_partial_stdout_and_carries_stop(self):
        from backend import agent

        stop = threading.Event()
        result = {"ok": False, "code": 124, "stdout": "useful partial output",
                  "stderr": "ERROR: timed out after 2s"}
        with mock.patch.object(agent.sandbox, "run", return_value=result) as run:
            text = agent.t_run_shell("slow-check", timeout=2, stop=stop)
            out = {}
            list(agent.call_tool("run_shell", {"command": "slow-check", "timeout": 2},
                                 {"project": None, "session": "", "stop": stop}, out, None))
        self.assertIn("useful partial output", text)
        self.assertIn("useful partial output", out["result"])
        self.assertEqual([call.kwargs["stop"] for call in run.call_args_list], [stop, stop])


class TestShellTreeSupervision(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.child_pid = 0
        self.addCleanup(self._stop_leftover)

    def _stop_leftover(self):
        if self.child_pid and proctools._pid_alive(self.child_pid):
            try:
                os.kill(self.child_pid, 15)
            except OSError:
                pass

    def _parent(self, marker: str) -> Path:
        child = (f"import pathlib,time; time.sleep(4); "
                 f"pathlib.Path({str(marker)!r}).write_text('survived')")
        path = self.root / "parent.py"
        path.write_text(
            "import subprocess,sys\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            "print(f'CHILD_PID={child.pid}', flush=True)\n",
            encoding="utf-8")
        return path

    def _pid(self, output: str) -> int:
        for line in output.splitlines():
            if line.startswith("CHILD_PID="):
                return int(line.split("=", 1)[1])
        self.fail(f"child pid missing from output: {output!r}")

    def test_root_exit_cannot_turn_a_pipe_holding_child_into_success(self):
        from backend import sandbox

        for streamed in (False, True):
            with self.subTest(streamed=streamed):
                self.child_pid = 0
                marker = self.root / f"survivor-{streamed}.txt"
                parent = self._parent(marker)
                cmd = f'"{sys.executable}" "{parent}"'
                started = time.monotonic()
                res = sandbox.run(cmd, backend="none", timeout=1,
                                  on_output=(lambda _k, _t: None) if streamed else None)
                elapsed = time.monotonic() - started
                self.assertFalse(res["ok"], res)
                self.assertEqual(res["code"], 124)
                self.assertIn("timed out after 1s", res["stderr"])
                self.assertLess(elapsed, 3, f"command returned after {elapsed:.2f}s")
                self.child_pid = self._pid(res["stdout"])
                self.assertFalse(proctools._pid_alive(self.child_pid),
                                 "the pipe-holding child survived its root and the timeout")

    def test_stop_kills_a_live_descendant_and_returns_cancelled(self):
        from backend import sandbox

        marker = self.root / "cancelled-survivor.txt"
        parent = self._parent(marker)
        # Keep the root alive too: Stop must cover the whole tree, not only the
        # early-root-exit timeout case above.
        parent.write_text(parent.read_text(encoding="utf-8") + "\nimport time\ntime.sleep(30)\n",
                          encoding="utf-8")
        stop = threading.Event()

        def cancel_on_pid(_kind, text):
            if "CHILD_PID=" in text:
                stop.set()

        started = time.monotonic()
        res = sandbox.run(f'"{sys.executable}" "{parent}"', backend="none", timeout=30,
                          on_output=cancel_on_pid, stop=stop)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(stop.is_set())
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["code"], 130)
        self.assertIn("cancelled", res["stderr"])
        self.child_pid = self._pid(res["stdout"])
        self.assertFalse(proctools._pid_alive(self.child_pid))

    def test_invalid_output_bytes_do_not_kill_the_stream_reader(self):
        from backend import sandbox

        path = self.root / "bytes.py"
        path.write_text("import sys\nsys.stdout.buffer.write(b'\\x81\\nvalid\\n')\n",
                        encoding="utf-8")
        res = sandbox.run(f'"{sys.executable}" "{path}"', backend="none", timeout=10,
                          on_output=lambda _k, _t: None)
        self.assertEqual(res["code"], 0, res["stderr"])
        self.assertIn("valid", res["stdout"])
        self.assertNotIn("reader:", res["stderr"])


if __name__ == "__main__":
    unittest.main()

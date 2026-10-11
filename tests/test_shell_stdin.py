"""Every command a turn spawns gets an immediate EOF on stdin, never an inherited handle.

The failure this pins is the one that hung *every* shell command in a turn. A turn
runs in the worker process, whose stdin is the server's control pipe: its write
end is held open for the whole turn so `{"cancel": true}` and steer lines can
arrive. A child spawned without a `stdin` kwarg inherits that read end, so a
command that reads stdin waits on a pipe whose writer is alive and silent --
forever. Nothing times out cleanly, the partial work is lost, and the same
command re-run hangs the same way. Inheriting also left a stray shell holding a
read handle on the control pipe, where a cancel meant for the worker could go to
it instead.

The behavioural test reproduces the chain rather than trusting the runner's own
stdin, which may already be harmless: a middle process whose stdin is a pipe we
never write to and never close calls the real `sandbox.run`, so what is measured
is what a turn actually does. If the fix regresses this blocks and fails; it does
not silently pass.
"""

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Runs the real sandbox.run on a command that reads stdin to EOF. Under the bug
# the grandchild blocks; under the fix it gets an immediate EOF and prints.
MIDDLE = (
    "import sys\n"
    "sys.path.insert(0, {root!r})\n"
    "from backend import sandbox\n"
    "import time\n"
    "t0 = time.time()\n"
    "res = sandbox.run(sys.executable + ' -c \"import sys; sys.stdin.read(); "
    "print(\\'got-eof\\')\"', project=None, timeout=20)\n"
    "print('RESULT', res.get('ok'), (res.get('stdout') or '').strip(), "
    "'%.1f' % (time.time() - t0), flush=True)\n"
)

# How long to wait for the middle before calling it a hang. Comfortably longer
# than a passing run (~2s: one interpreter plus one child) and far shorter than
# the 20s inner timeout, so a regression fails fast instead of hanging the suite.
WAIT_S = 30


class TestShellStdin(unittest.TestCase):
    def _run_middle(self):
        script = MIDDLE.format(root=str(ROOT))
        # stdin is a pipe we deliberately never write to and never close -- the
        # control pipe exactly. Closing it would hand the child an EOF for free
        # and the test would pass even with the bug present.
        mid = subprocess.Popen([sys.executable, "-c", script],
                               stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, cwd=str(ROOT))
        lines: list[str] = []
        done = threading.Event()

        def read():
            for line in mid.stdout:
                if "RESULT" in line:
                    lines.append(line.strip())
                    break
            done.set()

        t = threading.Thread(target=read, daemon=True)
        t.start()
        finished = done.wait(WAIT_S)
        try:
            mid.kill()
        except Exception:  # noqa: BLE001
            pass
        for stream in (mid.stdin, mid.stdout):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            mid.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                mid.kill()
                mid.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        return finished, lines

    def test_a_stdin_reading_command_gets_eof_instead_of_hanging(self):
        finished, lines = self._run_middle()
        body = " ".join(lines)
        # The content is the discriminator, not the return. Under the bug the
        # child blocks on the inherited pipe and sandbox.run's own timeout fires,
        # so the call does come back -- after burning the whole timeout, with
        # ok=False and no output. That is exactly what the agent saw: a shell
        # command dying at 180s having printed nothing.
        self.assertTrue(finished, "no RESULT line at all in %ds" % WAIT_S)
        self.assertIn("RESULT True got-eof", body,
                      "the command must read an immediate EOF and finish. "
                      "RESULT False means it blocked until the timeout killed it "
                      "-- that is the inherited-stdin hang. Pass stdin=DEVNULL.")
        self.assertLess(float(lines[0].split()[-1]), 10.0,
                        "and it must finish promptly, not after the inner timeout; "
                        "a passing run costs about a second")

    def test_every_spawn_site_hands_down_devnull(self):
        """The source, not just the one path the behavioural test exercises.

        Four sites spawn children on the agent's behalf and each was inheriting
        stdin; only the shell path is reachable cheaply above, so the rest are
        pinned here. A guard that covers one of four is a guard for three bugs.
        """
        import inspect

        from backend import extras, sandbox, vcs

        cap = inspect.getsource(sandbox._run_captured)
        self.assertIn('"stdin": subprocess.DEVNULL', cap,
                      "_run_captured is every agent shell command")
        spawn = inspect.getsource(sandbox._spawn)
        self.assertIn('"stdin": subprocess.DEVNULL', spawn,
                      "_spawn builds the kwargs for the isolated path")

        bg = inspect.getsource(extras.bg_start)
        self.assertIn("stdin=subprocess.DEVNULL", bg,
                      "a background job must not sit on the control pipe either")

        git = inspect.getsource(vcs.run_argv)
        self.assertIn("subprocess.DEVNULL", git,
                      "and neither may a git call from the panel")
        self.assertIn("input=stdin", git,
                      "input still owns stdin when the caller has something to send")

    def test_devnull_is_not_a_pipe(self):
        """A PIPE here would be its own deadlock: nobody writes it and nobody
        closes it, which is the failure this file exists for, one handle later."""
        import inspect

        from backend import sandbox
        for fn in (sandbox._run_captured, sandbox._spawn):
            self.assertNotIn('"stdin": subprocess.PIPE', inspect.getsource(fn))


if __name__ == "__main__":
    unittest.main()

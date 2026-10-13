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

# Fixed-argument system probes: Tacit names the binary, it never reads stdin, and
# its own timeout bounds it. They are listed rather than patched because the
# handle they hold is harmless today -- and because a list forces the next person
# to justify it instead of quietly adding a site to a private set.
ALLOWED_INHERIT = {
    "backend/proctools.py:taskkill",
    "backend/sandbox.py:taskkill",
    "backend/turn_worker.py:taskkill",
    "backend/sandbox.py:image",
    "backend/sandbox.py:kill",
    "backend/sandbox.py:pull",
    # Not a fixed probe: this is `Popen(argv, **kwargs)`, whose stdin arrives via
    # the forwarded dict, so the scan cannot see it. test_forwarded_kwargs_carry_
    # devnull below checks the builder instead -- forwarding is verified, never
    # assumed, or the scan's blind spot becomes the bug's hiding place.
    "backend/sandbox.py:?",
}


class TestForwardedKwargs(unittest.TestCase):
    def test_forwarded_kwargs_carry_devnull(self):
        """The scan is blind to `**kwargs`, so the builder is checked here."""
        import inspect

        from backend import proctools, sandbox

        for fn, why in ((sandbox._run_captured, "every agent shell command"),
                        (sandbox._spawn, "the isolated sandbox path")):
            src = inspect.getsource(fn)
            self.assertIn("DEVNULL", src, f"{fn.__name__} builds the kwargs for "
                                          f"{why} and must set stdin")

        # proctools.spawn is the funnel for turn workers, watchers, bg jobs and
        # the restart: it defaults stdin so no caller can inherit by accident,
        # while still letting one ask for a real pipe.
        src = inspect.getsource(proctools.spawn)
        self.assertIn('popen_kwargs.pop("stdin", subprocess.DEVNULL)', src,
                      "proctools.spawn must default stdin, not forward blindly")

_SPAWNERS = ("subprocess.run(", "subprocess.Popen(")


def _call_text(lines, start, limit=14):
    """The call from `start`, extended until its parentheses balance."""
    text, depth = "", 0
    for i in range(start, min(start + limit, len(lines))):
        text += lines[i] + "\n"
        depth += lines[i].count("(") - lines[i].count(")")
        if depth <= 0:
            break
    return text


def _inheriting_spawn_sites(root=None):
    """Every spawn call that names neither `stdin` nor `input`."""
    import re
    from pathlib import Path

    root = Path(root) if root else Path(__file__).resolve().parent.parent
    out = []
    for py in sorted((root / "backend").rglob("*.py")):
        lines = py.read_text(encoding="utf-8", errors="replace").splitlines()
        rel = py.relative_to(root).as_posix()
        for i, ln in enumerate(lines):
            s = ln.strip()
            if s.startswith("#") or s.startswith("def ") or s.startswith("test_"):
                continue
            if not any(sp in s for sp in _SPAWNERS):
                continue
            call = _call_text(lines, i)
            if "stdin" in call or "input=" in call:
                continue
            m = re.search(r"""['"]([^'"]+)['"]""", call)
            out.append(f"{rel}:{m.group(1) if m else '?'}")
    return out


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

    def test_no_spawn_site_inherits_stdin_unlisted(self):
        """Every child Tacit spawns states what it does with stdin.

        This scan replaced a hand-written list of "the four sites", and the list
        was wrong: it missed agent.run_terminal (the Terminal panel), gateways
        (arbitrary operator commands) and five more. A guard that enumerates is a
        guard against the bugs someone remembered. Anything that reads stdin and
        was not thought about fails here until it is either fixed or justified.

        An explicit `stdin=` is required -- DEVNULL for anything that must not
        block, PIPE only where the handle is a real channel (the turn worker's
        control pipe, MCP and the browser driver's JSON-RPC). `input=` counts as
        explicit. A fixed-argument system probe that never reads stdin may be
        listed below instead, with the reason.
        """
        offenders = _inheriting_spawn_sites()
        unexplained = sorted(set(offenders) - set(ALLOWED_INHERIT))
        self.assertEqual(unexplained, [],
                         "these spawn sites inherit stdin:\n  " + "\n  ".join(unexplained) +
                         "\nInheriting hands the child whatever stdin the parent "
                         "holds -- inside a turn that is the control pipe, so a "
                         "command that reads input blocks until the timeout kills "
                         "it. Pass stdin=subprocess.DEVNULL, or list the site with "
                         "a reason.")
        stale = sorted(set(ALLOWED_INHERIT) - set(offenders))
        self.assertEqual(stale, [],
                         "these are allowlisted but no longer inherit -- fix the "
                         "allowlist so it cannot hide a new offender: " + ", ".join(stale))

    def test_devnull_is_not_a_pipe(self):
        """A PIPE here would be its own deadlock: nobody writes it and nobody
        closes it, which is the failure this file exists for, one handle later."""
        import inspect

        from backend import sandbox
        for fn in (sandbox._run_captured, sandbox._spawn):
            self.assertNotIn('"stdin": subprocess.PIPE', inspect.getsource(fn))


if __name__ == "__main__":
    unittest.main()

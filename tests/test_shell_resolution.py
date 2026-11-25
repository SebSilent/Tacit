"""One shell everywhere: what resolves, and what every executor actually runs.

The measured failure this pins: a CachyOS install with fish as the login shell
watched `Program '/bin/fish' crashed` warnings because agent commands ran as
`fish -c`, which sources the user's whole config.fish on EVERY spawn (confirmed:
config is read even for non-interactive -c), so one plugin crash in the person's
interactive setup took down commands the agent ran. The shell a person logs in
with is their preference; the shell a harness executes through is a resolved
contract - bash, named in the standing prompt, shared by the sandbox, the
terminal panel, background jobs and shell=True paths alike.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

from backend import agent, config, extras


class TestShellResolution(unittest.TestCase):
    def test_the_login_shell_is_ignored(self):
        """SHELL=/usr/bin/fish in the environment is the person's preference;
        the execution shell is resolved from the system, from bash."""
        old = os.environ.get("SHELL")
        os.environ["SHELL"] = "/usr/bin/fish"
        try:
            resolved = config.resolve_shell()
        finally:
            if old is None:
                os.environ.pop("SHELL", None)
            else:
                os.environ["SHELL"] = old
        if os.name != "nt":
            self.assertNotEqual(resolved, "/usr/bin/fish",
                                "the login shell must not become the execution shell")
            self.assertIn(Path(resolved).name, ("bash", "sh"),
                          f"resolved to a POSIX shell, got {resolved!r}")
        else:
            self.assertIn("cmd", resolved.lower())

    def test_tacit_shell_overrides(self):
        """A person who wants another shell can say so explicitly, and accepts
        the syntax that goes with it."""
        old = os.environ.get("TACIT_SHELL")
        os.environ["TACIT_SHELL"] = "/opt/shells/zsh"
        try:
            self.assertEqual(config.resolve_shell(), "/opt/shells/zsh")
        finally:
            if old is None:
                os.environ.pop("TACIT_SHELL", None)
            else:
                os.environ["TACIT_SHELL"] = old

    def test_shell_kind_names_the_resolved_binary(self):
        self.assertEqual(config.SHELL_KIND, Path(config.SHELL).name or config.OS)


class TestOneShellEverywhere(unittest.TestCase):
    """The sandbox, the terminal panel and background jobs all go through the
    resolved shell - observed through a stub that records its argv."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name) / "work"
        self.work.mkdir()
        if os.name == "nt":
            self.skipTest("the argv-recording stub is a POSIX shell script")
        self.stub = Path(self._tmp.name) / "stub-shell"
        self.stub.write_text('#!/bin/sh\nprintf \'%s\\0\' "$@" > argv.txt\n',
                             encoding="utf-8")
        self.stub.chmod(0o755)
        self._saved = config.SHELL
        self.addCleanup(setattr, config, "SHELL", self._saved)
        config.SHELL = str(self.stub)

    def _argv(self) -> list[str]:
        record = self.work / "argv.txt"
        for _ in range(200):
            if record.exists():
                raw = record.read_bytes().rstrip(b"\0")
                return [part.decode("utf-8") for part in raw.split(b"\0")]
            time.sleep(0.05)
        self.fail("the stub shell was never invoked")

    def test_sandbox_runs_the_resolved_shell(self):
        from backend import sandbox
        res = sandbox.run("record me", project=str(self.work), backend="none",
                          timeout=20)
        self.assertEqual(res["ok"], True, res)
        self.assertEqual(self._argv(), ["-c", "record me"],
                         "the sandbox must exec config.SHELL, and with -c")

    def test_the_terminal_panel_runs_the_resolved_shell(self):
        res = agent.run_terminal("record me", project=str(self.work))
        self.assertEqual(res["ok"], True, res)
        self.assertEqual(self._argv(), ["-c", "record me"])

    def test_background_jobs_run_the_resolved_shell(self):
        from tests.helpers import state_paths
        keys = state_paths(config)
        orig = {k: getattr(config, k) for k in keys}
        home = config.HOME
        tmp_home = Path(self._tmp.name) / "home"
        tmp_home.mkdir()
        config.HOME = tmp_home
        for k in keys:
            target = tmp_home / Path(orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        self.addCleanup(self._restore_paths, keys, orig, home)

        old_bg = dict(extras.BG)
        extras.BG.clear()
        self.addCleanup(lambda: (extras.BG.clear(), extras.BG.update(old_bg)))
        out = extras.bg_start("record me", cwd=str(self.work), session="shell-res")
        self.assertIn("started [", out, out)
        bid = out.split("started [", 1)[1].split("]", 1)[0]
        self.assertEqual(self._argv(), ["-c", "record me"])
        extras.bg_stop(bid)

    def _restore_paths(self, keys, orig, home):
        config.HOME = home
        for k, v in orig.items():
            setattr(config, k, v)


if __name__ == "__main__":
    unittest.main()
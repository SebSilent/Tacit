import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from backend import config, providers, sandbox
from tests.helpers import state_paths


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bwrap"),
                     "bubblewrap is the point")
class PidContainment(unittest.TestCase):
    """A sandboxed command must not be able to touch the person's session.

    The crash this pins: bwrap wrapped the filesystem but not the pid
    namespace, so an agent command could still `kill` the launching shell or
    the server by pid - a terminal warning with no coredump and nothing in
    the harness's signal log. With --unshare-pid the host pids are not even
    nameable inside the sandbox.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        self._root = config.ROOT
        self.addCleanup(self._restore)
        config.HOME = home
        for k in self._keys:
            target = home / Path(self._orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        providers.save({"sandbox": {"backend": "tacit-micro"}})

    def _restore(self):
        config.HOME = self._home
        config.ROOT = self._root
        for k, v in self._orig.items():
            setattr(config, k, v)

    def test_a_command_cannot_kill_a_process_outside_the_sandbox(self):
        victim = subprocess.Popen(["/bin/sleep", "30"],
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        self.addCleanup(lambda: (victim.poll() is None and victim.kill(),
                                 victim.wait(timeout=5)))
        res = sandbox.run(f"kill -9 {victim.pid}", timeout=20)
        self.assertTrue(res["ok"] or res["code"] != 0,
                        f"the sandboxed kill must not succeed; {res['code']}")
        time.sleep(0.3)
        self.assertIsNone(victim.poll(),
                          "the person's processes are not the sandbox's to signal")

    def test_ordinary_commands_still_work_inside_the_namespace(self):
        res = sandbox.run("echo hi && ps -o pid= | wc -l", timeout=20)
        self.assertEqual(res["code"], 0, res.get("stderr"))
        self.assertIn("hi", res["stdout"])
        seen = int(res["stdout"].split()[-1])
        self.assertLessEqual(seen, 12,
                             f"the sandbox must see only its own pids, saw {seen}")

    def test_the_debug_log_names_the_command_behind_every_spawn(self):
        os.environ["TACIT_DEBUG"] = "on"
        from backend import debuglog
        debuglog.reset()
        self.addCleanup(lambda: (os.environ.pop("TACIT_DEBUG", None),
                                 debuglog.reset()))
        res = sandbox.run("echo canary-line-42", timeout=20)
        self.assertEqual(res["code"], 0, res.get("stderr"))
        path = config.HOME / "debug.log"
        self.assertTrue(path.exists())
        hits = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
                if "canary-line-42" in l]
        self.assertTrue(hits, "a spawned shell must be attributable in the log")
        self.assertEqual(hits[0]["event"], "proc-spawn")


if __name__ == "__main__":
    unittest.main()

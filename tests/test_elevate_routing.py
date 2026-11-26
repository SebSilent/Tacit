"""Elevation routes around the sandbox, never through it (1.0.27).

bwrap strips setuid, so a sudo inside the wrapper can only ever fail. The
person's sudo switch is the standing decision that root work runs on the
host; a command that actually carries a feedable sudo must reach the host
path, while every ordinary command stays behind the wall.
"""

import os
import tempfile
import unittest
from pathlib import Path

from backend import config, providers, sandbox, sudo
from tests.helpers import state_paths


class ElevateRouting(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
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
        for k, v in self._orig.items():
            setattr(config, k, v)

    def _route(self, cmd, armed, fed):
        hits = []
        o_arm = sandbox.sudo.armed
        o_prep = sandbox.sudo.prepare
        o_none = sandbox._run_none
        o_micro = sandbox._run_micro
        sandbox.sudo.armed = lambda: armed
        sandbox.sudo.prepare = lambda c: {"command": c, "fed": fed, "sites": fed,
                                          "elevated": bool(fed), "stdin": None}
        sandbox._run_none = (lambda *a, **k:
                             hits.append("none") or {"ok": True, "code": 0,
                                                     "notes": []})
        sandbox._run_micro = (lambda *a, **k:
                              hits.append("micro") or {"ok": True, "code": 0,
                                                        "notes": []})
        try:
            res = sandbox.run(cmd, backend="tacit-micro", session="er-1")
        finally:
            sandbox.sudo.armed = o_arm
            sandbox.sudo.prepare = o_prep
            sandbox._run_none = o_none
            sandbox._run_micro = o_micro
        return hits, res

    def test_a_feedable_sudo_command_takes_the_host_path(self):
        hits, res = self._route("sudo apt install -y firmware-misc-nonfree",
                                armed=True, fed=1)
        self.assertEqual(hits, ["none"],
                         "the sudo command must not be sent into the wrapper")
        self.assertTrue(any("outside the sandbox" in n for n in res["notes"]),
                        "the escape must be stated, never silent")

    def test_a_plain_command_stays_behind_the_wall(self):
        hits, res = self._route("make && make install", armed=True, fed=0)
        self.assertEqual(hits, ["micro"],
                         "no sudo, no exception: the sandbox holds")

    def test_the_switch_off_keeps_sudo_inside_the_wall(self):
        hits, res = self._route("sudo reboot", armed=False, fed=1)
        self.assertEqual(hits, ["micro"],
                         "without the sudo switch armed, nothing escapes; the "
                         "command runs contained and sudo fails on its own")


if __name__ == "__main__":
    unittest.main()

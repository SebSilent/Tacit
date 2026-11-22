"""Opt-in live-daemon container conformance. NOT part of the offline suite.

Run with TACIT_LIVE_DOCKER=1 and a running Docker/Podman daemon. The image must
already be present (Tacit does not pull for you, and neither does a test); set
TACIT_LIVE_DOCKER_IMAGE to override the default. Every test verifies the
daemon-side outcome, not just the client's exit code, and leaves no tacit-*
container behind.
"""

import os
import shutil
import subprocess
import threading
import time
import unittest

from backend import sandbox


def _runtime() -> str:
    return shutil.which("docker") or shutil.which("podman") or ""


def _daemon_up(runtime: str) -> bool:
    try:
        return subprocess.run([runtime, "info"], capture_output=True,
                              timeout=20).returncode == 0
    except Exception:
        return False


def _image_present(runtime: str, image: str) -> bool:
    try:
        return subprocess.run([runtime, "image", "inspect", image],
                              capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def _leftover_tacit_containers(runtime: str) -> list[str]:
    try:
        out = subprocess.run([runtime, "ps", "-a", "--filter", "name=tacit-",
                              "--format", "{{.Names}}"], capture_output=True,
                             text=True, timeout=20)
        return [n for n in out.stdout.splitlines() if n.strip()]
    except Exception:
        return []


@unittest.skipUnless(os.environ.get("TACIT_LIVE_DOCKER") == "1",
                     "live-daemon tests are opt-in: set TACIT_LIVE_DOCKER=1")
class TestLiveContainer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = _runtime()
        if not cls.runtime or not _daemon_up(cls.runtime):
            raise unittest.SkipTest("no container runtime daemon is running")
        cls.image = os.environ.get("TACIT_LIVE_DOCKER_IMAGE") or sandbox.DEFAULT_IMAGE
        if not _image_present(cls.runtime, cls.image):
            raise unittest.SkipTest(
                f"image '{cls.image}' is not present; pull it first "
                f"({cls.runtime} pull {cls.image})")

    def setUp(self):
        self.before = set(_leftover_tacit_containers(self.runtime))

    def tearDown(self):
        left = set(_leftover_tacit_containers(self.runtime)) - self.before
        for name in left:
            subprocess.run([self.runtime, "rm", "-f", name], capture_output=True,
                           timeout=30)
        self.assertFalse(left, f"the test left containers behind: {left}")

    def test_a_command_runs_and_reports(self):
        res = sandbox.run("echo live-ok", backend="container", timeout=60,
                          session="live-test")
        self.assertTrue(res["ok"], res)
        self.assertIn("live-ok", res["stdout"])
        self.assertEqual(res["code"], 0)

    def test_a_timeout_kills_the_container_on_the_daemon(self):
        res = sandbox.run("sleep 300", backend="container", timeout=3,
                          session="live-test")
        self.assertEqual(res["code"], 124, res)
        self.assertTrue(any("container was killed" in n for n in res["notes"]),
                        res["notes"])
        self.assertFalse(_leftover_tacit_containers(self.runtime))

    def test_cancel_kills_the_container_on_the_daemon(self):
        stop = threading.Event()
        threading.Timer(2.0, stop.set).start()
        started = time.monotonic()
        res = sandbox.run("sleep 300", backend="container", timeout=120,
                          session="live-test", stop=stop)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(res["code"], 130, res)
        self.assertFalse(_leftover_tacit_containers(self.runtime))


if __name__ == "__main__":
    unittest.main()
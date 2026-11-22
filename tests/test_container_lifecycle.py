"""Container lifecycle deadlines, cancellation, and death cleanup, offline.

No daemon is needed: a fake runtime CLI on PATH speaks the same verbs the
adapter uses (image inspect, pull, run, kill, inspect) against a JSON state
file, so the tests can hang a pull, keep a container running after its client
dies, and refuse a kill, and watch what the adapter reports. The live-daemon
counterpart is opt-in in test_container_live.py.

What is pinned here:
- a timeout or cancel kills the daemon-side container, not just the client,
  and the result says whether the runtime confirmed it;
- a cancel during image inspect or pull returns promptly instead of waiting
  out the pull;
- the container name is registered before the run, and an unconfirmed cleanup
  leaves the row for the parent / next boot to retry;
- a dead owner's containers are settled by name through the ledger.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend import config, proctools, providers, sandbox
from tests.helpers import state_paths

ROOT = Path(__file__).resolve().parents[1]

FAKE_DOCKER = r'''
import json, os, sys, time
state_path = os.environ["TACIT_FAKE_DOCKER_STATE"]
def load():
    try:
        with open(state_path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}
def save(s):
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s, fh)
    os.replace(tmp, state_path)
args = sys.argv[1:]
s = load()
if args[:2] == ["image", "inspect"]:
    sys.exit(0 if s.get("image_present") else 1)
if args[:1] == ["pull"]:
    if s.get("pull_hang"):
        deadline = time.time() + 60
        while time.time() < deadline:
            if load().get("pull_release"):
                break
            time.sleep(0.1)
    s = load()
    s["image_present"] = True
    save(s)
    sys.exit(0)
if args[:1] == ["run"]:
    name = args[args.index("--name") + 1]
    s = load()
    s.setdefault("containers", {})[name] = {"running": True}
    save(s)
    run_seconds = s.get("run_seconds")
    if run_seconds:
        time.sleep(float(run_seconds))
        s = load()
        s["containers"][name]["running"] = False
        save(s)
        sys.exit(0)
    deadline = time.time() + 120
    while time.time() < deadline:
        if not load().get("containers", {}).get(name, {}).get("running"):
            break
        time.sleep(0.1)
    sys.exit(0)
if args[:1] == ["kill"]:
    name = args[1]
    s = load()
    c = s.get("containers", {}).get(name)
    if c is None:
        sys.exit(1)
    if not s.get("kill_noop"):
        c["running"] = False
        save(s)
    sys.exit(0)
if args[:1] == ["inspect"]:
    name = args[-1]
    c = load().get("containers", {}).get(name)
    if c is None:
        sys.exit(1)
    print("true" if c.get("running") else "false")
    sys.exit(0)
sys.exit(0)
'''


class FakeRuntimeFixture(unittest.TestCase):
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
        self.state_path = self.home / "fake-docker-state.json"
        self._write_state({"image_present": True})
        self.bin = self.home / "bin"
        self.bin.mkdir()
        fake_py = self.bin / "fake_docker.py"
        fake_py.write_text(FAKE_DOCKER, encoding="utf-8")
        if os.name == "nt":
            wrapper = self.bin / "docker.bat"
            wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{fake_py}" %*\r\n',
                               encoding="utf-8")
        else:
            wrapper = self.bin / "docker"
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake_py}" "$@"\n',
                               encoding="utf-8")
            wrapper.chmod(0o755)
        self._path = os.environ.get("PATH")
        os.environ["PATH"] = str(self.bin) + os.pathsep + (self._path or "")
        self.addCleanup(self._restore_path)
        os.environ["TACIT_FAKE_DOCKER_STATE"] = str(self.state_path)
        self.addCleanup(lambda: os.environ.pop("TACIT_FAKE_DOCKER_STATE", None))
        self._providers_save = providers.save
        self.addCleanup(setattr, providers, "save", self._providers_save)
        providers.save({"sandbox": {"backend": "container"}})

    def _restore_config(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)

    def _restore_path(self):
        if self._path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self._path

    def _write_state(self, s: dict) -> None:
        self.state_path.write_text(json.dumps(s), encoding="utf-8")

    def _state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _run(self, command: str = "sleep 999", timeout: int = 2, stop=None) -> dict:
        return sandbox.run(command, project=str(self.work), backend="container",
                           timeout=timeout, session="cont-test", stop=stop)


class TestContainerLifecycle(FakeRuntimeFixture):
    def test_a_timeout_kills_the_daemon_side_container(self):
        res = self._run(timeout=2)
        self.assertEqual(res["code"], 124, res)
        self.assertIn("timed out", res["stderr"])
        name = res["container"]
        self.assertFalse(self._state()["containers"][name]["running"],
                         "the container outlived the client")
        self.assertTrue(any("container was killed" in n for n in res["notes"]))
        self.assertEqual(proctools._load_file(proctools._containers_path()), [],
                         "a confirmed cleanup leaves no ledger row")

    def test_cancel_during_the_run_kills_the_container(self):
        stop = threading.Event()
        threading.Timer(1.0, stop.set).start()
        started = time.monotonic()
        res = self._run(timeout=60, stop=stop)
        self.assertLess(time.monotonic() - started, 12)
        self.assertEqual(res["code"], 130, res)
        self.assertIn("cancelled", res["stderr"])
        self.assertFalse(self._state()["containers"][res["container"]]["running"])

    def test_cancel_during_a_pull_returns_promptly(self):
        self._write_state({"image_present": False, "pull_hang": True})
        providers.save({"sandbox": {"backend": "container", "container_auto_pull": True}})
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        started = time.monotonic()
        res = self._run(timeout=60, stop=stop)
        elapsed = time.monotonic() - started
        self._write_state({"pull_release": True})
        self.assertLess(elapsed, 10, f"the cancel waited out the pull ({elapsed:.1f}s)")
        self.assertEqual(res["code"], 130, res)
        self.assertIn("cancelled", res["stderr"])

    def test_a_normal_exit_verifies_the_container_is_gone(self):
        self._write_state({"image_present": True, "run_seconds": 0.5})
        res = self._run(command="echo done", timeout=30)
        self.assertEqual(res["code"], 0, res)
        self.assertFalse(self._state()["containers"][res["container"]]["running"])
        self.assertFalse(any("did not confirm" in n for n in res["notes"]))
        self.assertEqual(proctools._load_file(proctools._containers_path()), [])

    def test_an_unconfirmed_cleanup_keeps_the_ledger_row(self):
        self._write_state({"image_present": True, "kill_noop": True})
        res = self._run(timeout=2)
        self.assertEqual(res["code"], 124)
        self.assertTrue(any("did not confirm" in n for n in res["notes"]),
                        "a kill the runtime did not carry out must not read as confirmed")
        rows = proctools._load_file(proctools._containers_path())
        self.assertTrue(any(r.get("name") == res["container"] for r in rows),
                        "an unconfirmed cleanup must leave the row for a retry")
        # The retry path (worker death / next boot) still cannot confirm it,
        # and must keep the row rather than forget the container.
        proctools.cleanup_owner_containers(os.getpid())
        rows = proctools._load_file(proctools._containers_path())
        self.assertTrue(any(r.get("name") == res["container"] for r in rows))

    def test_a_dead_owners_containers_are_settled_by_name(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        dead.wait(timeout=30)
        name = "tacit-12345678-999"
        self._write_state({"containers": {name: {"running": True}}})
        proctools.register_container(name, dead.pid, session="dead-worker")
        stopped = proctools.cleanup_owner_containers(dead.pid)
        self.assertTrue(any(r.get("name") == name for r in stopped))
        self.assertFalse(self._state()["containers"][name]["running"])
        self.assertEqual(proctools._load_file(proctools._containers_path()), [])

    def test_the_sweep_settles_dead_owner_containers_and_spares_live_ones(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        dead.wait(timeout=30)
        orphan = "tacit-11111111-111"
        mine = "tacit-22222222-222"
        self._write_state({"containers": {orphan: {"running": True},
                                          mine: {"running": True}}})
        proctools.register_container(orphan, dead.pid)
        proctools.register_container(mine, os.getpid())
        report = proctools.sweep_containers()
        self.assertTrue(any(r.get("name") == orphan for r in report["killed"]))
        self.assertFalse(self._state()["containers"][orphan]["running"])
        self.assertTrue(self._state()["containers"][mine]["running"],
                        "a live owner's container is not the sweep's to kill")
        rows = proctools._load_file(proctools._containers_path())
        self.assertTrue(any(r.get("name") == mine for r in rows))


if __name__ == "__main__":
    unittest.main()
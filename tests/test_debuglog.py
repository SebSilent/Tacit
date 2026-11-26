import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend import config, debuglog
from tests.helpers import state_paths


class DebugLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        self._old_debug = os.environ.get("TACIT_DEBUG")
        self.addCleanup(self._restore)
        config.HOME = home
        for k in self._keys:
            target = home / Path(self._orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        debuglog.reset()

    def _restore(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)
        debuglog.reset()
        if self._old_debug is None:
            os.environ.pop("TACIT_DEBUG", None)
        else:
            os.environ["TACIT_DEBUG"] = self._old_debug

    def _on(self):
        os.environ["TACIT_DEBUG"] = "on"
        debuglog.reset()

    def _read(self):
        path = config.HOME / "debug.log"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # ── ────────────────────────────────────────────────────────────────────
    def test_off_by_default_writes_nothing(self):
        os.environ.pop("TACIT_DEBUG", None)
        debuglog.reset()
        debuglog.log("signal", site="unit", pid=123, how="probe", ok=True)
        self.assertFalse((config.HOME / "debug.log").exists())

    def test_events_land_in_the_log_with_site_and_target(self):
        self._on()
        debuglog.signal_sent("unit.test", 4321, "killpg SIGKILL", True)
        debuglog.signal_refused("unit.test", 4322, "identity not proven")
        events = self._read()
        sig = [e for e in events if e["event"] == "signal"]
        ref = [e for e in events if e["event"] == "signal-refused"]
        self.assertEqual(sig[-1]["site"], "unit.test")
        self.assertEqual(sig[-1]["pid"], 4321)
        self.assertEqual(ref[-1]["reason"], "identity not proven")
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(seqs))

    def test_a_real_kill_by_the_bg_path_is_logged(self):
        self._on()
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(30)"],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(),
                                 proc.wait(timeout=5)))
        rec = {"pid": proc.pid, "pgid": None, "identity": None,
               "id": "dbg-1", "state": "running", "started": time.time()}
        ok, detail = self._signal(rec)
        self.assertTrue(ok, detail)
        proc.wait(timeout=10)
        events = [e for e in self._read() if e["event"] == "signal"]
        self.assertTrue(any(e.get("site") == "extras._signal_tree"
                            and e.get("pid") == proc.pid for e in events),
                        f"the kill must name its site; got {events}")

    def _signal(self, rec):
        from backend import extras
        return extras._signal_tree(rec)

    def test_a_refused_stop_is_logged_as_refused_not_silent(self):
        self._on()
        from backend import extras
        victim = subprocess.Popen([sys.executable, "-c",
                                   "import time; time.sleep(30)"],
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  **({} if os.name == "nt"
                                     else {"start_new_session": True}))
        self.addCleanup(lambda: (victim.poll() is None and victim.kill(),
                                 victim.wait(timeout=5)))
        rec = {"id": "dbg-forge", "pid": victim.pid,
               "pgid": None if os.name == "nt" else victim.pid,
               "identity": "wrong-on-purpose", "state": "running",
               "started": time.time()}
        st, msg = extras._stop_record("dbg-forge", rec)
        self.assertEqual(st, "error")
        self.assertIsNone(victim.poll(), "the stranger must survive")
        refused = [e for e in self._read() if e["event"] == "signal-refused"]
        self.assertTrue(any("identity" in str(e.get("reason"))
                            for e in refused), refused)

    def test_the_watchdog_names_the_moment_a_watched_pid_dies(self):
        self._on()
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(0.4)"],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        t = threading.Thread(target=debuglog._watch, args=(proc.pid,),
                             daemon=True)
        t.start()
        deadline = time.time() + 10
        gone = []
        while time.time() < deadline:
            gone = [e for e in self._read()
                    if e["event"] == "launch-shell-gone"]
            if gone:
                break
            time.sleep(0.2)
        proc.wait(timeout=10)
        t.join(timeout=5)
        self.assertTrue(gone, "the death of a watched pid must be logged")
        self.assertIn("ppid", gone[0])

    def test_proctools_lifecycle_lines_mirror_into_the_log(self):
        self._on()
        from backend import proctools
        proc = proctools.spawn("tacit-shell", [sys.executable, "-c", "pass"],
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        proc.wait(timeout=15)
        proctools.reap(proc)
        events = [e for e in self._read() if e["event"] == "proc"]
        self.assertTrue(any("reaped" in e.get("line", "") or
                            "spawned" in e.get("line", "") for e in events),
                        f"spawn/reap must mirror into debug.log; got {events}")


if __name__ == "__main__":
    unittest.main()

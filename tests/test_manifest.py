"""Run manifests: the evidence artifact's contract (P0B.1).

Schema round-trip, secret masking at every layer, the opt-in interactive path
through a real process worker, cancellation, nested usage charged to the
parent, file-mutation hashes, and the headless rule: a benchmark run always
records, whatever the interactive setting says.
"""

import json
import os
import time
import unittest
from pathlib import Path

from backend import config, headless, manifest, metrics, session_runtime
from tests.fake_provider import FakeProvider
from tests.helpers import state_paths
from tests.test_worker_cancel_integration import RuntimeFixture

SECRET = "sk-supersecretvalue123456"


def _registry_with_secret(fp: FakeProvider):
    fp.register()
    reg = config.registry()
    reg["providers"]["p"]["apiKey"] = SECRET
    config.save_registry(reg)


class TestManifestSchema(unittest.TestCase):
    """Synthetic events: the collector's arithmetic and masking, no model."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        self.addCleanup(self._restore)
        config.HOME = self.home
        for k in self._keys:
            target = self.home / Path(self._orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        config.save_registry({"default": "p/m", "providers": {"p": {
            "baseUrl": "http://127.0.0.1:9/v1", "apiKey": SECRET,
            "models": [{"id": "m", "contextWindow": 100000}]}}})

    def _restore(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)

    def _manifest(self, **kw):
        return manifest.RunManifest(session="s", model="p/m", prompt_text="standing prompt",
                                    **kw)

    def test_schema_round_trip_and_usage_arithmetic(self):
        man = self._manifest(kind="benchmark", profile="default", reasoning="high")
        man.event({"type": "usage", "usage": {"input": 100, "output": 20, "cache_read": 60}})
        man.event({"type": "usage", "usage": {"input": 50, "output": 10}, "subagent": True})
        man.event({"type": "tool_start", "name": "run_shell", "id": "c1",
                   "args": {"command": "echo hi"}})
        man.event({"type": "tool_end", "name": "run_shell", "id": "c1",
                   "result": "hi", "is_error": False, "elapsed_ms": 12,
                   "backend": "none"})
        man.event({"type": "compaction", "saved": 500, "chars_before": 9000,
                   "chars_after": 2000})
        man.event({"type": "delegation", "spent": 800, "saved": 1200})
        man.event({"type": "done"})
        summary = man.finish(wall_s=1.5)
        self.assertEqual(summary["status"], "completed")
        loaded = manifest.load(man.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["schema_version"], manifest.SCHEMA_VERSION)
        self.assertEqual(loaded["kind"], "benchmark")
        self.assertEqual(loaded["model"]["ref"], "p/m")
        self.assertEqual(loaded["model"]["endpoint_class"], "local")
        self.assertEqual(loaded["prompt"]["chars"], len("standing prompt"))
        self.assertNotIn("standing prompt", json.dumps(loaded))
        want = metrics.normalize_usage({"input": 100, "output": 20, "cache_read": 60})
        sub = metrics.normalize_usage({"input": 50, "output": 10})
        for key in want:
            self.assertEqual(loaded["usage"][key], want[key] + sub[key],
                             f"nested usage must charge to the parent: {key}")
        self.assertEqual(len(loaded["model_calls"]), 2)
        self.assertTrue(loaded["model_calls"][1]["subagent"])
        self.assertEqual(loaded["tool_calls"][0]["elapsed_ms"], 12)
        self.assertEqual(loaded["tool_calls"][0]["backend"], "none")
        self.assertEqual(loaded["compactions"][0]["saved"], 500)
        self.assertEqual(loaded["delegations"][0]["spent"], 800)
        self.assertEqual(loaded["terminal"]["status"], "completed")
        self.assertEqual(loaded["wall_s"], 1.5)
        self.assertTrue(loaded["source_fingerprint"])
        self.assertTrue(loaded["config_hash"])

    def test_secrets_never_land_in_the_file(self):
        man = self._manifest()
        man.event({"type": "tool_start", "name": "fetch", "id": "c1",
                   "args": {"url": "http://x/?token=" + SECRET, "api_key": SECRET,
                            "command": "echo " + SECRET}})
        man.event({"type": "tool_end", "name": "fetch", "id": "c1",
                   "result": "the key is " + SECRET, "is_error": False})
        man.event({"type": "error", "message": "auth failed for " + SECRET})
        man.note_steering("use " + SECRET)
        man.finish(error="auth failed for " + SECRET)
        raw = (config.MANIFESTS_DIR / f"{man.id}.json").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, raw, "a known key value must be redacted everywhere")
        loaded = manifest.load(man.id)
        self.assertEqual(loaded["tool_calls"][0]["args"]["api_key"], "***")

    def test_error_and_cancel_terminal_states(self):
        man = self._manifest()
        man.event({"type": "error", "message": "boom"})
        self.assertEqual(man.finish()["status"], "error")
        man2 = self._manifest()
        man2.event({"type": "done"})
        man2.note_cancel()
        self.assertEqual(man2.finish()["status"], "cancelled")

    def test_config_hash_is_stable_and_sensitive(self):
        a = manifest.config_hash()
        b = manifest.config_hash()
        self.assertEqual(a, b)
        config.save_prefs({"disabledTools": ["browser"]})
        self.assertNotEqual(manifest.config_hash(), a)


class TestManifestInteractive(RuntimeFixture):
    """The opt-in server path, through the default process worker."""

    def _wait_manifest(self, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            files = list(config.MANIFESTS_DIR.glob("*.json"))
            if files:
                return json.loads(files[0].read_text(encoding="utf-8"))
            time.sleep(0.05)
        self.fail("no manifest was written")

    def test_off_by_default_records_nothing(self):
        os.environ.pop("TACIT_MANIFEST", None)
        self.assertEqual(config.manifest_mode(), "off")
        fp = FakeProvider([{"text": "quiet"}])
        self.addCleanup(fp.stop)
        fp.register()
        rec = self._session("man-off")
        self._start("man-off", rec)
        deadline = time.monotonic() + 30
        while session_runtime.turn_running("man-off") and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        self.assertEqual(list(config.MANIFESTS_DIR.glob("*.json")), [],
                         "an interactive turn without TACIT_MANIFEST records nothing")

    def test_on_records_a_process_worker_turn(self):
        os.environ["TACIT_MANIFEST"] = "on"
        fp = FakeProvider([{"text": "recorded answer"}])
        self.addCleanup(fp.stop)
        fp.register()
        rec = self._session("man-on")
        self._start("man-on", rec)
        loaded = self._wait_manifest()
        self.assertEqual(loaded["kind"], "interactive")
        self.assertEqual(loaded["terminal"]["status"], "completed")
        self.assertEqual(loaded["model"]["ref"], "p/m")
        self.assertGreater(loaded["usage"]["input"], 0)
        self.assertTrue(loaded["prompt"]["hash"])
        self.assertNotIn("recorded answer", json.dumps(loaded["prompt"]),
                         "the prompt is hashed, never stored")

    def test_cancel_is_recorded_as_cancelled(self):
        os.environ["TACIT_MANIFEST"] = "on"
        pidfile = self.work / "pids.txt"
        model_script = [{"tool": {"name": "run_shell",
                                  "arguments": {"command": self._tree_script(pidfile),
                                                "timeout": 60}}},
                        {"text": "after"}]
        fp = FakeProvider(model_script)
        self.addCleanup(fp.stop)
        fp.register()
        rec = self._session("man-cancel")
        self._start("man-cancel", rec)
        self._wait_pids(pidfile)
        session_runtime.cancel("man-cancel")
        loaded = self._wait_manifest()
        self.assertEqual(loaded["terminal"]["status"], "cancelled")
        self.assertTrue(loaded["terminal"]["cancelled"])

    def test_steering_is_recorded(self):
        os.environ["TACIT_MANIFEST"] = "on"
        fp = FakeProvider([{"delay_s": 2, "text": "steered answer"}])
        self.addCleanup(fp.stop)
        fp.register()
        rec = self._session("man-steer")
        self._start("man-steer", rec)
        self.assertTrue(fp.request_seen.wait(30))
        self.assertTrue(session_runtime.steer("man-steer", "change direction"))
        loaded = self._wait_manifest()
        self.assertTrue(any("change direction" in s["text"] for s in loaded["steering"]))


class TestManifestHeadless(unittest.TestCase):
    """A benchmark run always records, and records the work's evidence."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.work = self.home / "work"
        self.work.mkdir()
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        self.addCleanup(self._restore)
        config.HOME = self.home
        for k in self._keys:
            target = self.home / Path(self._orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        self._manifest_env = os.environ.get("TACIT_MANIFEST")
        self.addCleanup(self._restore_env)
        os.environ.pop("TACIT_MANIFEST", None)

    def _restore(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)

    def _restore_env(self):
        if self._manifest_env is None:
            os.environ.pop("TACIT_MANIFEST", None)
        else:
            os.environ["TACIT_MANIFEST"] = self._manifest_env

    def test_headless_records_even_when_interactive_is_off(self):
        fp = FakeProvider([{"text": "benchmark answer"}])
        self.addCleanup(fp.stop)
        _registry_with_secret(fp)
        result = headless.run("say something", str(self.work), "p/m", timeout_s=60)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["manifest"], "a benchmark run must produce a manifest")
        loaded = manifest.load(result["manifest_id"])
        self.assertEqual(loaded["kind"], "benchmark")
        self.assertEqual(loaded["terminal"]["status"], "completed")
        self.assertNotIn(SECRET, Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(os.environ.get("TACIT_MANIFEST"), None,
                         "the forced setting must not leak past the run")

    def test_file_mutations_carry_before_and_after_hashes(self):
        fp = FakeProvider([
            {"tools": [
                {"name": "write_file", "id": "w1",
                 "arguments": {"path": "made.txt", "content": "one"}},
                {"name": "edit_file", "id": "e1",
                 "arguments": {"path": "made.txt", "find": "one", "replace": "two"}},
            ]},
            {"text": "done"},
        ])
        self.addCleanup(fp.stop)
        fp.register()
        result = headless.run("write and edit", str(self.work), "p/m", timeout_s=60)
        self.assertTrue(result["ok"], result.get("error"))
        loaded = manifest.load(result["manifest_id"])
        muts = loaded["file_mutations"]
        self.assertEqual(len(muts), 2, muts)
        self.assertEqual(muts[0]["path"], "made.txt")
        self.assertIsNone(muts[0]["before"], "a new file has no before")
        self.assertTrue(muts[0]["after"])
        self.assertEqual(muts[1]["before"], muts[0]["after"],
                         "the edit's before is the write's after")
        self.assertTrue(muts[1]["after"])
        self.assertNotEqual(muts[1]["after"], muts[1]["before"])
        self.assertEqual((self.work / "made.txt").read_text(encoding="utf-8"), "two")

    def test_a_secret_echoed_by_a_tool_is_redacted(self):
        fp = FakeProvider([
            {"tool": {"name": "run_shell", "arguments": {"command": "echo " + SECRET}}},
            {"text": "done"},
        ])
        self.addCleanup(fp.stop)
        _registry_with_secret(fp)
        result = headless.run("echo the key", str(self.work), "p/m", timeout_s=60)
        raw = Path(result["manifest"]).read_text(encoding="utf-8")
        self.assertNotIn(SECRET, raw,
                         "a key echoed through a tool result must still be redacted")

    def test_nested_delegation_usage_charges_to_the_parent(self):
        fp = FakeProvider([
            {"tool": {"name": "task", "arguments": {"prompt": "investigate the thing"}}},
            {"text": "sub-agent finding"},
            {"text": "parent conclusion"},
        ])
        self.addCleanup(fp.stop)
        fp.register()
        result = headless.run("delegate", str(self.work), "p/m", timeout_s=120)
        loaded = manifest.load(result["manifest_id"])
        self.assertTrue(loaded["delegations"], "the delegation must be recorded")
        self.assertTrue(any(c["subagent"] for c in loaded["model_calls"]),
                        "the sub-agent's model call must appear, flagged")
        self.assertEqual(loaded["usage"]["input"], result["input_tokens"],
                         "manifest and headless row share one arithmetic")


if __name__ == "__main__":
    unittest.main()
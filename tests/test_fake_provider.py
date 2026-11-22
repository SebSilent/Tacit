"""The scripted provider's failure shapes, through a REAL process worker.

P0B.2 of the master plan: integration tests that need process semantics must
not monkeypatch an in-process engine. Each test here points a real worker
subprocess at the reusable FakeProvider and asserts the worker's observable
contract: what streamed, what executed, and that the process ended. No real
provider, no network beyond loopback, all state under a temporary TACIT_HOME.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from backend import config, session_state, turn_worker
from tests.fake_provider import FakeProvider
from tests.helpers import state_paths


class ProviderFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
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
        self._env = {}
        for key in ("TACIT_TURN_WORKER", "TACIT_HOME"):
            self._env[key] = os.environ.get(key)
        self.addCleanup(self._restore_env)
        os.environ["TACIT_HOME"] = str(self.home)
        os.environ["TACIT_TURN_WORKER"] = "process"
        session_state.reset()
        self.addCleanup(session_state.reset)
        self._workers: list = []
        self.addCleanup(self._settle_workers)

    def _restore_config(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)

    def _restore_env(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _settle_workers(self):
        for w in self._workers:
            try:
                if w.alive():
                    w.kill()
                if w.proc is not None:
                    w.proc.wait(timeout=10)
            except Exception:
                pass

    def _worker(self, fp: FakeProvider, sid: str = "fp") -> turn_worker.TurnWorker:
        fp.register()
        self.addCleanup(fp.stop)
        w = turn_worker.spawn(sid)
        ok = w.start(project=None, ref="p/m", chat=False, session=sid,
                     has_instructions=False, reasoning=None, max_steps=8,
                     messages=[{"role": "system", "content": "s"},
                               {"role": "user", "content": "go"}],
                     trace=[])
        self.assertTrue(ok, "the process worker must spawn")
        self._workers.append(w)
        return w

    def _run(self, fp: FakeProvider, sid: str = "fp") -> tuple[list, turn_worker.TurnWorker]:
        w = self._worker(fp, sid)
        events = list(w.events())
        w.proc.wait(timeout=20)
        self.assertFalse(w.alive(), "the worker must exit after its stream ends")
        return events, w


class TestScriptedBehaviors(ProviderFixture):
    def test_a_plain_response_completes(self):
        events, w = self._run(FakeProvider([{"text": "the answer"}]))
        kinds = [e.get("type") for e in events]
        self.assertIn("text", kinds)
        self.assertIn("done", kinds)
        self.assertEqual([e for e in events if e.get("type") == "error"], [])
        self.assertIn("the answer", w._trace[-1].get("text") or "")

    def test_one_tool_call_executes_and_reports(self):
        fp = FakeProvider([
            {"tool": {"name": "run_shell", "arguments": {"command": "echo tool-ran"}}},
            {"text": "finished"},
        ])
        events, w = self._run(fp)
        ends = [e for e in events if e.get("type") == "tool_end"]
        self.assertEqual(len(ends), 1, events)
        self.assertIn("tool-ran", ends[0].get("result") or "")
        self.assertFalse(ends[0].get("is_error"))
        self.assertTrue(any("finished" in (s.get("text") or "") for s in w._trace))
        self.assertEqual(len(fp.requests), 2, "the tool result must go back to the model")

    def test_a_delayed_response_is_cancellable_promptly(self):
        fp = FakeProvider([{"delay_s": 30, "text": "too late"}])
        w = self._worker(fp)
        self.assertTrue(fp.request_seen.wait(30), "the worker never called the model")
        started = time.monotonic()
        res = w.cancel(grace=1.5)
        self.assertLess(time.monotonic() - started, 12,
                        "stop must not wait out the delayed response")
        self.assertIn(res["how"], ("cancelled", "killed"))
        w.proc.wait(timeout=10)
        self.assertFalse(w.alive())

    def test_truncated_tool_json_never_executes(self):
        fp = FakeProvider([
            {"tool": {"name": "run_shell", "id": "call_cut",
                      "arguments": '{"command": "echo should-not-run'}},
            {"text": "recovered"},
        ])
        events, w = self._run(fp)
        ends = [e for e in events if e.get("type") == "tool_end"]
        self.assertEqual(len(ends), 1)
        result = ends[0].get("result") or ""
        self.assertTrue(result.startswith("ERROR:"), result)
        self.assertIn("cut off", result)
        self.assertNotIn("should-not-run", result,
                         "a severed call's arguments must not reach execution or its result")
        self.assertTrue(ends[0].get("is_error"))

    def test_repeated_identical_calls_hit_the_repeat_guard(self):
        call = {"tool": {"name": "run_shell", "arguments": {"command": "echo same"}}}
        fp = FakeProvider([call] * 6 + [{"text": "end"}])
        events, w = self._run(fp)
        ends = [e for e in events if e.get("type") == "tool_end"]
        blocked = [e for e in ends if "BLOCKED:" in (e.get("result") or "")]
        self.assertTrue(blocked, "identical calls must eventually be refused")
        executed = [e for e in ends if "BLOCKED:" not in (e.get("result") or "")]
        self.assertLessEqual(len(executed), 3,
                             "the guard caches after three runs, it does not re-run forever")
        self.assertFalse(w.alive())

    def test_a_malformed_body_terminates_without_hanging(self):
        events, w = self._run(FakeProvider([{"garbage": True}]))
        kinds = [e.get("type") for e in events]
        self.assertTrue("error" in kinds or "done" in kinds,
                        f"a garbage response must reach a terminal event, got {kinds}")
        self.assertFalse(w.alive())

    def test_http_429_is_reported_as_an_error(self):
        events, w = self._run(FakeProvider([{"status": 429}]))
        errors = [e.get("message") or "" for e in events if e.get("type") == "error"]
        self.assertTrue(any("429" in m for m in errors), errors)

    def test_http_500_is_reported_as_an_error(self):
        events, w = self._run(FakeProvider([{"status": 500}]))
        errors = [e.get("message") or "" for e in events if e.get("type") == "error"]
        self.assertTrue(any("500" in m for m in errors), errors)

    def test_a_connection_reset_is_reported_as_an_error(self):
        events, w = self._run(FakeProvider([{"reset": True}]))
        errors = [e.get("message") or "" for e in events if e.get("type") == "error"]
        self.assertTrue(errors, "a reset connection must surface as an error event")
        self.assertFalse(w.alive())


if __name__ == "__main__":
    unittest.main()
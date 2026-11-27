"""T0 (PLAN 20.2): the standing prompt tells the model what the sandbox is.

The measured failure: the model forgot it was wrapped, and the person had to
write "you are isolated, use sudo" into every session. The note is derived in
`backend/sandbox.prompt_note()` from the same primitives `run()` uses, so the
prompt can only claim what the chosen backend actually enforces - and it must
never claim isolation where there is none.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from backend import config, providers, sandbox
from backend.ai import prompts
from tests.helpers import state_paths


class SandboxAwareness(unittest.TestCase):
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
        providers.save({"sandbox": {"backend": "none"}})
        self._pops: list = []
        self.addCleanup(self._unpatch)

    def _restore(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)
        config.ensure_home()

    def _patch(self, mod, name, value):
        self._pops.append((mod, name, getattr(mod, name)))
        setattr(mod, name, value)

    def _unpatch(self):
        for mod, name, value in self._pops:
            setattr(mod, name, value)

    # ── the note per state ─────────────────────────────────────────────────
    def test_none_on_this_platform_never_claims_isolation(self):
        note = sandbox.prompt_note()
        self.assertIn("no isolation primitive", note,
                      "the default backend must not be dressed up as a sandbox")
        self.assertNotIn("bubblewrap", note)

    def test_bubblewrap_describes_the_actual_walls(self):
        if os.name == "nt":
            self.skipTest("bubblewrap is the POSIX primitive")
        self._patch(sandbox, "mechanism", lambda: "bubblewrap")
        providers.save({"sandbox": {"backend": "tacit-micro"}})
        note = sandbox.prompt_note()
        self.assertIn("bubblewrap", note)
        self.assertIn("PID namespace", note)
        self.assertIn("read-only", note)

    def test_a_missing_container_runtime_is_said_not_hidden(self):
        self._patch(sandbox, "mechanism", lambda: "none")
        self._patch(sandbox, "_container_runtime", lambda: "")
        providers.save({"sandbox": {"backend": "container"}})
        note = sandbox.prompt_note()
        self.assertIn("no container runtime", note, note)

    def test_a_live_container_backend_describes_container_reach(self):
        self._patch(sandbox, "_container_runtime", lambda: "docker")
        providers.save({"sandbox": {"backend": "container"}})
        note = sandbox.prompt_note()
        self.assertIn("container", note)
        self.assertIn("not supplied inside the container", note)
        self.assertNotIn("no isolation", note)

    def test_an_unknown_backend_is_named_and_shown_as_refused(self):
        providers.save({"sandbox": {"backend": "carrier-belt"}})
        note = sandbox.prompt_note()
        self.assertIn("carrier-belt", note)
        self.assertIn("refused", note)

    def test_the_sudo_route_line_needs_the_switch(self):
        if os.name == "nt":
            self.skipTest("the route exists on the POSIX wrappers")
        self._patch(sandbox, "mechanism", lambda: "bubblewrap")
        providers.save({"sandbox": {"backend": "tacit-micro"}})
        quiet = sandbox.prompt_note()
        self.assertNotIn("outside the sandbox", quiet)
        from backend import sudo as sudo_mod
        self._patch(sudo_mod, "armed", lambda: True)
        armed = sandbox.prompt_note()
        self.assertIn("outside the sandbox", armed)
        self.assertIn("setuid", armed)

    # ── it travels with the standing prompt ────────────────────────────────
    def test_the_standing_prompt_carries_the_note_and_chat_does_not(self):
        text = prompts.system_prompt(None, False, chat=False)
        agent_note = sandbox.prompt_note()
        self.assertIn(agent_note[:40], text,
                      "the executor prompt must state the sandbox")
        chat_text = prompts.system_prompt(None, False, chat=True)
        self.assertNotIn(agent_note[:40], chat_text,
                         "chat has no shell; it must not be told it does")

    def test_the_prompt_stays_within_its_published_budget(self):
        # The README publishes standing-prompt characters; the note is part of
        # that number now. Bounded here so the derived block cannot creep.
        size = len(prompts.system_prompt(None, False, chat=False))
        self.assertLess(size, 1250, f"the standing prompt grew to {size} chars")
        self.assertGreater(size, 1050, f"the note is missing: only {size} chars")

    # ── end to end: the run_turn request itself carries it ─────────────────
    def test_a_real_turn_request_contains_the_note(self):
        import json
        from backend import agent

        config.save_registry({"default": "p/m", "providers": {
            "p": {"baseUrl": "http://127.0.0.1:1/v1", "apiKey": "k",
                  "models": [{"id": "m", "contextWindow": 100000}]}}})
        seen: list = []
        orig = agent.engine.stream_chat

        class FakeEngine:
            def __call__(self, messages, **kw):
                seen.append(messages)
                return iter([{"type": "text", "delta": "done answer"},
                             {"type": "usage",
                              "usage": {"input": 10, "output": 2, "total": 12}},
                             {"type": "done", "model": {"ref": "p/m"}}])

        agent.engine.stream_chat = FakeEngine()
        try:
            # The way start_turn assembles it: the standing prompt is built by
            # the caller (session_runtime) and the note rides inside it.
            system = prompts.system_prompt(None, False, chat=False)
            list(agent.run_turn([{"role": "system", "content": system},
                                 {"role": "user", "content": "task"}],
                                project=None, ref="p/m"))
        finally:
            agent.engine.stream_chat = orig
        self.assertTrue(seen)
        note = sandbox.prompt_note()
        self.assertIn(note[:40], seen[0][0]["content"],
                      "the first request must carry the sandbox note")


if __name__ == "__main__":
    unittest.main()

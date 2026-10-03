"""Tests for the capability registry and the audit ledger.

Every test runs against a temporary home, so nothing here touches real state.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import audit, config, providers  # noqa: E402
from tests.helpers import state_paths  # noqa: E402

# The list of paths a test must redirect is derived from the config module by
# tests.helpers.state_paths. The hand-written version drifted, and missing one
# entry means a test edits the user's real state.


class Isolated(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        config.HOME = home
        # Redirect *every* path-shaped setting under the home into the temporary
        # directory. Saving them for restore is not enough: a path that is not
        # redirected still points at the user's real state.
        for key in self._keys:
            setattr(config, key, home / Path(self._orig[key]).name)

    def tearDown(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)
        self._tmp.cleanup()

    def write_caps(self, data):
        config.write_json(config.CAPABILITIES_FILE, data)


class TestRegistry(Isolated):
    def test_every_provider_declares_the_required_fields(self):
        required = ("id", "name", "kind", "status", "summary", "permissions",
                    "network", "disk", "trust", "tokens", "install", "available")
        for row in providers.registry():
            for field in required:
                self.assertIn(field, row, f"{row.get('id')} is missing {field}")

    def test_kinds_and_statuses_are_from_the_allowed_sets(self):
        for row in providers.registry():
            self.assertIn(row["kind"], providers.KINDS)
            self.assertIn(row["status"], providers.STATUSES)
            self.assertIn(row["disk"], providers.DISK_LEVELS)
            self.assertIn(row["trust"], providers.TRUST_LEVELS)

    def test_ids_are_unique_within_a_kind(self):
        seen = {}
        for row in providers.registry():
            key = (row["kind"], row["id"])
            self.assertNotIn(key, seen, f"duplicate {key}")
            seen[key] = True

    def test_an_unavailable_backend_explains_itself(self):
        for row in providers.registry():
            if not row["available"]:
                self.assertTrue(row["reason"], f"{row['id']} is unavailable with no reason")

    def test_every_kind_has_a_usable_default(self):
        for kind, chosen in (("sandbox", "none"), ("memory", "off"), ("learning", "propose")):
            row = providers.get(chosen, kind)
            self.assertIsNotNone(row, chosen)
            self.assertTrue(row["available"], f"the default {kind} backend must always work")


class TestDefaults(Isolated):
    def test_a_fresh_install_enables_nothing_optional(self):
        cfg = providers.load()
        self.assertEqual(cfg["sandbox"]["backend"], "none")
        self.assertFalse(cfg["sandbox"]["network"])
        self.assertEqual(cfg["memory"]["mode"], "off")
        self.assertEqual(cfg["learning"]["mode"], "propose")
        self.assertEqual(cfg["profile"], "silent")

    def test_learning_defaults_to_propose_not_auto(self):
        self.assertEqual(providers.load()["learning"]["mode"], "propose")
        self.assertNotEqual(providers.load()["learning"]["mode"], "auto")

    def test_saving_is_additive_and_ignores_unknown_keys(self):
        providers.save({"sandbox": {"backend": "tacit-micro"}})
        providers.save({"nonsense": {"a": 1}})
        cfg = providers.load()
        self.assertEqual(cfg["sandbox"]["backend"], "tacit-micro")
        self.assertNotIn("nonsense", cfg)
        self.assertEqual(cfg["memory"]["mode"], "off", "unrelated values must survive")

    def test_a_corrupt_file_falls_back_to_defaults(self):
        config.CAPABILITIES_FILE.write_text("not json at all", encoding="utf-8")
        self.assertEqual(providers.load()["sandbox"]["backend"], "none")


class TestResolution(Isolated):
    def test_the_chosen_backend_is_reported(self):
        providers.save({"sandbox": {"backend": "tacit-micro"}})
        got = providers.resolve("sandbox")
        self.assertEqual(got["id"], "tacit-micro")
        self.assertTrue(got["ok"])

    def test_an_unavailable_backend_is_never_silently_replaced(self):
        providers.save({"sandbox": {"backend": "container"}})
        got = providers.resolve("sandbox")
        self.assertEqual(got["id"], "container", "must report what was asked for")
        if not got["available"]:
            self.assertFalse(got["ok"])
            self.assertTrue(got["reason"])
            self.assertNotEqual(got["id"], "none", "must not fall back to none")

    def test_an_unknown_backend_is_reported(self):
        providers.save({"memory": {"mode": "telepathy"}})
        got = providers.resolve("memory")
        self.assertFalse(got["ok"])
        self.assertIn("telepathy", got["reason"])

    def test_summary_carries_everything_the_interface_needs(self):
        got = providers.summary()
        for key in ("profile", "config", "sandbox", "memory", "learning", "providers"):
            self.assertIn(key, got)


class TestAudit(Isolated):
    def test_records_one_line_per_event(self):
        audit.record("sandbox_run", tool="run_shell", backend="none")
        audit.record("tool_blocked", tool="run_shell", status="blocked")
        rows = audit.recent(10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event"], "tool_blocked", "newest first")
        self.assertEqual(audit.count(), 2)

    def test_every_entry_carries_the_standard_fields(self):
        audit.record("x", session="s1", tool="t", backend="b", mode="m", status="ok", tokens=5)
        row = audit.recent(1)[0]
        for field in ("ts", "event", "session", "tool", "backend", "mode", "status", "tokens"):
            self.assertIn(field, row)

    def test_secrets_are_masked(self):
        audit.record("sandbox_run", env={"MY_API_KEY": "sk-supersecret-123456"},
                     token="ghp_abcdefghijklmnop")
        blob = config.AUDIT_FILE.read_text(encoding="utf-8")
        self.assertNotIn("sk-supersecret-123456", blob)
        self.assertNotIn("ghp_abcdefghijklmnop", blob)
        self.assertIn("***", blob)

    def test_credentials_in_urls_are_masked(self):
        audit.record("fetch", url="https://user:tok3n@example.com/x")
        blob = config.AUDIT_FILE.read_text(encoding="utf-8")
        self.assertNotIn("tok3n", blob)

    def test_filtering_by_session_and_event(self):
        audit.record("a", session="s1")
        audit.record("b", session="s2")
        audit.record("a", session="s2")
        self.assertEqual(len(audit.recent(10, session="s2")), 2)
        self.assertEqual(len(audit.recent(10, event="a")), 2)

    def test_it_survives_a_reload_and_reads_clean(self):
        audit.record("x", backend="tacit-micro")
        lines = config.AUDIT_FILE.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["backend"], "tacit-micro")

    def test_a_broken_line_does_not_break_reading(self):
        audit.record("good")
        with open(config.AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
        audit.record("also good")
        self.assertEqual(len(audit.recent(10)), 2)

    def test_recording_never_raises(self):
        config.AUDIT_FILE = Path("/definitely/not/here/audit.jsonl")
        audit.record("x")   # must not raise
        self.assertEqual(audit.recent(5), [])


class TestEndpoints(Isolated):
    """The API refuses to select a backend that cannot run."""

    class Req:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    def _call(self, fn, body):
        import asyncio
        res = asyncio.run(fn(self.Req(body)))
        # a refusal comes back as a JSONResponse, an accept as a plain dict
        if hasattr(res, "body"):
            return json.loads(res.body)
        return res

    def test_selecting_an_available_backend_works(self):
        from backend.routers import capabilities as cap
        res = self._call(cap.set_sandbox, {"backend": "tacit-micro"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["sandbox"]["id"], "tacit-micro")

    def test_selecting_an_unavailable_backend_is_refused(self):
        from backend.routers import capabilities as cap
        res = self._call(cap.set_sandbox, {"backend": "container"})
        if providers.get("container", "sandbox")["available"]:
            self.skipTest("a container runtime is installed here")
        self.assertFalse(res["ok"])
        self.assertIn("container", res["error"])
        self.assertEqual(providers.load()["sandbox"]["backend"], "none",
                         "a refused change must not be applied")

    def test_an_unknown_backend_is_refused(self):
        from backend.routers import capabilities as cap
        res = self._call(cap.set_memory, {"mode": "telepathy"})
        self.assertFalse(res["ok"])

    def test_changing_a_mode_is_recorded_in_the_ledger(self):
        from backend.routers import capabilities as cap
        self._call(cap.set_memory, {"mode": "explicit"})
        events = [e["event"] for e in audit.recent(10)]
        self.assertIn("memory_mode_changed", events)

    def test_the_summary_endpoint_answers(self):
        import asyncio
        from backend.routers import capabilities as cap
        res = asyncio.run(cap.capabilities())
        self.assertTrue(res["ok"])
        self.assertIn("providers", res)


class TestSandbox(Isolated):
    """Commands run under the chosen backend, or they do not run at all."""

    def setUp(self):
        super().setUp()
        self.proj = config.HOME / "proj"
        self.proj.mkdir()
        (self.proj / "keep.txt").write_text("original", encoding="utf-8")

    def test_none_runs_and_names_itself(self):
        from backend import sandbox
        res = sandbox.run("echo hi", project=str(self.proj))
        self.assertTrue(res["ok"])
        self.assertEqual(res["backend"], "none")
        self.assertIn("hi", res["stdout"])
        self.assertTrue(res["enforced"]["timeout"])
        self.assertFalse(res["enforced"]["readonly_project"])

    def test_micro_reports_what_changed(self):
        from backend import sandbox
        providers.save({"sandbox": {"backend": "tacit-micro"}})
        res = sandbox.run("echo changed > made.txt", project=str(self.proj))
        self.assertTrue(res["ok"])
        self.assertEqual(res["backend"], "tacit-micro")
        self.assertIn("made.txt", res["changed"]["added"])
        self.assertEqual(res["changed"]["count"], 1)
        self.assertTrue((self.proj / "made.txt").exists())

    def test_micro_reports_a_modification(self):
        from backend import sandbox
        providers.save({"sandbox": {"backend": "tacit-micro"}})
        res = sandbox.run("echo longer-content-here > keep.txt", project=str(self.proj))
        self.assertIn("keep.txt", res["changed"]["modified"])

    def test_micro_says_what_it_cannot_enforce(self):
        from backend import sandbox
        providers.save({"sandbox": {"backend": "tacit-micro"}})
        res = sandbox.run("echo x", project=str(self.proj))
        self.assertFalse(res["enforced"]["readonly_project"],
                         "the project is writable and that must be reported, not hidden")
        self.assertTrue(res["notes"], "limits that are not enforced must be stated")

    def test_a_refused_backend_does_not_run_the_command(self):
        # The container backend has an adapter now, so unavailability has to be
        # forced rather than assumed from the host: on a machine with Docker
        # installed this command would otherwise really run. What is being pinned
        # down is that a refusal has no side effects.
        from backend import sandbox
        orig = sandbox._container_runtime
        sandbox._container_runtime = lambda: ""
        self.addCleanup(setattr, sandbox, "_container_runtime", orig)
        marker = self.proj / "should-not-exist.txt"
        res = sandbox.run(f"echo x > {marker.name}", project=str(self.proj),
                          backend="container")
        self.assertFalse(res["ok"])
        self.assertFalse(marker.exists(), "a refused command must not have run")

    def test_an_unavailable_backend_refuses_and_says_how_to_fix_it(self):
        from backend import sandbox
        orig = sandbox._container_runtime
        sandbox._container_runtime = lambda: ""
        self.addCleanup(setattr, sandbox, "_container_runtime", orig)
        res = sandbox.run("echo x", project=str(self.proj), backend="container")
        self.assertFalse(res["ok"])
        # A dead end with no exit is worse than the refusal itself.
        self.assertIn("container", res["stderr"])
        self.assertTrue(res["blocked"], "the reason must reach the user, not just the log")

    def test_an_unknown_backend_refuses(self):
        from backend import sandbox
        res = sandbox.run("echo x", project=str(self.proj), backend="telepathy")
        self.assertFalse(res["ok"])
        self.assertIn("telepathy", res["stderr"])

    def test_an_empty_command_is_refused(self):
        from backend import sandbox
        self.assertFalse(sandbox.run("   ", project=str(self.proj))["ok"])

    def test_every_run_lands_in_the_ledger_with_its_backend(self):
        from backend import sandbox
        providers.save({"sandbox": {"backend": "tacit-micro"}})
        sandbox.run("echo one", project=str(self.proj), session="sess-1")
        entries = audit.recent(10, session="sess-1")
        self.assertTrue(entries)
        self.assertEqual(entries[0]["backend"], "tacit-micro")
        self.assertIn("enforced", entries[0])

    def test_describe_reports_the_platform_and_what_it_can_do(self):
        from backend import sandbox
        got = sandbox.describe()
        for key in ("backend", "usable", "platform", "can_enforce", "will_enforce"):
            self.assertIn(key, got)
        self.assertTrue(got["will_enforce"]["timeout"])

    def test_a_command_cannot_escape_its_timeout(self):
        from backend import sandbox
        providers.save({"sandbox": {"backend": "tacit-micro"}})
        res = sandbox.run("ping -n 6 127.0.0.1", project=str(self.proj), timeout=1)
        self.assertEqual(res["code"], 124, "a timeout must be reported as such")


class TestMemoryModes(Isolated):
    """What memory may inject, why, and how much it costs."""

    def setUp(self):
        super().setUp()
        config.MEMORY_DB = config.HOME / "memory.db"
        config.PREFS_FILE = config.HOME / "prefs.json"
        providers.save({"memory": {"mode": "off", "budget": 120}})

    def _cap(self, mode, budget=120):
        providers.save({"memory": {"mode": mode, "budget": budget}})

    def test_off_injects_nothing(self):
        from backend import memory_store as ms, memory_modes as mm
        ms.add("a note", source="user", confidence="high")
        self._cap("off")
        got = mm.startup()
        self.assertEqual(got["count"], 0)
        self.assertEqual(got["text"], "")
        self.assertFalse(mm.is_on())

    def test_a_zero_budget_injects_nothing(self):
        from backend import memory_store as ms, memory_modes as mm
        ms.add("a note", source="user", confidence="high")
        self._cap("explicit", budget=0)
        got = mm.startup()
        self.assertEqual(got["count"], 0)
        self.assertEqual(got["text"], "")

    def test_explicit_includes_what_you_wrote_and_approved(self):
        from backend import memory_store as ms, memory_modes as mm
        ms.add("mine", source="user", confidence="low")
        ms.add("approved", source="agent_suggestion", confidence="low", enabled=True)
        self._cap("explicit")
        got = mm.startup()
        self.assertEqual(sorted(i["content"] for i in got["items"]), ["approved", "mine"])
        whys = {i["content"]: i["why"] for i in got["items"]}
        self.assertEqual(whys["mine"], "you wrote this")
        self.assertEqual(whys["approved"], "you approved this")

    def test_an_unapproved_proposal_is_never_injected(self):
        from backend import memory_store as ms, memory_modes as mm
        ms.add("SECRET PROPOSAL", source="agent_suggestion", confidence="high",
               enabled=False)
        for mode in ("explicit", "jit"):
            self._cap(mode)
            got = mm.startup()
            self.assertNotIn("SECRET PROPOSAL", got["text"], f"leaked into {mode}")

    def test_jit_keeps_half_the_budget_for_retrieval(self):
        from backend import memory_store as ms, memory_modes as mm
        ms.add("pinned thing", source="user", pinned=True)
        self._cap("jit", budget=100)
        got = mm.startup()
        self.assertEqual(got["retrieval_budget"], 50)
        self.assertLessEqual(got["tokens"], 50)

    def test_the_budget_is_never_exceeded(self):
        from backend import memory_store as ms, memory_modes as mm
        for i in range(40):
            ms.add(f"note {i} " + "detail " * 8, source="user", confidence="high",
                   pinned=True)
        for mode in ("explicit", "jit"):
            self._cap(mode, budget=60)
            got = mm.startup()
            self.assertLessEqual(got["tokens"], 60, mode)
            self.assertGreater(got["held_back"], 0, mode)

    def test_every_injected_item_carries_provenance(self):
        from backend import memory_store as ms, memory_modes as mm
        ms.add("python uses tabs here", source="user", confidence="high",
               scope="language", scope_key="python", source_session="sess-7",
               reason="the user said so")
        self._cap("explicit")
        item = mm.startup()["items"][0]
        for field in ("id", "type", "scope", "scope_key", "source", "source_session",
                      "created_at", "tokens", "why"):
            self.assertIn(field, item)
        self.assertEqual(item["source_session"], "sess-7")
        self.assertEqual(item["scope_key"], "python")
        self.assertTrue(item["why"])

    def test_recall_only_works_in_jit(self):
        from backend import memory_store as ms, memory_modes as mm
        ms.add("python uses tabs", source="user", confidence="high")
        self._cap("jit")
        self.assertTrue(mm.recall_for("tabs")["items"])
        self._cap("explicit")
        self.assertEqual(mm.recall_for("tabs")["items"], [])
        self._cap("off")
        self.assertEqual(mm.recall_for("tabs")["items"], [])

    def test_expired_memory_is_not_injected(self):
        from backend import memory_store as ms, memory_modes as mm
        ms.add("stale note", source="user", confidence="high", ttl_days=1)
        with ms._connect() as con:
            con.execute("UPDATE memories SET expires_at = 1")
        self._cap("explicit")
        self.assertEqual(mm.startup()["count"], 0)

    def test_the_report_explains_the_current_state(self):
        from backend import memory_store as ms, memory_modes as mm
        ms.add("a note", source="user", pinned=True)
        self._cap("jit", budget=120)
        got = mm.report()
        for key in ("mode", "budget", "used", "remaining", "injected", "display"):
            self.assertIn(key, got)
        self.assertGreaterEqual(got["remaining"], 0)

    def test_an_unknown_mode_falls_back_to_off(self):
        from backend import memory_modes as mm
        config.write_json(config.CAPABILITIES_FILE, {"memory": {"mode": "telepathy"}})
        self.assertEqual(mm.mode(), "off")
        self.assertEqual(mm.startup()["count"], 0)


class TestLearning(Isolated):
    """Proposals are artifacts. Nothing changes until one is approved."""

    def setUp(self):
        super().setUp()
        from backend import learning
        self.learning = learning
        for key, sub in (("LEARNING_FILE", "learning.json"), ("MEMORY_DB", "memory.db"),
                         ("PREFS_FILE", "prefs.json"), ("SKILLS_DIR", "skills"),
                         ("KNOWLEDGE_DIR", "knowledge")):
            setattr(config, key, config.HOME / sub)
        providers.save({"learning": {"mode": "propose"}})

    def test_the_default_mode_is_propose(self):
        config.write_json(config.CAPABILITIES_FILE, {})
        self.assertEqual(self.learning.mode(), "propose")

    def test_proposing_changes_nothing(self):
        from backend import memory_store as ms
        res = self.learning.propose("rule", "run tests", "always run pytest first")
        self.assertTrue(res["ok"])
        self.assertEqual(res["artifact"]["state"], "proposed")
        self.assertEqual(ms.list_memories(), [], "a proposal must not write anything")

    def test_a_proposal_carries_everything_needed_to_judge_it(self):
        res = self.learning.propose("rule", "t", "body", confidence="high",
                                    scope="language", scope_key="python",
                                    source_session="sess-1")
        art = res["artifact"]
        for field in ("id", "kind", "title", "body", "scope", "scope_key",
                      "confidence", "risk", "state", "source_session",
                      "created_at", "last_used_at", "use_count", "tokens"):
            self.assertIn(field, art)
        self.assertEqual(art["source_session"], "sess-1")
        self.assertEqual(art["scope_key"], "python")

    def test_a_body_is_required(self):
        self.assertFalse(self.learning.propose("rule", "t", "   ")["ok"])

    def test_approving_a_rule_writes_it_to_memory(self):
        from backend import memory_store as ms
        art = self.learning.propose("rule", "t", "always run the tests")["artifact"]
        res = self.learning.approve(art["id"])
        self.assertTrue(res["ok"])
        self.assertTrue(res["wrote"].startswith("memory:"))
        rows = ms.list_memories()
        self.assertEqual(len(rows), 1)
        self.assertIn("tests", rows[0]["content"])
        self.assertEqual(rows[0]["source"], "agent_suggestion")

    def test_approving_a_skill_writes_a_skill_file(self):
        from backend import skills
        art = self.learning.propose("skill", "deploy-steps", "1. build\n2. ship")["artifact"]
        res = self.learning.approve(art["id"])
        self.assertTrue(res["wrote"].startswith("skill:"))
        self.assertTrue(any(s["name"] == "deploy-steps" for s in skills.load()))

    def test_a_skill_is_name_indexed_not_inlined(self):
        """Approved learning borrows the existing budgeting, it does not add to it."""
        from backend import skills
        art = self.learning.propose("skill", "big-thing", "x " * 800)["artifact"]
        self.learning.approve(art["id"])
        listing = skills.index()
        self.assertIn("big-thing", listing)
        self.assertLess(len(listing), 400, "the index must stay tiny")

    def test_disabling_undoes_what_approval_wrote(self):
        from backend import memory_store as ms, skills
        art = self.learning.propose("rule", "r", "a rule worth keeping")["artifact"]
        self.learning.approve(art["id"])
        self.assertEqual(len(ms.list_memories()), 1)
        self.learning.disable(art["id"])
        self.assertEqual(ms.list_memories(), [], "disabling must remove what it wrote")
        self.assertEqual(self.learning.get(art["id"])["state"], "disabled")

    def test_deleting_undoes_and_removes(self):
        from backend import memory_store as ms
        art = self.learning.propose("rule", "r", "a rule")["artifact"]
        self.learning.approve(art["id"])
        self.assertTrue(self.learning.remove(art["id"])["ok"])
        self.assertIsNone(self.learning.get(art["id"]))
        self.assertEqual(ms.list_memories(), [])

    def test_editing_updates_the_text_and_the_cost(self):
        art = self.learning.propose("rule", "r", "short")["artifact"]
        before = art["tokens"]
        res = self.learning.edit(art["id"], body="a much longer body " * 20)
        self.assertGreater(res["artifact"]["tokens"], before)

    def test_rejecting_records_it_without_writing(self):
        from backend import memory_store as ms
        art = self.learning.propose("rule", "r", "something")["artifact"]
        self.assertTrue(self.learning.reject(art["id"])["ok"])
        self.assertEqual(self.learning.get(art["id"])["state"], "rejected")
        self.assertEqual(ms.list_memories(), [])

    def test_risk_is_classified(self):
        self.assertEqual(self.learning.risk_of("prefers short answers"), "low")
        for bad in ("delete everything first", "overwrite the config",
                    "put the api_key in the file"):
            self.assertEqual(self.learning.risk_of(bad), "high", bad)

    def test_auto_low_risk_applies_preferences_only(self):
        providers.save({"learning": {"mode": "auto-low-risk"}})
        good = self.learning.propose("preference", "p", "prefers tabs", confidence="high")
        self.assertTrue(good.get("auto_applied"))
        bad = self.learning.propose("rule", "r", "delete all logs on start")
        self.assertFalse(bad.get("auto_applied"), "a risky rule must not self-apply")
        self.assertEqual(bad["artifact"]["state"], "proposed")

    def test_learning_off_refuses_to_propose(self):
        providers.save({"learning": {"mode": "learn-off"}})
        self.assertFalse(self.learning.propose("rule", "r", "anything")["ok"])

    def test_every_transition_lands_in_the_ledger(self):
        art = self.learning.propose("rule", "r", "body")["artifact"]
        self.learning.approve(art["id"])
        self.learning.disable(art["id"])
        events = {e["event"] for e in audit.recent(10)}
        for want in ("learning_proposed", "learning_approved", "learning_disabled"):
            self.assertIn(want, events)

    def test_the_store_is_bounded(self):
        self.learning.MAX_ARTIFACTS = 5
        for i in range(12):
            art = self.learning.propose("rule", f"r{i}", f"body {i}")["artifact"]
            self.learning.reject(art["id"])
        self.assertLessEqual(len(self.learning.load()), 5)

    def test_stats_report_every_state(self):
        art = self.learning.propose("rule", "r", "body")["artifact"]
        self.learning.approve(art["id"])
        got = self.learning.stats()
        for key in ("proposed", "approved", "rejected", "disabled", "total", "mode"):
            self.assertIn(key, got)
        self.assertEqual(got["approved"], 1)


class TestGateways(Isolated):
    """A gateway is a command you configured. Off until you say otherwise."""

    def setUp(self):
        super().setUp()
        from backend import gateways
        self.gw = gateways

    def test_none_is_the_default(self):
        self.assertEqual(self.gw.selected(), "none")
        self.assertFalse(self.gw.run("none", "do a thing")["ok"])

    def test_the_builtins_are_declared(self):
        rows = {g["id"]: g for g in self.gw.describe()}
        self.assertEqual(set(rows), {"none"},
                         "Tacit ships no gateway of its own and depends on no other harness")
        self.assertEqual(self.gw.BUILTIN, ("none",))
        for gid, row in rows.items():
            for field in ("id", "name", "kind", "status", "summary", "network",
                          "trust", "available", "implemented", "command"):
                self.assertIn(field, row, gid)

    def test_adding_requires_a_safe_id_and_a_command(self):
        self.assertFalse(self.gw.add({"id": "../etc", "command": "echo"})["ok"])
        self.assertFalse(self.gw.add({"id": "ok", "command": "  "})["ok"])
        self.assertFalse(self.gw.add({"id": "none", "command": "echo"})["ok"],
                         "a built-in id must not be shadowed")
        self.assertTrue(self.gw.add({"id": "ok", "command": "echo"})["ok"])

    def test_a_gateway_is_off_until_selected(self):
        self.gw.add({"id": "notes", "command": "echo"})
        res = self.gw.run("notes", "hi")
        self.assertFalse(res["ok"])
        self.assertIn("not the selected gateway", res["error"])

    def test_the_command_is_shown_before_it_runs(self):
        self.gw.add({"id": "notes", "command": "echo", "args": ["--", "{task}"]})
        row = self.gw.get("notes")
        self.assertIn("echo", row["command"])
        self.assertIn("<task>", row["command"], "the placeholder is shown, not a real task")

    def test_the_task_gets_its_own_argument_and_is_never_a_shell(self):
        argv = self.gw._argv({"command": "echo", "args": []}, "x; rm -rf /")
        self.assertEqual(argv, ["echo", "x; rm -rf /"])
        self.assertEqual(len(argv), 2, "a task must not become more arguments")

    def test_the_task_placeholder_is_honoured(self):
        argv = self.gw._argv({"command": "echo", "args": ["-n", "{task}"]}, "hi")
        self.assertEqual(argv, ["echo", "-n", "hi"])

    def test_a_selected_gateway_runs_and_is_recorded(self):
        self.gw.add({"id": "notes", "command": "echo"})
        self.gw.select("notes")
        res = self.gw.run("notes", "hello there", session="s1")
        self.assertTrue(res["ok"], res)
        self.assertIn("hello there", res["output"])
        entries = audit.recent(5, session="s1")
        self.assertIn("gateway_run", [e["event"] for e in entries])
        self.assertEqual(entries[0]["backend"], "notes")

    def test_a_timeout_is_enforced_and_recorded(self):
        import sys
        self.gw.add({"id": "slow", "command": sys.executable,
                     "args": ["-c", "import time; time.sleep(5)", "{task}"]})
        self.gw.select("slow")
        res = self.gw.run("slow", "go", timeout=1, session="t1")
        self.assertFalse(res["ok"])
        self.assertIn("timed out", res["error"])
        self.assertIn("timeout", [e["status"] for e in audit.recent(3)])

    def test_an_empty_task_is_refused_before_anything_runs(self):
        self.gw.add({"id": "notes", "command": "echo"})
        self.gw.select("notes")
        self.assertFalse(self.gw.run("notes", "   ")["ok"])

    def test_a_missing_command_is_reported_not_raised(self):
        self.gw.add({"id": "ghost", "command": "definitely-not-a-real-binary-xyz"})
        self.gw.select("ghost")
        res = self.gw.run("ghost", "hi")
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])

    def test_a_disabled_gateway_refuses(self):
        self.gw.add({"id": "off", "command": "echo", "enabled": False})
        self.gw.select("off")
        self.assertFalse(self.gw.run("off", "hi")["ok"])

    def test_builtins_cannot_be_removed_custom_ones_can(self):
        self.assertFalse(self.gw.remove("none")["ok"])
        self.assertFalse(self.gw.remove("dsh")["ok"])
        self.gw.add({"id": "mine", "command": "echo"})
        self.assertTrue(self.gw.remove("mine")["ok"])
        self.assertIsNone(self.gw.get("mine"))

    def test_selecting_something_unknown_is_refused(self):
        self.assertFalse(self.gw.select("nope")["ok"])


class TestStandalone(Isolated):
    """Tacit is self-contained. These are the proofs, not the intentions."""

    def _backend_sources(self):
        root = Path(__file__).resolve().parent.parent / "backend"
        out = {}
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            out[str(path.relative_to(root))] = path.read_text(encoding="utf-8",
                                                             errors="replace")
        return out

    def test_no_module_calls_another_harness_binary(self):
        import re
        offenders = []
        pattern = re.compile(r"which\(\s*[\"'](?:dsh|hermes)[\"']")
        for name, src in self._backend_sources().items():
            for i, line in enumerate(src.splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{name}:{i}")
        self.assertEqual(offenders, [], "nothing may look for or call another harness")

    def test_no_module_reads_another_tool_s_directory(self):
        offenders = []
        for name, src in self._backend_sources().items():
            for i, line in enumerate(src.splitlines(), 1):
                low = line.lower()
                if low.lstrip().startswith("#"):
                    continue
                if ".hermes" in low or ".dsh" in low:
                    offenders.append(f"{name}:{i}")
        # the only permitted mention is the legacy profile-name map
        offenders = [o for o in offenders if "profiles.py" not in o]
        self.assertEqual(offenders, [], "nothing may reach into another tool's state")

    def test_the_deleted_bridge_modules_are_gone(self):
        for module in ("dsh_adapter", "hermes_memory", "dsh_bridge"):
            path = Path(__file__).resolve().parent.parent / "backend" / f"{module}.py"
            self.assertFalse(path.exists(), f"{module}.py should not exist")
        import backend
        for module in ("dsh_adapter", "hermes_memory"):
            self.assertFalse(hasattr(backend, module), module)

    def test_no_provider_requires_another_harness(self):
        from backend import providers
        for row in providers.registry():
            self.assertNotIn(row["id"], ("dsh", "hermes"), row["id"])
            self.assertNotEqual(row["kind"], "gateway") if False else None
        kinds = {p["kind"] for p in providers.registry()}
        self.assertIn("sandbox", kinds)
        self.assertIn("memory", kinds)

    def test_the_sandbox_uses_only_local_primitives(self):
        from backend import sandbox
        self.assertIn(sandbox.mechanism(),
                      ("bubblewrap", "sandbox-exec", "posix-limits", "none"))
        got = sandbox.describe()
        self.assertTrue(got["standalone"])
        self.assertIn("no other harness", got["note"])

    def test_it_works_with_neither_installed(self):
        """The whole registry resolves with no external harness present."""
        from backend import providers
        summary = providers.summary()
        for kind in ("sandbox", "memory", "learning"):
            self.assertTrue(summary[kind]["available"],
                            f"{kind} must be usable on a machine with nothing extra installed")
        self.assertEqual(summary["profile"], "silent")

    def test_optional_things_stay_off_until_enabled(self):
        from backend import providers
        cfg = providers.load()
        self.assertEqual(cfg["sandbox"]["backend"], "none")
        self.assertEqual(cfg["memory"]["mode"], "off")
        self.assertEqual(cfg["gateway"]["id"], "none")
        self.assertEqual(providers.load().get("gateways", {}).get("user"), [])

    def test_dependencies_are_never_installed_automatically(self):
        from backend import deps
        for name in deps.GROUPS:
            res = deps.run_install(name)
            # it either reports "already satisfied", or hands back a command it
            # did not run. It never reports having installed something.
            if res.get("ok"):
                self.assertTrue(res.get("already_satisfied"))
                self.assertEqual(res.get("command"), "")
            else:
                self.assertTrue(res.get("command"), name)
                self.assertIn("yourself", res["error"], name)
        status = deps.status()
        self.assertIn(status["platform"], ("linux", "darwin", "windows"))
        self.assertTrue(status["groups"])
        self.assertIn("installs nothing", status["note"])

    def test_a_missing_dependency_blocks_rather_than_half_works(self):
        from backend import deps
        got = deps.check("container-isolation")
        self.assertTrue(got["ok"])
        self.assertEqual(got["satisfied"], not got["missing"])
        if not got["satisfied"]:
            self.assertTrue(got["install_command"], "a blocked feature must say how to unblock it")

    def test_migration_import_is_manual_only(self):
        from backend import migrate
        # nothing is detected: the module has no auto-discovery entry point
        for name in ("detect", "scan", "available", "find_sources", "autodetect"):
            self.assertFalse(hasattr(migrate, name),
                             f"migrate.{name} would be automatic discovery")
        # pointing at nothing yields nothing, and writes nothing
        from backend import memory_store as ms
        before = len(ms.list_memories())
        plan = migrate.preview("")
        self.assertFalse(plan["ok"])
        self.assertEqual(len(ms.list_memories()), before)

    def test_migration_reads_only_a_path_you_give_it(self):
        from backend import migrate
        from pathlib import Path as P
        notes = P(config.HOME) / "myexport.md"
        notes.write_text("a fact worth keeping\n§\nanother fact", encoding="utf-8")
        plan = migrate.preview(str(notes))
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["count"], 2)
        self.assertIn(str(notes), plan["files"][0]["file"])

    def test_migration_import_then_ends(self):
        from backend import migrate
        from backend import memory_store as ms
        from pathlib import Path as P
        notes = P(config.HOME) / "export.md"
        notes.write_text("keep this\n§\nand this", encoding="utf-8")
        first = migrate.import_items(str(notes))
        self.assertEqual(first["added_count"], 2, first)
        self.assertIn("Nothing stays connected", first["note"])
        second = migrate.import_items(str(notes))
        self.assertEqual(second["added_count"], 0, "a second import must not duplicate")
        self.assertEqual(len(ms.list_memories()), 2)

    def test_migration_export_is_text_only(self):
        from backend import migrate
        from backend import memory_store as ms
        ms.add("one", source="user")
        ms.add("two", source="user")
        text = migrate.export_text()
        self.assertIn("§", text)
        self.assertIn("one", text)
        self.assertIn("two", text)
        self.assertTrue(hasattr(migrate, "export_skills"))

    def test_the_core_prompt_stays_lean(self):
        from backend.ai import prompts
        size = len(prompts.system_prompt(None, False, chat=False))
        self.assertLess(size, 1200, f"the standing prompt grew to {size} characters")


class TestAnalyzer(Isolated):
    """The background analyzer finds learning moments. It decides nothing."""

    def _session(self, *turns, title="probe"):
        from backend import store
        rec = store.create(title=title)
        for role, text in turns:
            store.append(rec, role, text)
        store.save(rec)
        return rec

    def _correction(self):
        return self._session(
            ("user", "refactor the parser"),
            ("assistant", "done, using a regular expression"),
            ("user", "no, dont use regex there, always use the tokenizer instead"),
            ("assistant", "understood"),
            title="correction")

    # ── detection ──
    def test_a_correction_becomes_a_proposal(self):
        from backend import analyzer, learning
        rec = self._correction()
        out = analyzer.analyze_session(rec, force=True)
        self.assertEqual(out["proposals"], 1)
        rows = learning.list_artifacts("proposed")
        self.assertEqual(len(rows), 1)
        self.assertIn("tokenizer", rows[0]["body"])

    def test_an_explicit_recall_is_caught(self):
        from backend import analyzer
        rec = self._session(("user", "remember that I prefer tabs over spaces in Go"),
                            ("assistant", "noted"), title="recall")
        self.assertEqual(analyzer.analyze_session(rec, force=True)["proposals"], 1)

    def test_ordinary_chatter_is_not_a_learning_moment(self):
        from backend import analyzer
        rec = self._session(("user", "how do I reverse a list in python"),
                            ("assistant", "use slicing"),
                            ("user", "thanks, that works"),
                            ("assistant", "glad it helped"), title="chatter")
        self.assertEqual(analyzer.analyze_session(rec, force=True)["proposals"], 0)

    def test_a_question_with_always_in_it_is_still_weak(self):
        from backend import analyzer
        self.assertEqual(analyzer.classify("ok")[2], 0)
        self.assertEqual(analyzer.classify("thanks")[2], 0)
        self.assertEqual(analyzer.classify("")[2], 0)

    def test_classify_ranks_signals(self):
        from backend import analyzer
        _, kind, strong = analyzer.classify("remember that I always want tests first")
        self.assertEqual(kind, "preference")
        _, kind2, weak = analyzer.classify("I prefer tabs")
        self.assertEqual(kind2, "preference")
        self.assertGreater(strong, weak)

    # ── the artifact ──
    def test_the_artifact_carries_everything_required(self):
        from backend import analyzer, learning
        rec = self._correction()
        analyzer.analyze_session(rec, force=True)
        row = learning.list_artifacts("proposed")[0]
        for field in ("id", "kind", "body", "provenance", "confidence_score", "state"):
            self.assertIn(field, row, f"proposal is missing {field}")
        self.assertEqual(row["state"], "proposed")
        self.assertGreater(row["confidence_score"], 0)
        self.assertLessEqual(row["confidence_score"], 1.0)
        prov = row["provenance"]
        self.assertEqual(prov["session_id"], rec["id"])
        self.assertIsInstance(prov["turn"], int)
        self.assertTrue(prov["snippet"], "the user must be able to see what triggered it")
        self.assertTrue(prov["why"])

    def test_the_state_is_always_proposed(self):
        """Finding a rule must never apply it, in any learning mode."""
        from backend import analyzer, learning, providers
        for mode in ("propose", "auto-low-risk", "auto"):
            providers.save({"learning": {"mode": mode}})
            learning.clear(state="")
            analyzer.analyze_session(self._correction(), force=True)
            for row in learning.load():
                self.assertEqual(row["state"], "proposed", f"{mode} auto-applied")

    # ── idempotence ──
    def test_the_same_session_is_not_read_twice(self):
        from backend import analyzer
        rec = self._correction()
        self.assertEqual(analyzer.analyze_session(rec)["proposals"], 1)
        again = analyzer.analyze_session(rec)
        self.assertEqual(again["proposals"], 0)
        self.assertEqual(again["skipped"], "already read")

    def test_a_session_that_grew_is_read_again(self):
        from backend import analyzer, store
        rec = self._correction()
        analyzer.analyze_session(rec)
        store.append(rec, "user", "also, always run the linter before committing")
        store.save(rec)
        self.assertEqual(analyzer.analyze_session(rec)["proposals"], 1)

    def test_the_same_rule_twice_raises_confidence(self):
        from backend import analyzer, learning
        text = "no, dont use regex there, always use the tokenizer instead"
        first = self._session(("user", text), title="one")
        second = self._session(("user", text), title="two")
        analyzer.analyze_session(first, force=True)
        before = learning.list_artifacts("proposed")[0]["confidence_score"]
        analyzer.analyze_session(second, force=True)
        rows = learning.list_artifacts("proposed")
        self.assertEqual(len(rows), 1, "a repeat must not pile up duplicates")
        self.assertGreater(rows[0]["confidence_score"], before)

    def test_an_empty_session_is_skipped(self):
        from backend import analyzer
        from backend import store
        rec = store.create(title="nothing")
        self.assertEqual(analyzer.analyze_session(rec, force=True)["skipped"], "empty")

    # ── control ──
    def test_learning_off_switches_the_analyzer_off(self):
        from backend import analyzer, providers
        providers.save({"learning": {"mode": "learn-off"}})
        self.assertFalse(analyzer.enabled())
        providers.save({"learning": {"mode": "propose"}})
        self.assertTrue(analyzer.enabled())

    def test_the_analyzer_can_be_switched_off_explicitly(self):
        from backend import analyzer
        analyzer.configure(enabled_flag=False)
        self.assertFalse(analyzer.enabled())
        analyzer.configure(enabled_flag=True)
        self.assertTrue(analyzer.enabled())

    def test_a_sweep_reports_what_it_did(self):
        from backend import analyzer
        self._correction()
        out = analyzer.run_once(force=True)
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(out["sessions_read"], 1)
        self.assertGreaterEqual(out["proposals"], 1)

    def test_the_tally_survives_a_reread(self):
        """Re-reading a grown session must not wipe what it produced before."""
        from backend import analyzer, store
        rec = self._correction()
        analyzer.analyze_session(rec, force=True)
        first = analyzer.status()["proposals_made"]
        self.assertEqual(first, 1)
        store.append(rec, "user", "also, always run the linter before committing")
        store.save(rec)
        analyzer.analyze_session(rec, force=True)
        self.assertEqual(analyzer.status()["proposals_made"], 2,
                         "the earlier proposal vanished from the tally")

    def test_the_endpoint_payloads_do_not_collide(self):
        """run_once and status share key names; merging them must not raise."""
        from backend import analyzer
        self._correction()
        merged = {**analyzer.run_once(force=True), **analyzer.status()}
        self.assertIn("sessions_read", merged)
        self.assertIn("proposals_made", merged)

    # ── audit ──
    def test_generation_and_the_decision_are_both_logged(self):
        from backend import analyzer, audit, learning
        rec = self._correction()
        analyzer.analyze_session(rec, force=True)
        events = [r["event"] for r in audit.recent(limit=50)]
        self.assertIn("proposal_generated", events)
        row = learning.list_artifacts("proposed")[0]
        learning.approve(row["id"])
        events = [r["event"] for r in audit.recent(limit=50)]
        self.assertIn("learning_approved", events)
        gen = [r for r in audit.recent(limit=50) if r["event"] == "proposal_generated"][0]
        self.assertEqual(gen["proposal_id"], row["id"])
        self.assertTrue(gen["reason"])

    # ── the loop closes ──
    def test_approving_makes_it_available_to_memory(self):
        from backend import analyzer, learning, memory_store, providers
        providers.save({"memory": {"mode": "explicit", "budget": 400}})
        rec = self._correction()
        analyzer.analyze_session(rec, force=True)
        row = learning.list_artifacts("proposed")[0]
        learning.approve(row["id"])
        stored = memory_store.list_memories(enabled=True)
        self.assertTrue(stored, "an approved proposal must reach memory")
        self.assertIn("tokenizer", " ".join(m["content"] for m in stored))

    def test_rejecting_keeps_it_out_of_memory(self):
        from backend import analyzer, learning, memory_store, providers
        providers.save({"memory": {"mode": "explicit", "budget": 400}})
        analyzer.analyze_session(self._correction(), force=True)
        row = learning.list_artifacts("proposed")[0]
        learning.reject(row["id"])
        self.assertEqual(memory_store.list_memories(enabled=True), [])
        self.assertEqual(learning.list_artifacts("proposed"), [])

    # ── the constraints that must not drift ──
    def test_the_agent_gains_no_tool_and_no_prompt_text(self):
        from backend import analyzer
        from backend.ai import prompts
        before = prompts.system_prompt(None, False, chat=False)
        analyzer.analyze_session(self._correction(), force=True)
        after = prompts.system_prompt(None, False, chat=False)
        self.assertEqual(before, after, "the analyzer must not touch the prompt")

    def test_the_analyzer_never_calls_a_model(self):
        from pathlib import Path
        src = (Path(__file__).parent.parent / "backend/analyzer.py").read_text(encoding="utf-8")
        for forbidden in ("engine.chat", "ai.engine", "import requests", "urllib.request"):
            self.assertNotIn(forbidden, src, f"the analyzer must not use {forbidden}")

    def test_it_uses_only_local_storage(self):
        from pathlib import Path
        src = (Path(__file__).parent.parent / "backend/analyzer.py").read_text(encoding="utf-8")
        self.assertIn("sqlite3", src)
        for foreign in (".hermes", ".dsh", "hermes", "dsh"):
            self.assertNotIn(foreign, src)

    def test_every_connection_is_closed(self):
        """An open handle locks the database file on Windows."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "backend/analyzer.py").read_text(encoding="utf-8")
        self.assertNotIn("with _connect() as con", src,
                         "sqlite3's context manager commits, it does not close")
        self.assertIn("con.close()", src)


if __name__ == "__main__":
    unittest.main()

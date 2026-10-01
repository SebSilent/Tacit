"""Tests for the token accounting, plugin manager, MCP client and Memory Vault.

Every test runs against a throwaway home directory, so nothing here touches the
user's real ~/.tacit state. The MCP tests drive a real subprocess speaking the
real protocol (tests/fake_mcp_server.py) rather than a mock.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import (agent, assistant, config, folders, gateways, hosting, mcp_registry,  # noqa: E402
                     memory_store, metrics, plugin_manager, profiles, providers, store,
                     tokens, vcs)
from tests.helpers import state_paths  # noqa: E402

FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"


class Isolated(unittest.TestCase):
    """Points every storage path under the user's home at a temporary one.

    The list is derived from the config module rather than written out. The
    hand-written version omitted the sessions directory, so tests that read as
    isolated wrote fixture sessions into the real store.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {key: getattr(config, key) for key in self._keys}
        self._home = config.HOME
        config.HOME = home
        for key in self._keys:
            target = home / Path(self._orig[key]).name
            setattr(config, key, target)
            if key.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        mcp_registry.stop_all()
        config.HOME = self._home
        for key, value in self._orig.items():
            setattr(config, key, value)
        self._tmp.cleanup()


# ── 1. token estimator ─────────────────────────────────────────────────────
class TestTokenEstimator(unittest.TestCase):
    def test_stable_and_monotonic(self):
        text = "def greet(name):\n    return f'hello {name}'\n"
        first = tokens.estimate_tokens(text)
        self.assertEqual(first, tokens.estimate_tokens(text))
        self.assertGreater(first, 0)
        self.assertGreater(tokens.estimate_tokens(text * 4),
                           tokens.estimate_tokens(text * 2))

    def test_empty_is_zero(self):
        for value in ("", None, 0):
            self.assertEqual(tokens.estimate_tokens(value), 0)

    def test_tool_schema_cost(self):
        schema = agent.TOOLS[0]
        self.assertGreater(tokens.estimate_tool_schema_tokens(schema), 0)
        self.assertEqual(tokens.estimate_tools_tokens(agent.TOOLS),
                         sum(tokens.estimate_tool_schema_tokens(t) for t in agent.TOOLS))

    def test_messages_and_formatting(self):
        msgs = [{"role": "user", "content": "x" * 400}]
        self.assertGreater(tokens.estimate_messages_tokens(msgs), 90)
        self.assertEqual(tokens.format_token_count(0), "0")
        self.assertEqual(tokens.format_token_count(999), "999")
        self.assertEqual(tokens.format_token_count(1234), "1.2k")
        self.assertTrue(tokens.label(1234).endswith("1.2k"))


# ── 2. plugin manager ──────────────────────────────────────────────────────
class TestPluginManager(Isolated):
    def test_listing_and_toggle(self):
        rows = {p["id"]: p for p in plugin_manager.list_plugins()}
        self.assertIn("memory_vault", rows)
        self.assertNotIn("dsh_bridge", rows,
                         "Tacit ships no bridge to another harness")
        self.assertFalse(rows["memory_vault"]["enabled"])
        self.assertFalse(rows["memory_vault"]["provides"])

        self.assertTrue(plugin_manager.enable("memory_vault")["ok"])
        self.assertTrue(plugin_manager.is_enabled("memory_vault"))
        names = [t["function"]["name"] for t in plugin_manager.collect_tools()]
        self.assertIn("memory_recall", names)

        self.assertTrue(plugin_manager.disable("memory_vault")["ok"])
        self.assertFalse(plugin_manager.is_enabled("memory_vault"))
        self.assertEqual(plugin_manager.collect_tools(), [])

    def test_settings_roundtrip(self):
        plugin_manager.enable("memory_vault")
        res = plugin_manager.settings_of("memory_vault")
        self.assertTrue(res["ok"])
        self.assertIn("auto_approve", res["values"])
        plugin_manager.save_settings("memory_vault", {"auto_approve": True})
        self.assertTrue(plugin_manager.settings_of("memory_vault")["values"]["auto_approve"])

    def test_unknown_plugin(self):
        self.assertFalse(plugin_manager.enable("nope")["ok"])


# ── 3-6. MCP ───────────────────────────────────────────────────────────────
class TestMcp(Isolated):
    def _add(self):
        return mcp_registry.add_server({
            "id": "fake", "name": "Fake", "command": sys.executable,
            "args": [str(FAKE_SERVER)], "enabled": True, "timeout": 20,
        })

    def test_disabled_by_default_and_never_autostarts(self):
        res = mcp_registry.add_server({"id": "x", "command": sys.executable,
                                       "args": [str(FAKE_SERVER)]})
        self.assertTrue(res["ok"])
        self.assertFalse(res["server"]["enabled"])
        self.assertEqual(mcp_registry.start("x")["ok"], False)

    def test_start_discover_and_no_prompt_bloat(self):
        self.assertTrue(self._add()["ok"])
        base_count = len(agent.TOOLS)
        # nothing injected before activation
        self.assertEqual(len(agent.tools_for()), base_count)

        started = mcp_registry.start("fake")
        self.assertTrue(started["ok"], started)
        self.assertEqual(started["tools"], 3)

        tools = mcp_registry.all_tools()
        self.assertEqual(len(tools), 3)
        self.assertEqual(mcp_registry.status("fake")["state"], "running")
        # discovery alone must not touch the prompt
        self.assertEqual(len(agent.tools_for()), base_count)

        reported = mcp_registry.injection_report()
        self.assertGreater(reported["discovered_tokens"], 0)
        self.assertEqual(reported["injected_tokens"], 0)
        self.assertGreater(reported["saved_tokens"], 0)

    def test_search_finds_tool(self):
        self._add()
        mcp_registry.start("fake")
        hits = mcp_registry.search("echo", 5)
        self.assertEqual(hits[0]["name"], "echo")
        self.assertIn("fake:echo", [h["key"] for h in hits])

    def test_call_and_activation(self):
        self._add()
        mcp_registry.start("fake")
        base_count = len(agent.TOOLS)

        out = mcp_registry.call("fake", "echo", {"text": "hi"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"], "echo: hi")

        nums = mcp_registry.call("fake", "add_numbers", {"a": 2, "b": 3})
        self.assertEqual(nums["result"], "5")

        # activation injects the schema, and only that one
        mcp_registry.activate(["fake:echo"], 2)
        names = [t["function"]["name"] for t in agent.tools_for()]
        self.assertIn("mcp__fake__echo", names)
        self.assertEqual(len(agent.tools_for()), base_count + 1)

        mcp_registry.deactivate(["fake:echo"])
        self.assertEqual(len(agent.tools_for()), base_count)

    def test_destructive_tool_needs_confirmation(self):
        self._add()
        mcp_registry.start("fake")
        blocked = mcp_registry.call("fake", "delete_everything", {})
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["needs_confirmation"])
        allowed = mcp_registry.call("fake", "delete_everything", {}, confirmed=True)
        self.assertTrue(allowed["ok"], allowed)

    def test_policy_deny_list_blocks_calls(self):
        self._add()
        mcp_registry.start("fake")
        mcp_registry.update_server("fake", {"deny_tools": ["echo"]})
        res = mcp_registry.call("fake", "echo", {"text": "nope"})
        self.assertFalse(res["ok"])
        self.assertIn("blocked", res["error"])

    def test_ttl_expiry_prunes_activated_tools(self):
        self._add()
        mcp_registry.start("fake")
        mcp_registry.activate(["fake:echo"], 1)
        self.assertTrue(mcp_registry.active_tools())
        mcp_registry.next_turn()
        mcp_registry.next_turn()
        self.assertEqual(mcp_registry.active_tools(), [])

    def test_direct_mode_is_off_by_default(self):
        self._add()
        mcp_registry.start("fake")
        self.assertFalse(mcp_registry.settings()["direct_mode"])
        self.assertEqual(mcp_registry.schemas_for_prompt(), [])
        mcp_registry.save_settings({"direct_mode": True})
        self.assertEqual(len(mcp_registry.schemas_for_prompt()), 3)
        mcp_registry.save_settings({"direct_mode": False})


# ── 7-8. Memory Vault ──────────────────────────────────────────────────────
class TestMemoryVault(Isolated):
    def test_crud_and_token_estimate(self):
        added = memory_store.add("The user prefers tabs over spaces.",
                                 type="preference", confidence="high", pinned=True)
        self.assertTrue(added["ok"])
        mem = added["memory"]
        self.assertGreater(mem["token_estimate"], 0)

        edited = memory_store.update(mem["id"], content="The user prefers 4-space indents.")
        self.assertTrue(edited["ok"])
        self.assertIn("4-space", edited["memory"]["content"])

        rows = memory_store.list_memories(search="4-space")
        self.assertEqual(len(rows), 1)

        self.assertTrue(memory_store.delete(mem["id"])["ok"])
        self.assertEqual(memory_store.list_memories(), [])

    def test_recall_marks_usage(self):
        memory_store.add("Deploys happen from the release branch.", type="decision",
                         confidence="high")
        hits = memory_store.recall("release branch")
        self.assertTrue(hits)
        self.assertGreaterEqual(hits[0]["use_count"], 1)

    def test_budget_caps_startup_injection(self):
        for i in range(40):
            memory_store.add(f"Fact {i}: " + ("detail " * 3), type="project_fact",
                             confidence="high", pinned=True)
        memory_store.set_budget(60)
        selection = memory_store.startup_selection()
        self.assertLessEqual(selection["tokens"], 60)
        self.assertTrue(selection["used"], "some small memories should fit")
        stats = memory_store.stats()
        self.assertEqual(stats["budget"], 60)
        self.assertGreater(stats["excluded_by_budget"], 0)

    def test_oversized_memory_is_excluded_not_injected(self):
        # A single memory larger than the whole budget must not be injected.
        memory_store.add("x " * 400, type="project_fact", pinned=True)
        memory_store.set_budget(50)
        selection = memory_store.startup_selection()
        self.assertEqual(selection["used"], [])
        self.assertLessEqual(selection["tokens"], 50)

    def test_budget_zero_disables_injection(self):
        memory_store.add("Something important.", pinned=True)
        memory_store.set_budget(0)
        self.assertEqual(memory_store.startup_selection()["tokens"], 0)

    def test_hard_max_budget_without_override(self):
        memory_store.set_budget(99999)
        self.assertEqual(memory_store.budget(), memory_store.HARD_MAX_BUDGET)

    def test_disabled_memory_is_not_injected(self):
        added = memory_store.add("Transient note.", pinned=True)
        memory_store.update(added["memory"]["id"], enabled=False, pinned=False)
        self.assertEqual(memory_store.startup_selection()["used"], [])


# ── 9. snapshots still work ────────────────────────────────────────────────class TestSnapshots(Isolated):
    def test_snapshot_and_restore(self):
        from backend import extras

        project = config.HOME / "proj"
        project.mkdir()
        (project / "a.txt").write_text("original", encoding="utf-8")

        made = extras.snapshot(str(project), "test")
        self.assertIn("snapshot", made)

        (project / "a.txt").write_text("changed", encoding="utf-8")
        self.assertEqual((project / "a.txt").read_text(encoding="utf-8"), "changed")

        index = extras.snapshot_index()
        self.assertTrue(index)
        self.assertEqual(index[0]["file_count"], 1)
        self.assertIn("a.txt", index[0]["files"])
        self.assertEqual(index[0]["label"], "test")

        restored = extras.restore(index[0]["name"], str(project))
        self.assertIn("restored", restored)
        self.assertEqual((project / "a.txt").read_text(encoding="utf-8"), "original")


# ── 10. secrets never leak ─────────────────────────────────────────────────
class TestSecretHygiene(Isolated):
    SECRET = "sk-do-not-leak-me-1234567890"

    def test_env_is_masked_in_api_and_audit(self):
        mcp_registry.add_server({
            "id": "leaky", "name": "Leaky", "command": sys.executable,
            "args": [str(FAKE_SERVER)], "enabled": True,
            "env": {"MY_API_KEY": self.SECRET, "PLAIN": "fine"},
        })
        listed = mcp_registry.list_servers_for("leaky")
        self.assertEqual(listed["env"]["MY_API_KEY"], "***")
        self.assertEqual(listed["env"]["PLAIN"], "fine")

        mcp_registry.start("leaky")           # writes an audit entry with env
        mcp_registry.call("leaky", "echo", {"text": "hi"})

        audit_path = config.HOME / "mcp_audit.jsonl"
        blob = audit_path.read_text(encoding="utf-8")
        self.assertNotIn(self.SECRET, blob)
        self.assertIn("server_started", blob)
        self.assertIn("tool_invoked", blob)

    def test_redact_helpers(self):
        from backend.mcp_client import redact, redact_env
        env = redact_env({"API_KEY": self.SECRET, "PATH": "/usr/bin"})
        self.assertEqual(env["API_KEY"], "***")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn(self.SECRET, redact(f"token={self.SECRET}", [self.SECRET]))


# ── metrics / dashboard ────────────────────────────────────────────────────
class TestMetrics(unittest.TestCase):
    def test_bump_and_dashboard(self):
        rec = {}
        metrics.bump(rec, prompt_tokens=100, completion_tokens=20, saved_lazy_tools=500)
        dash = metrics.dashboard(rec, base_prompt_tokens=980, tools=2000,
                                 mcp_discovered=8000, memory_total=900, mcp_injected=0)
        self.assertEqual(dash["total_tokens"], 120)
        self.assertEqual(dash["saved_total"], 500)
        self.assertEqual(dash["full_context_baseline"], 980 + 2000 + 8000 + 900)
        self.assertGreater(dash["saved_by_discipline"], 0)


class TestSnapshotCompare(Isolated):
    def test_compare_reports_changed_added_removed(self):
        from backend import extras
        project = config.HOME / "proj"
        project.mkdir()
        (project / "keep.txt").write_text("same", encoding="utf-8")
        (project / "change.txt").write_text("before", encoding="utf-8")
        (project / "gone.txt").write_text("bye", encoding="utf-8")
        extras.snapshot(str(project), "base")
        name = extras.snapshot_index()[0]["name"]

        (project / "change.txt").write_text("after", encoding="utf-8")
        (project / "gone.txt").unlink()
        (project / "new.txt").write_text("hi", encoding="utf-8")

        res = extras.snapshot_compare(name, str(project))
        self.assertTrue(res["ok"], res)
        self.assertIn("change.txt", res["changed"])
        self.assertIn("new.txt", res["added"])
        self.assertIn("gone.txt", res["removed"])
        self.assertNotIn("keep.txt", res["changed"])

    def test_compare_identical_is_empty(self):
        from backend import extras
        project = config.HOME / "p2"
        project.mkdir()
        (project / "a.txt").write_text("x", encoding="utf-8")
        extras.snapshot(str(project))
        name = extras.snapshot_index()[0]["name"]
        self.assertEqual(extras.snapshot_compare(name, str(project))["count"], 0)

    def test_compare_unknown_snapshot(self):
        from backend import extras
        project = config.HOME / "p3"
        project.mkdir()
        self.assertFalse(extras.snapshot_compare("nope", str(project))["ok"])


class TestMcpHttpTransport(Isolated):
    def test_http_server_requires_an_endpoint_url(self):
        mcp_registry.add_server({"id": "h", "command": "not-a-url",
                                 "transport": "http", "enabled": True})
        out = mcp_registry.start("h")
        self.assertFalse(out["ok"])
        self.assertIn("http", out["error"].lower())

    def test_unreachable_endpoint_fails_gracefully(self):
        mcp_registry.add_server({"id": "h2", "command": "http://127.0.0.1:9/mcp",
                                 "transport": "http", "enabled": True, "timeout": 3})
        out = mcp_registry.start("h2")
        self.assertFalse(out["ok"])          # refused, but must not raise
        self.assertTrue(mcp_registry.status("h2")["error"])

    def test_headers_kept_and_masked(self):
        mcp_registry.add_server({"id": "h3", "command": "http://127.0.0.1:9/mcp",
                                 "transport": "http",
                                 "headers": {"Authorization": "Bearer sk-abcdef123456"}})
        row = mcp_registry.list_servers_for("h3")
        self.assertIn("Authorization", row["header_keys"])
        self.assertEqual(row["headers"]["Authorization"], "***")

    def test_url_credentials_masked(self):
        from backend.mcp_client import mask_url
        self.assertNotIn("supersecret", mask_url("https://supersecret@host/mcp"))


class TestProfiles(Isolated):
    def test_builtins_are_listed_with_costs(self):
        rows = {p["name"]: p for p in profiles.list_profiles()}
        for name in ("minimal", "silent", "safe", "power-isolation",
                     "power-memory", "full"):
            self.assertIn(name, rows)
        self.assertNotIn("dsh", rows, "no profile is named after another harness")
        self.assertNotIn("hermes", rows, "no profile is named after another harness")
        self.assertTrue(rows["silent"]["active"])
        self.assertFalse(rows["silent"]["mcp_direct"])

    def test_legacy_names_still_resolve(self):
        for old, new in profiles.LEGACY_NAMES.items():
            self.assertIn(new, {p["name"] for p in profiles.list_profiles()})
            config.write_json(config.PROFILES_FILE, {"active": old, "profiles": {}})
            self.assertEqual(profiles.load()["active"], new)
            self.assertTrue(profiles.apply(old)["ok"])

    def test_minimal_narrows_the_tool_set(self):
        rows = {p["name"]: p for p in profiles.list_profiles()}
        self.assertEqual(rows["minimal"]["tool_count"], len(profiles.CORE_TOOLS))
        self.assertGreater(rows["silent"]["tool_count"], rows["minimal"]["tool_count"])
        self.assertLess(rows["minimal"]["cost"]["total"], rows["silent"]["cost"]["total"])

    def test_cost_ordering(self):
        rows = {p["name"]: p["cost"]["total"] for p in profiles.list_profiles()}
        self.assertLess(rows["minimal"], rows["silent"])
        self.assertLessEqual(rows["silent"], rows["full"])

    def test_every_profile_pays_for_the_base_prompt(self):
        for p in profiles.list_profiles():
            self.assertGreater(p["cost"]["prompt"], 0)
            self.assertGreaterEqual(p["cost"]["total"], p["cost"]["prompt"])

    def test_every_profile_declares_its_capabilities(self):
        from backend import providers
        for name, cfg in profiles.BUILTIN.items():
            caps = cfg.get("capabilities") or {}
            for kind in ("sandbox", "memory", "learning"):
                self.assertIn(kind, caps, f"{name} does not declare {kind}")
                self.assertIsNotNone(providers.get(caps[kind], kind),
                                     f"{name} names an unknown {kind} backend")

    def test_no_profile_enables_autonomous_learning(self):
        for name, cfg in profiles.BUILTIN.items():
            mode = (cfg.get("capabilities") or {}).get("learning")
            self.assertNotEqual(mode, "auto", f"{name} turns learning loose")
            self.assertNotEqual(mode, "auto-low-risk", f"{name} turns learning loose")

    def test_a_profile_applies_its_capabilities(self):
        profiles.apply("safe")
        caps = providers.load()
        self.assertEqual(caps["sandbox"]["backend"], "tacit-micro")
        self.assertEqual(caps["memory"]["mode"], "explicit")

    def test_every_profile_is_satisfiable_without_anything_external(self):
        """No profile may depend on a tool Tacit does not implement itself.

        This is the correction of direction: every capability a profile names is
        one Tacit provides. If a profile ever points at something external, this
        fails.
        """
        from backend import providers
        applied = {}
        for name, cfg in profiles.BUILTIN.items():
            for kind, wanted in (cfg.get("capabilities") or {}).items():
                got = providers.resolve(kind) if False else providers.get(wanted, kind)
                self.assertIsNotNone(got, f"{name} names an unknown {kind}: {wanted}")
                self.assertTrue(got["implemented"],
                                f"{name} wants {wanted}, which Tacit does not implement")
                self.assertTrue(got["available"],
                                f"{name} wants {wanted}, which needs something installed")
                applied[kind] = wanted
        # and applying the heaviest profile actually works, with nothing external
        res = profiles.apply("power-isolation")
        self.assertTrue(res["ok"])
        self.assertEqual(res["skipped"], [], "a standalone profile must not skip anything")

    def test_turning_memory_off_takes_the_recall_tools_away(self):
        profiles.apply("safe")
        self.assertTrue(plugin_manager.is_enabled("memory_vault"))
        profiles.apply("silent")
        self.assertFalse(plugin_manager.is_enabled("memory_vault"))
        self.assertEqual(providers.load()["memory"]["mode"], "off")

    def test_applying_a_profile_is_recorded(self):
        from backend import audit
        profiles.apply("safe")
        self.assertIn("profile_applied", [e["event"] for e in audit.recent(5)])

    def test_apply_changes_plugin_state(self):
        self.assertTrue(profiles.apply("safe")["ok"])
        self.assertTrue(plugin_manager.is_enabled("memory_vault"))
        self.assertTrue(profiles.apply("silent")["ok"])
        self.assertFalse(plugin_manager.is_enabled("memory_vault"))
        self.assertEqual(memory_store.budget(), 0)

    def test_unknown_profile_refused(self):
        self.assertFalse(profiles.apply("nope")["ok"])

    def test_capture_and_delete(self):
        plugin_manager.enable("memory_vault")
        made = profiles.capture("my setup", "My setup")
        self.assertTrue(made["ok"])
        self.assertEqual(made["name"], "my-setup")
        self.assertIn("my-setup", {p["name"] for p in profiles.list_profiles()})
        self.assertTrue(profiles.delete("my-setup")["ok"])
        self.assertNotIn("my-setup", {p["name"] for p in profiles.list_profiles()})

    def test_builtin_cannot_be_deleted_or_shadowed(self):
        self.assertFalse(profiles.delete("silent")["ok"])
        self.assertFalse(profiles.capture("silent")["ok"])

    def test_apply_minimal_disables_everything_else(self):
        profiles.apply("minimal")
        live = set(profiles.current()["tools"] or [])
        self.assertEqual(live, set(profiles.CORE_TOOLS))
        self.assertNotIn("browser", live)

    def test_switching_back_restores_every_tool(self):
        profiles.apply("minimal")
        profiles.apply("silent")
        self.assertIsNone(profiles.current()["tools"])
        self.assertEqual(profiles.current()["tool_count"], len(agent.TOOLS))


class TestCommitIdentity(Isolated):
    """Commits must carry a real identity, never a placeholder."""

    def setUp(self):
        super().setUp()
        for key in ("TACIT_VCS_NAME", "TACIT_VCS_EMAIL"):
            os.environ.pop(key, None)

    def test_github_identity_uses_the_noreply_address(self):
        ident = vcs.github_identity("octocat")
        self.assertEqual(ident["name"], "octocat")
        self.assertEqual(ident["email"], "octocat@users.noreply.github.com")
        self.assertEqual(ident["source"], "github")
        self.assertIsNone(vcs.github_identity(""))

    def test_github_display_name_goes_on_the_commit(self):
        ident = vcs.github_identity("SebSilent", "Silent")
        self.assertEqual(ident["name"], "Silent")
        self.assertEqual(ident["email"], "SebSilent@users.noreply.github.com")
        self.assertEqual(ident["login"], "SebSilent")
        self.assertEqual(vcs.github_identity("SebSilent", "  ")["name"], "SebSilent")

    def test_save_identity_validates(self):
        self.assertIsNone(vcs.saved_identity())
        self.assertTrue(vcs.save_identity("Ada Lovelace", "ada@example.com")["ok"])
        self.assertEqual(vcs.saved_identity()["name"], "Ada Lovelace")
        self.assertEqual(vcs.saved_identity()["source"], "settings")
        self.assertFalse(vcs.save_identity("", "a@b.c")["ok"])
        self.assertFalse(vcs.save_identity("Ada", "not-an-email")["ok"])
        self.assertFalse(vcs.save_identity("Ada", "two words@x.y")["ok"])

    def test_env_identity(self):
        self.assertIsNone(vcs.env_identity())
        os.environ["TACIT_VCS_NAME"] = "Env User"
        os.environ["TACIT_VCS_EMAIL"] = "env@example.com"
        try:
            self.assertEqual(vcs.env_identity()["name"], "Env User")
            self.assertEqual(vcs.env_identity()["source"], "environment")
        finally:
            os.environ.pop("TACIT_VCS_NAME", None)
            os.environ.pop("TACIT_VCS_EMAIL", None)

    def _resolve(self, cwd=None):
        import asyncio
        from backend.routers import vcs as vcs_router
        return asyncio.run(vcs_router._commit_identity(cwd or str(config.HOME)))

    def test_defaults_to_tacit_and_never_blocks(self):
        ident, why = self._resolve()
        self.assertEqual(ident["source"], "tacit")
        self.assertEqual(ident["name"], "Tacit")
        self.assertEqual(ident["email"], "tacit@localhost")
        self.assertEqual(why, "")

    def test_the_default_comes_from_one_constant(self):
        self.assertEqual(vcs.default_identity(), vcs.DEFAULT_IDENTITY)
        self.assertEqual(vcs.DEFAULT_IDENTITY["name"], "Tacit")

    def test_a_choice_is_remembered_across_sessions(self):
        vcs.save_identity("Silent", "SebSilent@users.noreply.github.com")
        stored = config.read_json(config.PREFS_FILE, {})
        self.assertEqual(stored.get("vcsName"), "Silent")
        ident, _ = self._resolve()
        self.assertEqual(ident["name"], "Silent")
        self.assertEqual(ident["source"], "settings")

    def test_the_github_choice_also_persists(self):
        gh = vcs.github_identity("SebSilent", "Silent")
        self.assertTrue(vcs.save_identity(gh["name"], gh["email"])["ok"])
        ident, _ = self._resolve()
        self.assertEqual(ident["name"], "Silent")
        self.assertEqual(ident["email"], "SebSilent@users.noreply.github.com")

    def _with_repository(self, ident):
        from backend import vcs as vcs_mod
        original = vcs_mod.configured_identity
        vcs_mod.configured_identity = lambda cwd: ident
        return original

    def test_a_chosen_identity_wins_over_the_repository(self):
        from backend import vcs as vcs_mod
        vcs.save_identity("Chosen", "chosen@example.com")
        original = self._with_repository({"name": "Repo", "email": "repo@example.com",
                                          "source": "repository"})
        try:
            ident, _ = self._resolve()
        finally:
            vcs_mod.configured_identity = original
        self.assertEqual(ident["source"], "settings")
        self.assertEqual(ident["name"], "Chosen")

    def test_repository_config_is_used_when_nothing_is_chosen(self):
        from backend import vcs as vcs_mod
        original = self._with_repository({"name": "Repo", "email": "repo@example.com",
                                          "source": "repository"})
        try:
            ident, _ = self._resolve()
        finally:
            vcs_mod.configured_identity = original
        self.assertEqual(ident["source"], "repository")
        self.assertEqual(ident["name"], "Repo")

    def test_env_is_used_when_nothing_is_chosen(self):
        os.environ["TACIT_VCS_NAME"] = "Env User"
        os.environ["TACIT_VCS_EMAIL"] = "env@example.com"
        try:
            ident, _ = self._resolve()
            self.assertEqual(ident["source"], "environment")
        finally:
            os.environ.pop("TACIT_VCS_NAME", None)
            os.environ.pop("TACIT_VCS_EMAIL", None)

    def test_saved_settings_win_over_github(self):
        vcs.save_identity("Ada", "ada@example.com")
        ident, _ = self._resolve()
        self.assertEqual(ident["source"], "settings")
        self.assertEqual(ident["name"], "Ada")

    def test_the_placeholder_fallback_is_no_longer_hidden(self):
        import inspect
        from backend.routers import vcs as vcs_router
        self.assertNotIn('or "Tacit"', inspect.getsource(vcs_router))
        self.assertEqual(vcs.DEFAULT_IDENTITY["email"], "tacit@localhost")


class TestVersionControlPermission(Isolated):
    """Off for the agent by default, and the user can turn it on."""

    def setUp(self):
        super().setUp()
        config.save_prefs({"allowVersionControl": False})

    def test_off_by_default(self):
        config.save_prefs({"allowVersionControl": None})
        self.assertFalse(config.allow_vcs())

    def test_shell_tool_refuses_while_off(self):
        out = agent.t_run_shell("git status")
        self.assertIn("turned off", out)
        self.assertIn("Settings > Tools", out)

    def test_chained_commands_are_still_caught(self):
        self.assertIn("turned off", agent.t_run_shell("ls && git commit -m x"))
        self.assertIn("turned off", agent.t_run_shell("echo hi | git log"))

    def test_detection_is_unchanged_by_the_setting(self):
        """_blocked_shell stays a pure check; the gate is the preference."""
        self.assertTrue(agent._blocked_shell("git status"))
        config.save_prefs({"allowVersionControl": True})
        self.assertTrue(agent._blocked_shell("git status"))

    def test_the_setting_round_trips(self):
        self.assertFalse(config.allow_vcs())
        config.save_prefs({"allowVersionControl": True})
        self.assertTrue(config.allow_vcs())
        self.assertTrue(config.read_json(config.PREFS_FILE, {}).get("allowVersionControl"))

    def test_the_prompt_tracks_the_setting(self):
        from backend.ai import prompts

        config.save_prefs({"allowVersionControl": False})
        off = prompts.system_prompt(None, False, chat=False)
        self.assertIn("human action", off)

        config.save_prefs({"allowVersionControl": True})
        on = prompts.system_prompt(None, False, chat=False)
        self.assertIn("available to you", on)
        self.assertNotIn("human action", on)

    def test_the_prompt_stays_small_either_way(self):
        from backend.ai import prompts

        for allowed in (False, True):
            config.save_prefs({"allowVersionControl": allowed})
            size = len(prompts.system_prompt(None, False, chat=False))
            self.assertLess(size, 1200, f"prompt grew to {size} chars with allow={allowed}")


class TestSavedFolders(Isolated):
    """Folders that have been used are remembered, and can be forgotten."""

    BS = chr(92)

    def test_empty_to_begin_with(self):
        self.assertEqual(folders.list_folders(), [])

    def test_most_recent_first(self):
        folders.add("C:/Projects/one")
        folders.add("C:/Projects/two")
        self.assertEqual([r["name"] for r in folders.list_folders()], ["two", "one"])

    def test_re_adding_moves_up_without_duplicating(self):
        folders.add("C:/a")
        folders.add("C:/b")
        folders.add("C:/a")
        rows = folders.list_folders()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "a")

    def test_the_same_folder_in_another_style_is_the_same_folder(self):
        folders.add("C:/Projects/Tacit")
        folders.add("C:" + self.BS + "Projects" + self.BS + "Tacit" + self.BS)
        self.assertEqual(len(folders.list_folders()), 1, folders.list_folders())

    def test_remove(self):
        folders.add("C:/keep")
        folders.add("C:/drop")
        res = folders.remove("C:/drop")
        self.assertTrue(res["ok"])
        self.assertEqual([r["name"] for r in folders.list_folders()], ["keep"])

    def test_remove_accepts_a_different_separator_style(self):
        folders.add("C:/Projects/Tacit")
        res = folders.remove("C:" + self.BS + "Projects" + self.BS + "Tacit")
        self.assertTrue(res["ok"], res)
        self.assertEqual(folders.list_folders(), [])

    def test_removing_something_absent_is_reported(self):
        self.assertFalse(folders.remove("C:/never")["ok"])

    def test_blank_is_rejected(self):
        self.assertFalse(folders.add("   ")["ok"])
        self.assertEqual(folders.list_folders(), [])

    def test_the_list_is_capped(self):
        for i in range(folders.MAX + 8):
            folders.add(f"C:/x/{i}")
        self.assertEqual(len(folders.list_folders()), folders.MAX)

    def test_it_survives_a_reload(self):
        folders.add("C:/Projects/Tacit")
        on_disk = config.read_json(config.FOLDERS_FILE, [])
        self.assertEqual(len(on_disk), 1)
        self.assertEqual(folders.list_folders()[0]["path"].replace(chr(92), "/"),
                         "C:/Projects/Tacit")

    def test_endpoints(self):
        import asyncio
        from backend.routers import api

        class Req:
            def __init__(self, body):
                self._body = body

            async def json(self):
                return self._body

        added = asyncio.run(api.remember_folder(Req({"path": "C:/Projects/Tacit"})))
        self.assertTrue(added["ok"])
        self.assertTrue(asyncio.run(api.list_folders())["folders"])
        gone = asyncio.run(api.forget_folder(Req({"path": "C:/Projects/Tacit"})))
        self.assertTrue(gone["ok"])
        self.assertEqual(asyncio.run(api.list_folders())["folders"], [])


class TestAssistant(Isolated):
    """A per-session side conversation the main agent never sees."""

    def _rec(self):
        return {"id": "s1", "title": "Build a game", "project": "C:/x", "model": "p/m",
                "mode": "agent",
                "messages": [
                    {"role": "user", "content": "make snake"},
                    {"role": "assistant", "content": "done"},
                    {"role": "user", "content": "add score"},
                    {"role": "assistant", "content": "added"},
                ]}

    def test_defaults(self):
        cfg = assistant.settings_of({})
        self.assertTrue(cfg["include_user"])
        self.assertTrue(cfg["include_assistant"])
        self.assertFalse(cfg["tools"], "tool access must be opt-in")
        self.assertEqual(cfg["turns"], assistant.DEFAULTS["turns"])

    def test_settings_round_trip_and_clamp(self):
        rec = {}
        assistant.save_settings(rec, {"tools": True, "turns": 999})
        cfg = assistant.settings_of(rec)
        self.assertTrue(cfg["tools"])
        self.assertLessEqual(cfg["turns"], 200)
        assistant.save_settings(rec, {"turns": 0})
        self.assertGreaterEqual(assistant.settings_of(rec)["turns"], 1)

    def test_digest_respects_the_toggles(self):
        rec = self._rec()
        self.assertIn("make snake", assistant.digest(rec))
        self.assertIn("added", assistant.digest(rec))

        no_user = assistant.digest(rec, {**assistant.DEFAULTS, "include_user": False})
        self.assertNotIn("make snake", no_user)
        self.assertIn("added", no_user)

        no_reply = assistant.digest(rec, {**assistant.DEFAULTS, "include_assistant": False})
        self.assertIn("make snake", no_reply)
        self.assertNotIn("added", no_reply)

        no_meta = assistant.digest(rec, {**assistant.DEFAULTS, "include_meta": False})
        self.assertNotIn("[session]", no_meta)

    def test_digest_respects_the_turn_limit(self):
        rec = self._rec()
        one = assistant.digest(rec, {**assistant.DEFAULTS, "turns": 1})
        self.assertIn("add score", one)
        self.assertNotIn("make snake", one)

    def test_preview_costs_it_out(self):
        rec = self._rec()
        cheap = assistant.preview(rec)
        rich = assistant.preview(rec, {**assistant.DEFAULTS, "tools": True})
        self.assertEqual(cheap["tokens"]["tools"], 0)
        self.assertGreater(rich["tokens"]["tools"], 0)
        self.assertGreater(rich["tokens"]["total"], cheap["tokens"]["total"])
        self.assertEqual(cheap["turns"], 2)

    def test_model_defaults_to_the_session(self):
        rec = self._rec()
        self.assertEqual(assistant.settings_of(rec)["model"], "")
        self.assertEqual(assistant.resolve_model(rec), "p/m")
        self.assertEqual(assistant.preview(rec)["resolved_model"], "p/m")

    def test_a_different_model_can_be_chosen(self):
        rec = self._rec()
        assistant.save_settings(rec, {"model": "other/cheap"})
        self.assertEqual(assistant.resolve_model(rec), "other/cheap")
        self.assertEqual(assistant.preview(rec)["resolved_model"], "other/cheap")
        # and switching back to "same as session" restores the fallback
        assistant.save_settings(rec, {"model": ""})
        self.assertEqual(assistant.resolve_model(rec), "p/m")

    def test_thinking_follows_the_session_until_overridden(self):
        rec = self._rec()
        rec["thinking"] = "low"
        self.assertEqual(assistant.resolve_thinking(rec), "low")
        assistant.save_settings(rec, {"thinking": "high"})
        self.assertEqual(assistant.resolve_thinking(rec), "high")
        self.assertEqual(assistant.preview(rec)["resolved_thinking"], "high")

    def test_model_and_thinking_do_not_affect_the_token_cost(self):
        rec = self._rec()
        cheap = assistant.preview(rec)["tokens"]["total"]
        assistant.save_settings(rec, {"model": "other/cheap", "thinking": "max"})
        self.assertEqual(assistant.preview(rec)["tokens"]["total"], cheap)

    def test_tools_are_read_only(self):
        names = {t["function"]["name"] for t in assistant.read_tools()}
        for blocked in ("write_file", "edit_file", "run_shell", "restore", "bg_start"):
            self.assertNotIn(blocked, names)
        self.assertIn("read_file", names)

    def test_a_write_tool_is_refused_even_if_asked(self):
        out = assistant._run_read_tool("write_file", {"path": "x", "content": "y"}, self._rec())
        self.assertIn("read-only", out)

    def test_the_agent_never_sees_the_assistant(self):
        from backend.routers import chat

        rec = self._rec()
        assistant.append(rec, "user", "SECRET ASSISTANT NOTE")
        assistant.append(rec, "assistant", "SECRET ASSISTANT REPLY")

        history = chat._history(rec)
        blob = " ".join(m.get("content") or "" for m in history)
        self.assertNotIn("SECRET", blob)
        # ...while the assistant can still read the session itself
        self.assertIn("make snake", assistant.digest(rec))

    def test_clear_only_removes_the_side_conversation(self):
        rec = self._rec()
        assistant.append(rec, "user", "hi")
        assistant.clear(rec)
        self.assertEqual(rec.get("assistant"), [])
        self.assertEqual(len(rec["messages"]), 4, "the session itself must be untouched")

    def test_it_lives_in_the_session_record(self):
        rec = self._rec()
        assistant.append(rec, "user", "remember me")
        self.assertIn("assistant", rec)
        self.assertEqual(rec["assistant"][0]["content"], "remember me")


class TestSessionSummaries(Isolated):
    """Long-term memory: a whole session becomes one recallable note."""

    def setUp(self):
        super().setUp()
        from backend.routers import memory as mem
        self.mem = mem
        self._orig_chat = mem.engine.chat
        mem.engine.chat = lambda messages, ref=None: (
            "Goal: add a flag\nDid: added it\nDecided: kept it simple\nOpen: none")
        self.rec = store.create(title="Add a flag")
        store.append(self.rec, "user", "please add a --verbose flag")
        store.append(self.rec, "assistant", "added it to main.py")
        store.save(self.rec)

    def tearDown(self):
        self.mem.engine.chat = self._orig_chat
        super().tearDown()

    class Req:
        def __init__(self, body):
            self._b = body

        async def json(self):
            return self._b

    def _call(self, body):
        import asyncio
        res = asyncio.run(self.mem.summarise(self.Req(body)))
        return json.loads(res.body) if hasattr(res, "body") else res

    def test_a_preview_saves_nothing(self):
        from backend import memory_store as ms
        got = self._call({"sid": self.rec["id"]})
        self.assertTrue(got["ok"])
        self.assertFalse(got["saved"])
        self.assertIn("Goal", got["summary"])
        self.assertGreater(got["tokens"], 0)
        self.assertEqual(ms.list_memories(), [], "a preview must not write")

    def test_saving_carries_the_session_it_came_from(self):
        from backend import memory_store as ms
        got = self._call({"sid": self.rec["id"], "save": True})
        self.assertTrue(got["saved"])
        rows = ms.list_memories()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_session"], f"session:{self.rec['id']}")
        self.assertIn("session summary", rows[0]["reason"])
        self.assertIn("Goal", rows[0]["content"])

    def test_a_supplied_summary_is_not_regenerated(self):
        got = self._call({"sid": self.rec["id"], "text": "my own words"})
        self.assertEqual(got["summary"], "my own words")

    def test_an_unknown_session_is_refused(self):
        for bad in ("", "nope"):
            got = self._call({"sid": bad})
            self.assertFalse(got["ok"])

    def test_a_session_with_no_content_is_refused(self):
        empty = store.create(title="Empty")
        self.assertFalse(self._call({"sid": empty["id"]})["ok"])

    def test_a_summary_can_expire(self):
        from backend import memory_store as ms
        got = self._call({"sid": self.rec["id"], "save": True, "ttl_days": 7})
        self.assertTrue(got["saved"])
        self.assertGreater(ms.get(got["memory"]["id"])["expires_at"], 0)


class TestSessionCreationRule(Isolated):
    """A session exists because the user asked for one, never because a page loaded.

    The interface opens a socket on every page load, and that socket used to
    create a session for whatever id it was handed. One reload could therefore
    add a session, and a sync could add every local one at once.
    """

    def test_a_sync_never_creates_an_unknown_session(self):
        store.merge({"sessions": [{"id": "never-seen", "title": "ghost",
                                   "messages": [{"role": "user", "content": "hi"}]}]})
        self.assertEqual(store.index(), [])
        self.assertIsNone(store.get("never-seen"))

    def test_a_sync_still_updates_a_known_session(self):
        rec = store.create(title="real")
        store.merge({"sessions": [{"id": rec["id"], "title": "renamed",
                                   "messages": [{"role": "user", "content": "hello"}]}]})
        after = store.get(rec["id"])
        self.assertEqual(after["title"], "renamed")
        self.assertEqual(len(after["messages"]), 1)

    def test_a_sync_cannot_inflate_the_list(self):
        before = len(store.index())
        store.merge({"sessions": [{"id": f"ghost-{i}", "title": "g"} for i in range(40)]})
        self.assertEqual(len(store.index()), before)

    def test_an_empty_registry_creates_nothing(self):
        reg = store.registry()
        self.assertEqual(reg["sessions"], [])
        self.assertEqual(reg["active"], "")
        self.assertEqual(store.index(), [])

    def test_a_client_may_name_the_id_it_asked_for(self):
        rec = store.create(title="mine", sid="chosen-id")
        self.assertEqual(rec["id"], "chosen-id")
        self.assertIsNotNone(store.get("chosen-id"))

    def test_creating_the_same_id_twice_is_harmless(self):
        first = store.create(title="one", sid="dup")
        second = store.create(title="two", sid="dup")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["title"], "one")   # the original is kept
        self.assertEqual(len(store.index()), 1)

    def test_the_socket_refuses_an_unknown_id(self):
        """Only an explicit create request may bring a session into being."""
        from pathlib import Path as _P
        src = (_P(__file__).parent.parent / "backend/routers/chat.py").read_text(
            encoding="utf-8")
        self.assertIn('q.get("create") != "1"', src)
        self.assertIn("unknown session", src)
        # the old unconditional creation must be gone
        self.assertNotIn('rec = store.create(title=q.get("name") or "New session",\n'
                         '                           model=q.get("model") or "",',
                         src.split('q.get("create")')[0].split("rec = store.get(sid)")[-1])

    def test_the_browser_only_asks_for_one_when_told_to(self):
        from pathlib import Path as _P
        js = (_P(__file__).parent.parent / "static/app.js").read_text(encoding="utf-8")
        self.assertIn("s._new ? '&create=1' : ''", js)
        # no boot-time creation may remain
        self.assertNotIn("if (!sessions.length) newSession(true)", js)

    def test_storage_paths_follow_the_config(self):
        """A frozen path at import time is how the index escaped its directory."""
        from pathlib import Path as _P
        src = (_P(__file__).parent.parent / "backend/store.py").read_text(encoding="utf-8")
        self.assertIn("def _index_path", src)
        self.assertNotIn("INDEX = config.", src)
        moved = config.SESSIONS_DIR
        self.assertEqual(store._index_path(), moved / "index.json")


if __name__ == "__main__":
    unittest.main()

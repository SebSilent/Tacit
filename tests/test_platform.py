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
        for name in ("minimal", "default", "safe", "power-isolation",
                     "power-memory", "full"):
            self.assertIn(name, rows)
        self.assertNotIn("dsh", rows, "no profile is named after another harness")
        self.assertNotIn("hermes", rows, "no profile is named after another harness")
        self.assertTrue(rows["default"]["active"])
        self.assertFalse(rows["default"]["mcp_direct"])

    def test_legacy_names_still_resolve(self):
        for old, new in profiles.LEGACY_NAMES.items():
            self.assertIn(new, {p["name"] for p in profiles.list_profiles()})
            config.write_json(config.PROFILES_FILE, {"active": old, "profiles": {}})
            self.assertEqual(profiles.load()["active"], new)
            self.assertTrue(profiles.apply(old)["ok"])

    def test_minimal_narrows_the_tool_set(self):
        rows = {p["name"]: p for p in profiles.list_profiles()}
        self.assertEqual(rows["minimal"]["tool_count"], len(profiles.CORE_TOOLS))
        self.assertGreater(rows["default"]["tool_count"], rows["minimal"]["tool_count"])
        self.assertLess(rows["minimal"]["cost"]["total"], rows["default"]["cost"]["total"])

    def test_cost_ordering(self):
        rows = {p["name"]: p["cost"]["total"] for p in profiles.list_profiles()}
        self.assertLess(rows["minimal"], rows["default"])
        self.assertLessEqual(rows["default"], rows["full"])

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
        profiles.apply("default")
        self.assertFalse(plugin_manager.is_enabled("memory_vault"))
        self.assertEqual(providers.load()["memory"]["mode"], "off")

    def test_applying_a_profile_is_recorded(self):
        from backend import audit
        profiles.apply("safe")
        self.assertIn("profile_applied", [e["event"] for e in audit.recent(5)])

    def test_apply_changes_plugin_state(self):
        self.assertTrue(profiles.apply("safe")["ok"])
        self.assertTrue(plugin_manager.is_enabled("memory_vault"))
        self.assertTrue(profiles.apply("default")["ok"])
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
        profiles.apply("default")
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
        # create=1 is gated. It rides on a session the New-session button just
        # made, and on the single re-attach after the server answered 4404. It
        # must never ride an ordinary connection, or any stale id could put a
        # row in the store.
        self.assertIn("(s._new || s._adopt) ? '&create=1' : ''", js)
        # adoption is one-shot per session, so a refusal cannot re-create the
        # same row on every retry
        self.assertIn("if (!s._adopted)", js)
        self.assertIn("s._adopted = true", js)
        # and the second refusal stops instead of reconnecting forever
        self.assertIn("could not be restored", js)
        # a real disconnect must still retry: the server restarts constantly
        self.assertIn("disconnected \u2014 retrying", js)
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


class TestTurnHistory(Isolated):
    """A turn's tool calls must survive into the next turn's context.

    They used to be thrown away: a whole multi-step turn was flattened into one
    string of narration, so on the next turn the agent could not see anything it
    had done and would begin the same work again.
    """

    def _scripted(self):
        first = [
            {"type": "text", "delta": "first I look"},
            {"type": "reason", "delta": "let me inspect"},
            {"type": "tool_calls", "calls": [
                {"id": "a", "name": "list_files", "arguments": '{"path": "."}'}]},
            {"type": "done", "model": None},
        ]
        second = [
            {"type": "text", "delta": "now I am done"},
            {"type": "done", "finish": "stop", "model": None},
        ]
        turns = iter([first, second])
        return lambda *a, **k: iter(next(turns))

    def _trace(self):
        from backend import agent
        orig = agent.engine.stream_chat
        trace = []
        agent.engine.stream_chat = self._scripted()
        try:
            list(agent.run_turn([{"role": "user", "content": "go"}], project=None,
                                max_steps=3, trace=trace))
        finally:
            agent.engine.stream_chat = orig
        return trace

    def test_each_step_is_recorded_separately(self):
        trace = self._trace()
        self.assertEqual(len(trace), 2, "the two steps were merged")
        self.assertEqual(trace[0]["text"], "first I look")
        self.assertEqual(trace[0]["reason"], "let me inspect")
        self.assertEqual([t["name"] for t in trace[0]["tools"]], ["list_files"])
        self.assertEqual(trace[1]["text"], "now I am done")
        self.assertEqual(trace[1]["tools"], [])

    def test_no_single_message_holds_two_steps_of_narration(self):
        """The old bug stored one message per turn, so two steps shared a string.

        Checked on the stored transcript, not by re-joining it by hand.
        """
        trace = self._trace()
        rec = store.create(title="glue")
        store.append(rec, "user", "go")
        for s in trace:
            store.append(rec, "assistant", s["text"])
        store.save(rec)
        bodies = [m["content"] for m in store.get(rec["id"])["messages"]
                  if m["role"] == "assistant"]
        self.assertEqual(len(bodies), 2)
        for b in bodies:
            self.assertFalse("first I look" in b and "now I am done" in b,
                             f"two steps landed in one message: {b!r}")

    def test_the_call_and_its_result_both_reach_the_transcript(self):
        from backend.routers import chat as ch
        trace = self._trace()
        rec = store.create(title="hist")
        store.append(rec, "user", "go")
        for s in trace:
            extra = {}
            if s["reason"]:
                extra["reason"] = s["reason"]
            if s["tools"]:
                extra["tools"] = s["tools"]
            store.append(rec, "assistant", s["text"], extra or None)
        store.save(rec)
        hist = ch._history(store.get(rec["id"]))
        calls = [c for h in hist if h["role"] == "assistant"
                 for c in (h.get("tool_calls") or [])]
        results = [h for h in hist if h["role"] == "tool"]
        self.assertEqual(len(calls), 1)
        self.assertEqual([r["tool_call_id"] for r in results], [c["id"] for c in calls])

    def test_history_rebuilds_the_protocol_shape(self):
        from backend.routers import chat as ch
        rec = store.create(title="rebuild")
        store.append(rec, "user", "explore this")
        store.append(rec, "assistant", "looking", {"tools": [
            {"id": "c1", "name": "list_files", "args": {"path": "."},
             "result": "FILE a.py", "is_error": False},
            {"id": "c2", "name": "grep_files", "args": {"pattern": "x"},
             "result": "a.py:1", "is_error": True}]})
        store.save(rec)
        hist = ch._history(store.get(rec["id"]))
        asst = next(h for h in hist if h["role"] == "assistant")
        self.assertEqual(len(asst["tool_calls"]), 2)
        self.assertEqual(json.loads(asst["tool_calls"][0]["function"]["arguments"]),
                         {"path": "."})
        tools = [h for h in hist if h["role"] == "tool"]
        self.assertEqual([t["tool_call_id"] for t in tools], ["c1", "c2"])
        self.assertEqual(tools[0]["content"], "FILE a.py")

    def test_a_silent_step_that_ran_tools_is_not_dropped(self):
        """Silent work is still work, and the agent must be able to remember it."""
        from backend.routers import chat as ch
        rec = store.create(title="silent")
        store.append(rec, "user", "go")
        store.append(rec, "assistant", "", {"tools": [
            {"id": "s1", "name": "list_files", "args": {}, "result": "FILE a",
             "is_error": False}]})
        store.save(rec)
        hist = ch._history(store.get(rec["id"]))
        self.assertEqual(len([h for h in hist if h["role"] == "tool"]), 1)

    def test_a_call_is_never_emitted_without_its_result(self):
        """An unanswered call makes the API reject the whole request."""
        from backend.routers import chat as ch
        rec = store.create(title="pair")
        store.append(rec, "user", "go")
        store.append(rec, "assistant", "x", {"tools": [{"name": "list_files", "args": {}}]})
        store.save(rec)
        hist = ch._history(store.get(rec["id"]))
        ids = [c["id"] for h in hist if h["role"] == "assistant"
               for c in (h.get("tool_calls") or [])]
        answers = [t["tool_call_id"] for t in hist if t["role"] == "tool"]
        self.assertEqual(ids, answers)

    def test_the_sync_path_does_not_strip_the_history(self):
        from backend.routers import chat as ch
        rows = [{"role": "assistant", "content": "looked", "reason": "because",
                 "tools": [{"id": "z", "name": "list_files", "args": {},
                            "result": "FILE a", "is_error": False}]}]
        kept = ch._clean_history(rows)
        self.assertEqual(kept[0]["tools"][0]["id"], "z")
        self.assertEqual(kept[0]["reason"], "because")


class TestGuidance(Isolated):
    """Little-coder style behaviour: act, and be told the thing you need when you
    need it, instead of carrying a prompt that grows for every edge case."""

    def test_a_plan_is_recognised_as_a_plan(self):
        from backend import guidance
        self.assertTrue(guidance.plan_like("I'll start by exploring the project structure."))
        self.assertTrue(guidance.plan_like("Let me check the README first."))
        self.assertTrue(guidance.plan_like("I will now look at the tests."))

    def test_an_answer_is_not_mistaken_for_a_plan(self):
        from backend import guidance
        self.assertFalse(guidance.plan_like("There are 32 modules in backend/."))
        self.assertFalse(guidance.plan_like("Done. The fix is in chat.py."))
        self.assertFalse(guidance.plan_like("```\nimport os\n```"))
        self.assertFalse(guidance.plan_like("- read_file\n- edit_file"))
        self.assertFalse(guidance.plan_like("This route returned an error and I explained why."))
        self.assertFalse(guidance.plan_like(""))
        self.assertFalse(guidance.plan_like("x" * 500))

    def test_failure_outranks_everything_else(self):
        from backend import guidance
        b = guidance.for_step(step=0, steps=24, errors=[("edit_file", "no match")],
                              repeated=["read_file"], first=True, project=True)
        self.assertIn("edit_file", b)
        self.assertIn("The last step's tool call failed", b)

    def test_priority_is_error_then_repeat_then_budget_then_discovery(self):
        from backend import guidance
        self.assertIn("already ran", guidance.for_step(step=0, steps=24,
                                                       repeated=["read_file"], first=True,
                                                       project=True))
        self.assertIn("step(s) left", guidance.for_step(step=22, steps=24, first=True,
                                                        project=True))
        self.assertIn("project's own instructions", guidance.for_step(step=0, steps=24,
                                                                      first=True, project=True))
        self.assertEqual(guidance.for_step(step=5, steps=24), "")

    def test_a_block_is_not_repeated_while_it_still_applies(self):
        from backend import guidance
        first = guidance.for_step(step=22, steps=24)
        self.assertTrue(first)
        self.assertEqual(guidance.for_step(step=22, steps=24, previous=first), "",
                         "the same block was billed twice in a row")

    def test_blocks_stay_short(self):
        from backend import guidance
        cases = [
            guidance.for_step(step=0, steps=24, errors=[("run_shell", "x") * 4]),
            guidance.for_step(step=0, steps=24, repeated=["a"] * 30),
            guidance.for_step(step=0, steps=24, first=True, project=True),
            guidance.nudge(), guidance.closing(),
        ]
        for c in cases:
            self.assertLessEqual(len(c), guidance.MAX_BLOCK)

    def test_the_standing_prompt_never_carries_guidance(self):
        """The whole point: the cached prompt stays tiny, 975 chars on Windows.

        The number is Windows-only because the platform line changes per OS, and the Windows
        wording is the longest of the three. So Windows pins the exact size and every other lane
        asserts it is under that, which is what actually matters: no lane may grow the standing
        prompt past the size we measured and shipped.
        """
        from backend import config, guidance
        from backend.ai import prompts
        p = prompts.system_prompt("", False, chat=False)
        if config.OS == "Windows":
            self.assertEqual(len(p), 975)
        else:
            self.assertLess(len(p), 975,
                            f"{config.OS} prompt is {len(p)} chars, no shorter than Windows")
        for frag in ("Do it now", "project's own instructions", "step(s) left",
                     "Out of steps"):
            self.assertNotIn(frag, p)
        src = (Path("backend/agent.py")).read_text(encoding="utf-8")
        self.assertIn("ctx_msgs = ctx_msgs + ", src,
                      "guidance must be appended per step, not merged into the prompt")

    def test_a_planning_step_is_pushed_back_into_action(self):
        from backend import agent
        orig = agent.engine.stream_chat
        turns = iter([
            [{"type": "text", "delta": "I'll start by exploring the project structure."},
             {"type": "done", "model": None}],
            [{"type": "text", "delta": "There are 32 modules."},
             {"type": "done", "model": None}],
        ])
        agent.engine.stream_chat = lambda *a, **k: iter(next(turns))
        trace, events = [], []
        try:
            events = list(agent.run_turn([{"role": "user", "content": "go"}],
                                         project=None, max_steps=5, trace=trace))
        finally:
            agent.engine.stream_chat = orig
        self.assertIn("planning detected", json.dumps(events))
        self.assertEqual(len(trace), 2)
        self.assertEqual(trace[1]["text"], "There are 32 modules.")

    def test_the_nudge_is_not_stored_as_something_the_user_said(self):
        from backend import agent
        from backend.routers import chat as ch
        orig = agent.engine.stream_chat
        turns = iter([
            [{"type": "text", "delta": "I'll start by exploring."},
             {"type": "done", "model": None}],
            [{"type": "text", "delta": "Found 32 modules."},
             {"type": "done", "model": None}],
        ])
        agent.engine.stream_chat = lambda *a, **k: iter(next(turns))
        rec = store.create(title="nudge")
        store.append(rec, "user", "go")
        try:
            list(agent.run_turn([{"role": "user", "content": "go"}], project=None,
                                max_steps=5, trace=[]))
        finally:
            agent.engine.stream_chat = orig
        store.save(rec)
        roles = [m["role"] for m in ch._history(store.get(rec["id"]))]
        self.assertEqual(roles.count("user"), 1,
                         "the harness nudge leaked into the user's voice")

    def test_running_out_of_steps_still_produces_an_answer(self):
        """Before, the turn ended on a tool result and the user got nothing."""
        from backend import agent
        orig = agent.engine.stream_chat
        seen = {"tools": 0, "none": 0}

        def fake(*a, **k):
            if k.get("tools") is None:
                seen["none"] += 1
                return iter([{"type": "text", "delta": "Ran out. I changed two files."},
                             {"type": "done", "model": None}])
            seen["tools"] += 1
            n = seen["tools"]
            return iter([
                {"type": "text", "delta": "working"},
                {"type": "tool_calls", "calls": [
                    {"id": f"c{n}", "name": "read_file", "arguments": '{"path": "x"}'}]},
                {"type": "done", "model": None}])

        agent.engine.stream_chat = fake
        trace = []
        try:
            list(agent.run_turn([{"role": "user", "content": "go"}], project=None,
                                max_steps=2, trace=trace))
        finally:
            agent.engine.stream_chat = orig
        self.assertEqual(seen["none"], 1, "no closing call was made")
        self.assertEqual(trace[-1]["text"], "Ran out. I changed two files.")


    def test_discovery_is_offered_once_per_task_not_once_per_turn(self):
        from backend import agent
        seen = []
        orig = agent.engine.stream_chat
        turns = iter([[{"type": "text", "delta": "found it"}, {"type": "done", "model": None}]] * 6)

        def fake(msgs, **k):
            seen.append([m for m in msgs if m.get("role") == "system"
                         and "project's own instructions" in (m.get("content") or "")])
            return iter(next(turns))

        agent.engine.stream_chat = fake
        try:
            msgs = [{"role": "system", "content": "base"}, {"role": "user", "content": "go"}]
            list(agent.run_turn(msgs, project=r"C:\proj", max_steps=24))
            msgs.append({"role": "assistant", "content": "found it"})
            msgs.append({"role": "user", "content": "and now?"})
            list(agent.run_turn(msgs, project=r"C:\proj", max_steps=24))
        finally:
            agent.engine.stream_chat = orig
        self.assertEqual(len(seen[0]), 1, "the first turn never got the discovery note")
        self.assertEqual(len(seen[1]), 0, "discovery was repeated on a later turn")


    def test_a_turn_that_stops_talking_still_leaves_an_answer(self):
        """The 24-step live probe ended on a tool result with no final text."""
        from backend import agent
        orig = agent.engine.stream_chat
        n = {"tools": 0, "none": 0}

        def fake(*a, **k):
            if k.get("tools") is None:
                n["none"] += 1
                return iter([{"type": "text", "delta": "Read 4 files. No changes made."},
                             {"type": "done", "model": None}])
            n["tools"] += 1
            if n["tools"] == 1:
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "c1", "name": "read_file", "arguments": '{"path": "x"}'}]},
                    {"type": "done", "model": None}])
            return iter([{"type": "done", "finish": "stop", "model": None}])

        agent.engine.stream_chat = fake
        trace = []
        try:
            list(agent.run_turn([{"role": "user", "content": "go"}], project=None,
                                max_steps=6, trace=trace))
        finally:
            agent.engine.stream_chat = orig
        self.assertEqual(n["none"], 1, "a silent ending produced no closing call")
        self.assertTrue(trace, "nothing was recorded")
        self.assertTrue(trace[-1]["text"], "the turn ended with no answer to read")

    def test_a_failing_closing_call_says_so(self):
        from backend import agent
        from backend.ai import engine as eng
        orig = agent.engine.stream_chat
        orig_err = eng.EngineError

        def fake(*a, **k):
            if k.get("tools") is None:
                raise eng.EngineError("context too long")
            return iter([{"type": "tool_calls", "calls": [
                {"id": "c1", "name": "read_file", "arguments": '{"path": "x"}'}]},
                {"type": "done", "model": None}])

        agent.engine.stream_chat = fake
        try:
            events = list(agent.run_turn([{"role": "user", "content": "go"}], project=None,
                                         max_steps=1))
        finally:
            agent.engine.stream_chat = orig
        errs = [e for e in events if e.get("type") == "error"]
        self.assertTrue(errs, "the failure was swallowed and the turn just looked short")
        self.assertIn("no closing report", errs[0]["message"])


    def test_an_empty_closing_call_still_leaves_something_to_read(self):
        """The live probe returned no text from its closing call. Blank is not ok."""
        from backend import agent
        orig = agent.engine.stream_chat
        n = {"tools": 0, "none": 0}

        def fake(*a, **k):
            if k.get("tools") is None:
                n["none"] += 1
                return iter([{"type": "done", "finish": "stop", "model": None}])
            n["tools"] += 1
            return iter([{"type": "tool_calls", "calls": [
                {"id": f"c{n['tools']}", "name": "read_file",
                 "arguments": '{"path": "x"}'}]},
                {"type": "done", "model": None}])

        agent.engine.stream_chat = fake
        trace = []
        try:
            list(agent.run_turn([{"role": "user", "content": "go"}], project=None,
                                max_steps=2, trace=trace))
        finally:
            agent.engine.stream_chat = orig
        self.assertTrue(n["none"], "no closing call was made")
        self.assertTrue(trace[-1]["text"], "the turn ended blank again")
        self.assertIn("tool calls", trace[-1]["text"])


class TestThinkingLevels(Isolated):
    """The thinking control has to reach the provider or it is decoration.

    Measured on the live endpoint: every named effort produced 1.5k to 1.8k chars of
    reasoning while 'none' produced zero, so a level that is never sent leaves a
    model that thinks by default thinking away, whatever the UI says.
    """

    def test_off_maps_to_a_value_that_actually_disables(self):
        from backend import config
        self.assertEqual(config.reasoning_for("off"), "none")
        self.assertIsNone(config.reasoning_for(""))

    def test_named_levels_are_passed_through_unchanged(self):
        from backend import config
        for lvl in ("minimal", "low", "medium", "high", "xhigh", "max"):
            self.assertEqual(config.reasoning_for(lvl), lvl,
                             f"{lvl} was renamed before the provider could map it")

    def _payload(self, engine, reasoning_flag, effort):
        model = {"model": "m", "maxTokens": 100, "reasoning": reasoning_flag}
        return engine._payload(model, [{"role": "user", "content": "x"}], False,
                               None, None, None, effort)

    def test_a_reasoning_model_gets_its_effort(self):
        from backend.ai import engine
        body = self._payload(engine, True, "max")
        self.assertEqual(body.get("reasoning_effort"), "max")

    def test_off_is_sent_even_when_the_model_is_not_marked_reasoning(self):
        """The flag is a claim about capability. Turning thinking off cannot require it."""
        from backend.ai import engine
        body = self._payload(engine, False, "none")
        self.assertEqual(body.get("reasoning_effort"), "none")

    def test_an_unmarked_model_is_not_asked_to_grade_its_thinking(self):
        from backend.ai import engine
        body = self._payload(engine, False, "high")
        self.assertNotIn("reasoning_effort", body)

    def test_the_tools_loop_sends_the_chain_of_thought_back(self):
        """With tools attached the reasoning of earlier turns has to travel too."""
        from backend import agent
        orig = agent.engine.stream_chat
        sent = []
        turns = iter([
            [{"type": "reason", "delta": "I should list files first"},
             {"type": "tool_calls", "calls": [
                 {"id": "a", "name": "list_files", "arguments": '{}'}]},
             {"type": "done", "model": None}],
            [{"type": "text", "delta": "two files"}, {"type": "done", "model": None}],
        ])

        def fake(msgs, **k):
            sent.append(msgs)
            return iter(next(turns))

        agent.engine.stream_chat = fake
        try:
            list(agent.run_turn([{"role": "user", "content": "go"}], project=None,
                                max_steps=4))
        finally:
            agent.engine.stream_chat = orig
        first = sent[1]
        asst = next(m for m in first if m["role"] == "assistant" and m.get("tool_calls"))
        self.assertEqual(asst.get("reasoning_content"), "I should list files first")

    def test_stored_reasoning_is_replayed_only_for_a_thinking_model(self):
        rows = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": "looked", "reason": "because",
                 "tools": [{"id": "c1", "name": "list_files", "args": {},
                            "result": "FILE a", "is_error": False}]}]
        plain = __import__("backend.agent", fromlist=["x"]).transcript_messages(rows)
        replayed = __import__("backend.agent", fromlist=["x"]).transcript_messages(
            rows, reasoning=True)
        a_plain = next(m for m in plain if m["role"] == "assistant")
        a_rep = next(m for m in replayed if m["role"] == "assistant")
        self.assertNotIn("reasoning_content", a_plain)
        self.assertEqual(a_rep["reasoning_content"], "because")


    def test_an_unsupported_name_is_clamped_to_one_the_model_lists(self):
        """ollama resolves a name a model does not list to that model's default,
        silently. deepseek-v4.1-flash lists low/high/max and nothing else."""
        from backend import thinking
        model = {"reasoning": True, "thinkingValues": [False, "low", "high", "max"]}
        self.assertEqual(thinking.send(model, "max"), "max")
        self.assertEqual(thinking.send(model, "high"), "high")
        self.assertEqual(thinking.send(model, "low"), "low")
        self.assertIn(thinking.send(model, "medium"), ("low", "high"))
        self.assertNotIn(thinking.send(model, "xhigh"), ("xhigh", None),
                         "xhigh is not in this model's list and would become its default")

    def test_a_model_without_max_is_not_asked_for_max(self):
        from backend import thinking
        model = {"thinkingValues": ["low", "medium", "high"]}
        self.assertEqual(thinking.send(model, "max"), "high")

    def test_a_boolean_switch_is_asked_to_think_not_renamed(self):
        """Values [false, true] means on or off, and there is no ladder to clamp onto.

        This used to assert that "medium" was passed straight through. That is what broke:
        measured on ollama_cloud/kimi-k2.7-code, thinking="medium" returned 297 reasoning
        characters while sending nothing at all returned 399 - the endpoint takes a word it
        does not support and thinks less. On a boolean switch, any level above off means on.
        """
        from backend import thinking
        model = {"thinkingValues": [False, True]}
        self.assertIs(thinking.send(model, "medium"), True)
        self.assertIs(thinking.send(model, "high"), True)
        self.assertEqual(thinking.send(model, "none"), "none")

    def test_off_is_reported_as_unavailable_when_the_model_has_no_off(self):
        from backend import thinking
        self.assertFalse(thinking.supports_off({"thinkingValues": ["low", "high", "max"]}))
        self.assertTrue(thinking.supports_off({"thinkingValues": [False, "low"]}))
        self.assertTrue(thinking.supports_off({}), "never probed: ask anyway")

    def test_the_discovery_is_cached_on_the_model(self):
        """resolve_model must carry the probed values through, or the clamp has
        nothing to read. Written here rather than read from a real models.json,
        because this fixture runs with a temporary home."""
        from backend import config
        path = config.MODELS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"default": "p/m", "providers": {"p": {
            "baseUrl": "https://example.invalid/v1", "apiKey": "k", "models": [
                {"id": "m", "reasoning": True,
                 "thinkingValues": [False, "low", "high", "max"],
                 "thinkingDefault": "high"}]}}}, indent=2), encoding="utf-8")
        model = config.resolve_model("p/m")
        self.assertIn("thinkingValues", model)
        self.assertIn("low", model["thinkingValues"])
        self.assertEqual(model["thinkingDefault"], "high")
        from backend import thinking
        self.assertEqual(thinking.send(model, "xhigh"), "high")

    def test_nothing_invented_when_a_model_was_never_probed(self):
        from backend import thinking
        self.assertEqual(thinking.send({"reasoning": True}, "high"), "high")
        self.assertIsNone(thinking.send({"reasoning": True}, None))


class TestThinkingDiscovery(Isolated):
    """Levels are measured, not assumed. Each provider shape is simulated here so the
    measuring logic is tested without a network."""

    def _patch(self, thinking, table):
        orig = thinking._ask
        thinking._ask = lambda ref, effort, tokens=1500: table.get(
            effort or "__none__", {"ok": False, "error": "simulated down"})
        self.addCleanup(setattr, thinking, "_ask", orig)

    def test_a_graded_provider_is_offered_a_ladder(self):
        from backend import thinking
        self._patch(thinking, {
            "__none__": {"ok": True, "reasoning": 700, "content": 40},
            "none": {"ok": True, "reasoning": 0, "content": 30},
            "low": {"ok": True, "reasoning": 300, "content": 40},
            "high": {"ok": True, "reasoning": 900, "content": 40},
        })
        found = thinking.detect({"baseUrl": "https://api.example.invalid/v1"})
        self.assertTrue(found["ok"])
        self.assertEqual(found["source"], "probe")
        self.assertTrue(found["graded"])
        self.assertIn(False, found["values"])
        self.assertIn("low", found["values"])
        self.assertEqual(thinking.levels({"thinkingValues": found["values"]}),
                         ["default", "off", "low", "high"])

    def test_a_provider_that_accepts_words_and_ignores_them_gets_no_ladder(self):
        """This is the old bug in provider form: identical traces for every name."""
        from backend import thinking
        same = {"ok": True, "reasoning": 512, "content": 20}
        self._patch(thinking, {"__none__": same, "low": same, "high": same,
                               "none": {"ok": True, "reasoning": 0, "content": 20}})
        found = thinking.detect({"baseUrl": "https://api.example.invalid/v1"})
        self.assertFalse(found["graded"], "equal traces were called a ladder")
        self.assertEqual(found["values"], [True, False],
                         "names that did nothing were still offered as levels")
        self.assertEqual(thinking.levels({"thinkingValues": found["values"]}),
                         ["default", "off", "on"],
                         "a boolean switch offers on and off, and nothing else")

    def test_a_provider_that_rejects_the_parameter_offers_only_default(self):
        from backend import thinking
        self._patch(thinking, {"__none__": {"ok": True, "reasoning": 0, "content": 12},
                               "low": {"ok": False, "status": 400, "error": "unknown field"},
                               "high": {"ok": False, "status": 400, "error": "unknown field"},
                               "none": {"ok": False, "status": 400, "error": "unknown field"}})
        found = thinking.detect({"baseUrl": "https://api.example.invalid/v1"})
        self.assertTrue(found["ok"])          # a refusal is an answer
        self.assertEqual(found["values"], [])
        self.assertEqual(thinking.levels({"thinkingValues": [], "thinkingSource": "probe"}),
                         ["default"], "only 'default' is honest here")

    def test_a_model_that_thinks_unasked_is_recorded_as_thinking(self):
        from backend import thinking
        self._patch(thinking, {
            "__none__": {"ok": True, "reasoning": 900, "content": 20},
            "none": {"ok": True, "reasoning": 0, "content": 20},
            "low": {"ok": True, "reasoning": 400, "content": 20},
            "high": {"ok": True, "reasoning": 1000, "content": 20}})
        found = thinking.detect({"baseUrl": "https://api.example.invalid/v1"})
        self.assertIn(True, found["values"], "thinking by default was lost")

    def test_an_unreachable_model_fails_loudly(self):
        from backend import thinking
        self._patch(thinking, {})
        found = thinking.detect({"baseUrl": "https://api.example.invalid/v1"})
        self.assertFalse(found["ok"])

    def test_no_published_list_and_probe_declined_costs_nothing(self):
        """Bulk import must not spend requests per model."""
        from backend import thinking
        self._patch(thinking, {"__none__": {"ok": True, "reasoning": 5, "content": 5}})
        found = thinking.detect({"baseUrl": "https://api.example.invalid/v1"}, probe=False)
        self.assertFalse(found["ok"])
        self.assertIn("probing was declined", found["error"])

    def test_detection_is_written_once_and_read_back(self):
        from backend import config, thinking
        path = config.MODELS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"default": "p/m", "providers": {"p": {
            "baseUrl": "https://api.example.invalid/v1", "apiKey": "k", "models": [
                {"id": "m"}]}}}, indent=2), encoding="utf-8")
        self.assertFalse(thinking.known("p/m"))
        self._patch(thinking, {"__none__": {"ok": True, "reasoning": 800, "content": 9},
                               "none": {"ok": True, "reasoning": 0, "content": 9},
                               "low": {"ok": True, "reasoning": 200, "content": 9},
                               "high": {"ok": True, "reasoning": 900, "content": 9}})
        first = thinking.ensure("p/m")
        self.assertTrue(first["ok"]) and self.assertFalse(first["cached"])
        again = thinking.ensure("p/m")
        self.assertTrue(again["cached"], "a cached model was measured twice")
        self.assertEqual(thinking.levels("p/m"), ["default", "off", "low", "high"])
        # a saved preference naming a rung the model lacks is clamped, not forwarded
        self.assertEqual(thinking.send("p/m", "xhigh"), "high")


    def test_the_whole_record_survives_the_registry(self):
        """resolve_model projects a small list of keys. A field it drops is a field
        no caller can read, which is how 'published' looked like nothing at all."""
        from backend import config, thinking
        path = config.MODELS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"default": "p/m", "providers": {"p": {
            "baseUrl": "https://ollama.com/v1", "apiKey": "k", "models": [
                {"id": "m", "reasoning": True, "thinkingValues": [False, "low", "max"],
                 "thinkingDefault": "low", "thinkingSource": "show",
                 "thinkingGraded": True}]}}}, indent=2), encoding="utf-8")
        m = thinking.meta("p/m")
        self.assertEqual(m["source"], "show")
        self.assertTrue(m["graded"])
        self.assertEqual(m["default"], "low")
        self.assertTrue(m["known"])


class TestSwitchHandler(Isolated):
    """A settings message names the session it belongs to, over a real socket.

    switch used to apply model/mode/thinking to whichever session was open when the
    target id could not be found, so a stale or mistyped id quietly reconfigured a
    different conversation. There was no websocket test anywhere, so nothing could
    notice; this builds one.
    """

    def _client(self):
        from fastapi.testclient import TestClient
        from backend.main import app
        return TestClient(app)

    def _until(self, ws, want, limit=10):
        for _ in range(limit):
            msg = ws.receive_json()
            if msg.get("type") == want:
                return msg
        self.fail("never saw a " + want + " message")

    def test_an_unknown_target_leaves_the_open_session_alone(self):
        keep = "ollama_cloud/deepseek-v4.1-flash"
        rec = store.create(title="mine", model=keep, mode="agent", thinking="high")
        store.save(rec)
        with self._client() as c:
            with c.websocket_connect("/ws/" + rec["id"]) as ws:
                self._until(ws, "session_ready")
                ws.send_json({"type": "switch", "sid": "no-such-session",
                              "model": "evil/model", "mode": "chat",
                              "thinking": "off"})
                # Take the single reply and look at it, rather than scanning for the
                # message I expect. Waiting for a message that a broken build never
                # sends hangs the test instead of failing it.
                reply = ws.receive_json()
        self.assertEqual(reply.get("type"), "error", str(reply)[:120])
        self.assertIn("unknown session", reply.get("message", ""))
        after = store.get(rec["id"])
        self.assertEqual(after["model"], keep)
        self.assertEqual(after["mode"], "agent")
        self.assertEqual(after["thinking"], "high")

    def test_a_real_target_is_the_one_that_gets_the_settings(self):
        a = store.create(title="a", model="m/a", thinking="high")
        b = store.create(title="b", model="m/b", thinking="high")
        store.save(a)
        store.save(b)
        with self._client() as c:
            with c.websocket_connect("/ws/" + a["id"]) as ws:
                self._until(ws, "session_ready")
                ws.send_json({"type": "switch", "sid": b["id"], "thinking": "off"})
                self._until(ws, "hello")
        self.assertEqual(store.get(b["id"])["thinking"], "off")
        self.assertEqual(store.get(a["id"])["thinking"], "high",
                         "the session left behind was the one edited")


class TestRepeatGuard(Isolated):
    """A turn that re-runs one probe is not investigating, it is looping.

    Measured on the live benchmark: a cell issued 59 shell calls with 10 distinct argument
    sets, 33k output tokens, 1,606 chars of text, and never wrote the file the task named.
    The old behaviour was a set membership test plus an advice line that was suppressed
    whenever it repeated itself, so the model was told once and then left alone 80 times.
    """

    def _loop(self, agent, name, args, runs, calls=None, steps=8):
        """Drive a turn that keeps issuing the same call, counting real executions."""
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        turns = iter([[{"type": "tool_calls", "calls": [
            {"id": f"c{i}", "name": name, "arguments": args}]},
            {"type": "done", "model": None}] for i in range(steps)]
            + [[{"type": "text", "delta": "done"}, {"type": "done", "model": None}]])

        def fake_stream(msgs, **k):
            # The turn now ends early when it is stuck, so the closing rounds get asked too.
            # Answering those with another probe would put a third execution in `runs`.
            ask = next((m for m in reversed(msgs) if m.get("role") == "user"), {})
            if "Out of steps" in str(ask.get("content") or ""):
                return iter([{"type": "text", "delta": "reported"},
                             {"type": "done", "model": None}])
            return iter(next(turns))

        def fake_tool(n, a, ctx, out):
            runs.append(n)
            out["result"] = (calls or {}).get(n, "the same answer")
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            msgs = [{"role": "system", "content": "base"},
                    {"role": "user", "content": "investigate it"}]
            list(agent.run_turn(msgs, project=None, max_steps=steps))
            return msgs
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool

    def test_the_third_identical_call_is_answered_from_cache(self):
        from backend import agent
        runs = []
        msgs = self._loop(agent, "run_shell", '{"command": "python3 -q probe.py"}', runs=runs)
        self.assertEqual(runs, ["run_shell", "run_shell"],
                         "the same call should run twice, then be answered from cache")
        blocked = [m for m in msgs if m.get("role") == "tool"
                   and "BLOCKED" in str(m.get("content"))]
        self.assertTrue(blocked, "a blocked repeat must say so in the tool result")
        self.assertIn("the same answer", blocked[0]["content"],
                      "the model must get its previous result back, not a refusal")

    def test_a_call_that_failed_last_time_is_free_to_run_again(self):
        """Retrying after an error is debugging. Blocking it would break legitimate work."""
        from backend import agent
        runs = []
        self._loop(agent, "run_shell", '{"command": "make"}', runs=runs,
                   calls={"run_shell": "ERROR: command failed"}, steps=6)
        self.assertEqual(len(runs), 6,
                         "a failing call must not be served from cache")

    def test_repeats_that_differ_are_not_blocked(self):
        from backend import agent
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        runs = []
        turns = iter([[{"type": "tool_calls", "calls": [
            {"id": f"c{i}", "name": "run_shell",
             "arguments": '{"command": "ls dir' + str(i) + '"}'}]},
            {"type": "done", "model": None}] for i in range(6)]
            + [[{"type": "text", "delta": "done"}, {"type": "done", "model": None}]])

        def fake_stream(msgs, **k):
            return iter(next(turns))

        def fake_tool(n, a, ctx, out):
            runs.append(a)
            out["result"] = "ok"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            msgs = [{"role": "system", "content": "base"},
                    {"role": "user", "content": "look around"}]
            list(agent.run_turn(msgs, project=None, max_steps=6))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        self.assertEqual(len(runs), 6, "distinct arguments were treated as repeats")

    def test_a_blocked_repeat_names_the_file_that_is_still_missing(self):
        """The whole point of the guard: stop the loop AND say what would finish the task."""
        from backend import agent
        import tempfile
        runs = []
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        turns = iter([[{"type": "tool_calls", "calls": [
            {"id": f"c{i}", "name": "run_shell",
             "arguments": '{"command": "python3 probe.py"}'}]},
            {"type": "done", "model": None}] for i in range(5)]
            + [[{"type": "text", "delta": "done"}, {"type": "done", "model": None}]])

        def fake_stream(msgs, **k):
            ask = str(next((m for m in reversed(msgs) if m.get("role") == "user"), {})
                      .get("content") or "")
            if "Out of steps" in ask:
                return iter([{"type": "text", "delta": "done"}, {"type": "done", "model": None}])
            return iter(next(turns))

        def fake_tool(n, a, ctx, out):
            runs.append(n)
            out["result"] = "0x80808080 -> 0x02000002"
            return iter([])
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            with tempfile.TemporaryDirectory() as d:
                msgs = [{"role": "system", "content": "base"},
                        {"role": "user", "content": f"find the differential and write attack.py "
                                                    f"in {d}"}]
                list(agent.run_turn(msgs, project=d, max_steps=5))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        blocked = [m for m in msgs if m.get("role") == "tool" and "BLOCKED" in str(m.get("content"))]
        self.assertTrue(blocked)
        self.assertIn("attack.py", blocked[0]["content"],
                      "a blocked call with no deliverable must point at the deliverable")
        self.assertIn("0x80808080", blocked[0]["content"],
                      "and it must still hand back the previous result")

    def test_a_blocked_repeat_stays_short_after_the_first_time(self):
        """Echoing the whole payload into every blocked call is its own kind of waste."""
        from backend import agent
        runs = []
        blob = "RESULT-BODY-" + ("x" * 3000)
        msgs = self._loop(agent, "run_shell", '{"command": "x"}', runs=runs, steps=7,
                          calls={"run_shell": blob})
        blocked = [str(m.get("content")) for m in msgs if m.get("role") == "tool"
                   and "BLOCKED" in str(m.get("content"))]
        self.assertTrue(len(blocked) >= 2, "no repeat was blocked at all")
        self.assertIn("RESULT-BODY-", blocked[0],
                      "the first block must show the result it is refusing to re-run")
        self.assertNotIn("RESULT-BODY-", blocked[-1],
                         "a later block re-echoing the payload would cost the saving")
        self.assertLess(len(blocked[-1]), len(blocked[0]))

    def test_mutating_tools_are_never_served_from_cache(self):
        """A repeated write_file may be a recovery, and silently skipping a mutation that
        was asked for would make the harness lie about what it did to the project."""
        from backend import agent
        runs = []
        self._loop(agent, "write_file", '{"path": "a.txt", "content": "x"}', runs=runs, steps=5)
        self.assertEqual(len(runs), 5, "a blocked mutation would be an unreported omission")


class TestDeliverables(Isolated):
    """The thing being graded is a file, so its absence is worth detecting."""

    def test_named_files_that_exist_are_inputs_not_deliverables(self):
        import tempfile
        from backend import agent
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "requests_bucket_1.jsonl").write_text("{}", encoding="utf-8")
            got = agent.deliverables(
                "Read requests_bucket_1.jsonl and write attack.py, then emit plan_b1.jsonl", d)
            self.assertEqual(got, ["attack.py", "plan_b1.jsonl"],
                             "inputs already on disk must not be nagged about")

    def test_paths_in_the_text_are_reduced_to_a_bare_name(self):
        from backend import agent
        got = agent.deliverables("create src/deep/attack.py", None)
        self.assertEqual(got, ["src/deep/attack.py"])

    def test_no_file_names_means_no_nagging(self):
        from backend import agent
        self.assertEqual(agent.deliverables("explain what this cipher does", None), [])

    def test_the_missing_file_outranks_the_repeat_advice_near_the_cap(self):
        from backend import guidance
        text = guidance.for_step(step=28, steps=30, errors=(), repeated=("run_shell",),
                                 missing=("attack.py",))
        self.assertIn("attack.py", text)
        self.assertNotIn("already ran", text,
                         "a cell that never wrote the file is graded on the file, not the loop")

    def test_a_failure_still_outranks_the_missing_file(self):
        from backend import guidance
        text = guidance.for_step(step=28, steps=30, errors=(("edit_file", "ERROR: no match"),),
                                 missing=("attack.py",))
        self.assertIn("edit_file", text)

    def test_the_nudge_arrives_once_and_not_early(self):
        from backend import agent
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        blocks = []
        turns = iter([[{"type": "tool_calls", "calls": [
            {"id": f"c{i}", "name": "run_shell",
             "arguments": '{"command": "probe step' + str(i) + '"}'}]},
            {"type": "done", "model": None}] for i in range(6)]
            + [[{"type": "text", "delta": "done"}, {"type": "done", "model": None}]])

        def fake_stream(msgs, **k):
            if "Out of steps" in str(next((m for m in reversed(msgs)
                                           if m.get("role") == "user"), {}).get("content") or ""):
                return iter([{"type": "text", "delta": "done"}, {"type": "done", "model": None}])
            blocks.append([m.get("content") or "" for m in msgs
                           if m.get("role") == "system" and "named in the task" in str(m)])
            return iter(next(turns))

        def fake_tool(n, a, ctx, out):
            out["result"] = "ok"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            msgs = [{"role": "system", "content": "base"},
                    {"role": "user", "content": "write attack.py and run probes"}]
            list(agent.run_turn(msgs, project=None, max_steps=6))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        fired = [i for i, b in enumerate(blocks) if b]
        self.assertTrue(fired, "the missing deliverable was never mentioned")
        self.assertGreaterEqual(fired[0], 3,
                               "nagging from step one wastes the budget it is trying to save")


class TestHeadlessReasoning(Isolated):
    """headless has to send the thinking level, or a benchmark row pins nothing."""

    def _capture(self, **kw):
        import tempfile
        from backend import headless
        orig = headless.agent.engine.stream_chat
        seen = []

        def fake(msgs, **k):
            seen.append(k)
            return iter([{"type": "text", "delta": "done"},
                         {"type": "usage", "usage": {"input_tokens": 10,
                                                     "output_tokens": 2,
                                                     "total_tokens": 12}},
                         {"type": "done", "model": None}])

        headless.agent.engine.stream_chat = fake
        try:
            with tempfile.TemporaryDirectory() as d:
                out = headless.run("say done", d, "ollama_cloud/kimi-k2.7-code",
                                   max_steps=2, **kw)
        finally:
            headless.agent.engine.stream_chat = orig
        return seen, out

    def test_a_pinned_level_reaches_the_request(self):
        seen, out = self._capture(reasoning="medium")
        self.assertEqual(out["reasoning"], "medium")
        self.assertEqual(out["reasoning_effort"], "medium")
        self.assertEqual(seen[0]["reasoning_effort"], "medium",
                         "the level was resolved in headless but never forwarded")

    def test_off_actually_turns_thinking_off(self):
        seen, out = self._capture(reasoning="off")
        self.assertEqual(seen[0]["reasoning_effort"], "none",
                         "'off' that sends nothing leaves a default-thinking model thinking")

    def test_default_sends_nothing_and_says_so(self):
        seen, out = self._capture()
        self.assertEqual(out["reasoning"], "default")
        self.assertIsNone(out["reasoning_effort"])
        self.assertIsNone(seen[0]["reasoning_effort"],
                          "'default' must not smuggle a level into the request")

    def test_the_cli_flag_exists_for_benchmark_runners(self):
        from backend import headless
        ap = None
        for action in _actions(headless):
            if action.dest == "reasoning":
                ap = action
        self.assertIsNotNone(ap, "--reasoning is not on the headless parser")


def _actions(module):
    import argparse
    from backend import headless
    parser = None
    orig_parse = argparse.ArgumentParser.parse_args

    def grab(self, *a, **k):
        nonlocal parser
        parser = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = grab
    try:
        try:
            headless.main(["--instruction", "x"])
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = orig_parse
    return list(parser._actions) if parser else []


class TestStuckTurnExit(Isolated):
    """The guard has to change outcomes, not only costs.

    From the fixed Linux arm: cells that solved did it in 14 to 25 rounds, while the cells that
    failed issued up to 26 refused repeats and stopped at the cap with the named file still absent.
    A refused repeat is nearly free now, so a turn can afford to notice it is stuck: end early and
    spend the last round writing the deliverable, with only the tools that can produce it.
    """

    def _drive(self, agent, instruction, steps=12):
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        offered, runs = [], []

        def fake_stream(msgs, **k):
            offered.append([t["function"]["name"] for t in (k.get("tools") or [])])
            ask = str(next((m for m in reversed(msgs) if m.get("role") == "user"), {})
                      .get("content") or "")
            if "Write it now with write_file" in ask:
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "w1", "name": "write_file",
                     "arguments": '{"path": "attack.py", "content": "def attack(): pass"}'}]},
                    {"type": "done", "model": None}])
            if "Answer now without using any more tools" in ask:
                return iter([{"type": "text", "delta": "reported"},
                             {"type": "done", "model": None}])
            return iter([{"type": "tool_calls", "calls": [
                {"id": "p", "name": "run_shell",
                 "arguments": '{"command": "python3 probe.py"}'}]},
                {"type": "done", "model": None}])

        def fake_tool(n, a, ctx, out):
            runs.append((n, str(a.get("path") or "")))
            out["result"] = "same answer" if n == "run_shell" else "wrote attack.py"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            msgs = [{"role": "system", "content": "base"},
                    {"role": "user", "content": instruction}]
            events = list(agent.run_turn(msgs, project=None, max_steps=steps))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        return msgs, events, offered, runs

    def test_a_stuck_turn_ends_well_before_the_cap(self):
        from backend import agent
        _m, events, _o, _r = self._drive(agent, "Write attack.py that recovers the key.")
        notes = [e["message"] for e in events if e["type"] == "notify"]
        self.assertTrue(any("refused in a row" in n for n in notes), notes)
        self.assertTrue(any(n.startswith("stopped after 6 steps") for n in notes),
                        "two runs plus four refusals is the end of the story, not twelve rounds")

    def test_the_last_round_writes_the_named_file(self):
        from backend import agent
        _m, _e, offered, runs = self._drive(agent, "Write attack.py that recovers the key.")
        self.assertIn(("write_file", "attack.py"), runs,
                      "a named deliverable must be written before the turn ends")
        phase = [names for names in offered
                 if names and set(names) <= set(agent.SALVAGE_TOOLS)]
        self.assertTrue(phase, "no deliverable phase ran")
        self.assertLessEqual(len(phase), agent.SALVAGE_ROUNDS,
                             "the phase is bounded, or one turn quietly becomes two")
        wrote = runs.index(("write_file", "attack.py"))
        self.assertTrue([n for n, _p in runs[wrote + 1:] if n == "run_shell"],
                        "the file has to be RUN before the phase counts itself done")
        self.assertEqual(len(phase), 2,
                         "one round to write it, one to run it - not every round")

    def test_the_deliverable_phase_keeps_going_until_the_file_exists(self):
        """Writing it is step one. The rounds after that are where a wrong file gets fixed."""
        from backend import agent
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        asked = []

        def fake_stream(msgs, **k):
            ask = str(next((m for m in reversed(msgs) if m.get("role") == "user"), {})
                      .get("content") or "")
            asked.append(ask)
            if "Write it now with write_file" in ask:
                return iter([{"type": "tool_calls", "calls": [
                    {"id": "w", "name": "run_shell",
                     "arguments": '{"command": "python3 attack.py"}'}]},
                    {"type": "done", "model": None}])
            if "Answer now without using any more tools" in ask:
                return iter([{"type": "text", "delta": "reported"},
                             {"type": "done", "model": None}])
            return iter([{"type": "tool_calls", "calls": [
                {"id": "p", "name": "run_shell", "arguments": '{"command": "python3 p.py"}'}]},
                {"type": "done", "model": None}])

        def fake_tool(n, a, ctx, out):
            out["result"] = "ran, no output"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            msgs = [{"role": "system", "content": "base"},
                    {"role": "user", "content": "Write attack.py that recovers the key."}]
            list(agent.run_turn(msgs, project=None, max_steps=12))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        self.assertEqual(sum(1 for a in asked if "Write it now with write_file" in a),
                         agent.SALVAGE_ROUNDS,
                         "a file that never lands should get every bounded round, not one")

    def test_nothing_is_salvaged_when_the_task_named_no_file(self):
        """An investigation task has no deliverable, so it must not get a round of invention."""
        from backend import agent
        _m, _e, offered, runs = self._drive(agent, "Find out why the suite is red.")
        self.assertEqual([n for n in offered
                          if n and set(n) <= set(agent.SALVAGE_TOOLS)], [])
        self.assertNotIn("write_file", [n for n, _p in runs])

    def test_a_turn_that_stops_early_still_delivers_the_named_file(self):
        """The worst hard-set cells ended on their own — 13 turns, 45,000 output tokens,
        no attack.py. Waiting for the step cap never reached them, so this exit had to
        grow the same deliverable phase."""
        from backend import agent
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        offered, wrote = [], []
        calls = {"n": 0}

        def fake_stream(msgs, **k):
            offered.append([t["function"]["name"] for t in (k.get("tools") or [])])
            calls["n"] += 1
            if calls["n"] == 1:
                return iter([{"type": "text", "delta": "Let me look at the cipher. " * 20},
                             {"type": "tool_calls", "calls": [
                                 {"id": "a", "name": "run_shell",
                                  "arguments": '{"command": "python3 -c 1"}'}]},
                             {"type": "done", "model": None}])
            if calls["n"] == 2:
                # Narration and no tool call: this used to end the turn immediately.
                return iter([{"type": "text", "delta": "Here is my analysis of FEAL."},
                             {"type": "done", "model": None}])
            return iter([{"type": "tool_calls", "calls": [
                {"id": "w", "name": "write_file",
                 "arguments": '{"path": "attack.py", "content": "def attack(f):\\n'
                              '    return 1\\n"}'}]},
                {"type": "done", "model": None}])

        def fake_tool(n, a, ctx, out):
            if n == "write_file":
                wrote.append(str(a.get("path") or ""))
                out["result"] = f"wrote {a.get('path')}"
            else:
                out["result"] = "1"
            return iter([])

        agent.engine.stream_chat, agent.call_tool = fake_stream, fake_tool
        try:
            msgs = [{"role": "system", "content": "base"},
                    {"role": "user",
                     "content": "Write attack.py that recovers the key."}]
            list(agent.run_turn(msgs, project=None, max_steps=12))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        self.assertIn("attack.py", wrote, "it narrated an answer and left nothing on disk")
        self.assertTrue([n for n in offered
                         if n and set(n) <= set(agent.SALVAGE_TOOLS)],
                        "the phase that writes the file must be the one that ran")

    def test_the_writes_are_recorded_in_the_transcript(self):
        """A file written after the cap still has to appear in the history the next turn reads."""
        from backend import agent
        msgs, _e, _o, _r = self._drive(agent, "Write attack.py that recovers the key.")
        self.assertTrue([m for m in msgs if m.get("role") == "tool"
                         and "wrote attack.py" in str(m.get("content"))])


if __name__ == "__main__":
    unittest.main()

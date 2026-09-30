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

from backend import (agent, config, mcp_registry, memory_store, metrics,  # noqa: E402
                     plugin_manager, tokens)

FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"

_PATCHED = ("HOME", "MEMORY_DB", "MCP_FILE", "PLUGINS_FILE", "PLUGINS_USER_DIR",
            "ADAPTERS_DIR", "CHECKPOINT_DIR", "PREFS_FILE", "GITHUB_FILE")


class Isolated(unittest.TestCase):
    """Points every storage path at a temporary home."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        self._orig = {key: getattr(config, key) for key in _PATCHED}
        config.HOME = home
        config.MEMORY_DB = home / "memory.db"
        config.MCP_FILE = home / "mcp.json"
        config.PLUGINS_FILE = home / "plugins.json"
        config.PLUGINS_USER_DIR = home / "plugins"
        config.ADAPTERS_DIR = home / "adapters"
        config.CHECKPOINT_DIR = home / "checkpoints"
        # prefs must be redirected too, or a test that changes a preference
        # would rewrite the user's real ~/.tacit/prefs.json
        config.PREFS_FILE = home / "prefs.json"
        config.GITHUB_FILE = home / "github.json"
        if hasattr(config, "SETTINGS_FILE"):
            config.SETTINGS_FILE = home / "settings.json"
        config.PLUGINS_USER_DIR.mkdir(parents=True, exist_ok=True)
        config.ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
        config.CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        mcp_registry.stop_all()
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
        self.assertIn("dsh_bridge", rows)
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


class TestDshBridge(Isolated):
    def test_import_registers_a_disabled_server(self):
        from backend.plugins import dsh_bridge
        out = dsh_bridge.call("dsh_import", {"package": "@scope/some-mcp-thing"}, {})
        self.assertIn("registered", out)
        servers = mcp_registry.list_servers()
        self.assertEqual(len(servers), 1)
        self.assertFalse(servers[0]["enabled"])       # never auto-enabled

    def test_status_classifies(self):
        from backend.plugins import dsh_bridge
        dsh_bridge.call("dsh_import", {"package": "some-tool"}, {})
        text = dsh_bridge.call("dsh_status", {}, {})
        self.assertIn("some-tool", text)
        self.assertTrue(any(word in text for word in
                            ("working_native", "working_bridge", "partial", "unsupported")))

    def test_scaffold_writes_a_usable_adapter(self):
        from backend.plugins import dsh_bridge
        out = dsh_bridge.call("dsh_scaffold", {"plugin_id": "myplugin"}, {})
        self.assertIn("scaffolded", out)
        target = config.ADAPTERS_DIR / "myplugin"
        for name in ("README.md", "manifest.json", "server.py"):
            self.assertTrue((target / name).exists(), name)
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "myplugin")

    def test_scaffolded_server_speaks_mcp(self):
        """The generated stub must really answer initialize + tools/list."""
        import subprocess
        from backend.plugins import dsh_bridge
        dsh_bridge.call("dsh_scaffold", {"plugin_id": "speaks"}, {})
        server = config.ADAPTERS_DIR / "speaks" / "server.py"
        script = (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
                  + "\n"
                  + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
        proc = subprocess.run([sys.executable, str(server)], input=script,
                              capture_output=True, text=True, timeout=60)
        lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        self.assertTrue(any((m.get("result") or {}).get("serverInfo", {}).get("name") == "speaks"
                            for m in lines), proc.stdout + proc.stderr)
        listed = [m for m in lines
                  if isinstance(m.get("result"), dict) and "tools" in m["result"]]
        self.assertTrue(listed and listed[0]["result"]["tools"][0]["name"])


class TestNodeBridge(unittest.TestCase):
    def test_bridge_ships(self):
        bridge = config.BRIDGES_DIR / "dsh-host.mjs"
        self.assertTrue(bridge.exists(), "bridges/dsh-host.mjs should ship with Tacit")
        text = bridge.read_text(encoding="utf-8")
        for marker in ("initialize", "tools/list", "tools/call", "protocolVersion"):
            self.assertIn(marker, text)


class TestNodeBridgeRuntime(unittest.TestCase):
    """Runs only when Node happens to be installed — it is never a dependency."""

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_bridge_exposes_bundle_tools_over_mcp(self):
        import subprocess
        bridge = config.BRIDGES_DIR / "dsh-host.mjs"
        bundle = Path(__file__).parent / "fake_dsh_bundle.mjs"
        script = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "greet", "arguments": {"who": "Tacit"}}}),
        ]) + "\n"
        proc = subprocess.run(["node", str(bridge), "--plugin", str(bundle)],
                              input=script, capture_output=True, text=True, timeout=90)
        replies = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        listed = [m for m in replies if isinstance(m.get("result"), dict)
                  and "tools" in m["result"]]
        self.assertTrue(listed, proc.stderr)
        self.assertEqual(sorted(t["name"] for t in listed[0]["result"]["tools"]),
                         ["greet", "tally"])
        called = [m for m in replies if isinstance(m.get("result"), dict)
                  and "content" in m["result"]]
        self.assertTrue(called)
        self.assertEqual(called[0]["result"]["content"][0]["text"], "hello Tacit")


if __name__ == "__main__":
    unittest.main()

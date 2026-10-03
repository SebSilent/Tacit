"""Regression tests for the frontend/backend contracts that were drifting.

These lock the wire shapes the browser reads (and the guardrails around the
shell tool) so the fixes cannot silently regress. No test spawns a real
version-control process: the git layer is stubbed where it is exercised.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import agent, config, hosting, skills, vcs  # noqa: E402
from backend.ai import engine  # noqa: E402
from backend.routers import api, chat  # noqa: E402


class Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class TestShellGuard(unittest.TestCase):
    """The agent must not reach version control from a shell."""

    def test_first_token(self):
        self.assertTrue(agent._blocked_shell("git status"))

    def test_chained(self):
        self.assertTrue(agent._blocked_shell("echo hi && git commit -m x"))

    def test_pipe(self):
        self.assertTrue(agent._blocked_shell("ls | git log"))

    def test_semicolon_and_gh(self):
        self.assertTrue(agent._blocked_shell("echo go; gh pr list"))

    def test_absolute_path(self):
        self.assertTrue(agent._blocked_shell("/usr/bin/git status"))

    def test_wrapper(self):
        self.assertTrue(agent._blocked_shell("sudo git reset --hard"))

    def test_assignment_prefix(self):
        self.assertTrue(agent._blocked_shell("GIT_PAGER=cat git log"))

    def test_exe_suffix(self):
        self.assertTrue(agent._blocked_shell("git.exe status"))

    def test_allowed_commands(self):
        for cmd in ("ls -la", "python -m pytest", "grep -r todo .",
                    "echo git", "find . -name '*.txt'"):
            with self.subTest(cmd=cmd):
                self.assertFalse(agent._blocked_shell(cmd))


class TestEditAndContinue(unittest.TestCase):
    """truncate_from: the client rewinds to a user turn and resends it."""

    def test_clean_history_strips_client_only_fields(self):
        incoming = [
            {"role": "user", "content": "hi", "ts": 1.0},
            {"role": "assistant", "content": "hello",
             "tools": [{"huge": "x" * 100}], "reason": "why"},
            {"role": "bogus", "content": "ignore me"},
            "not-a-dict",
        ]
        clean = chat._clean_history(incoming)
        self.assertEqual([m["role"] for m in clean], ["user", "assistant"])
        for m in clean:
            self.assertLessEqual(set(m), {"role", "content", "ts", "reason", "tools"})
        self.assertEqual(clean[0]["ts"], 1.0)
        # The junk tool entry has no call id, so it cannot be paired with a result
        # and is dropped. reason is a real field and survives.
        self.assertNotIn("tools", clean[1])
        self.assertEqual(clean[1]["reason"], "why")

    def test_clean_history_keeps_a_real_call_but_clips_it(self):
        big = "y" * 40000
        incoming = [{"role": "assistant", "content": "ran it",
                     "tools": [{"id": "c1", "name": "read_file",
                                "args": {"path": "a.py"}, "result": big,
                                "is_error": False, "junk": {"a": 1}}]}]
        kept = chat._clean_history(incoming)[0]["tools"][0]
        self.assertEqual(kept["id"], "c1")
        self.assertEqual(kept["args"], {"path": "a.py"})
        self.assertNotIn("junk", kept)
        self.assertLessEqual(len(kept["result"]), 6000)

    def _cut(self, msgs, turn):
        seen = -1
        for i, m in enumerate(msgs):
            if m.get("role") == "user":
                seen += 1
                if seen == turn:
                    return i
        return None

    def test_rewind_to_second_user_turn(self):
        msgs = [{"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": "c"},
                {"role": "assistant", "content": "d"}]
        self.assertEqual(self._cut(msgs, 0), 0)
        self.assertEqual(self._cut(msgs, 1), 2)
        self.assertEqual(msgs[:self._cut(msgs, 1)],
                         [{"role": "user", "content": "a"},
                          {"role": "assistant", "content": "b"}])

    def test_missing_turn_is_not_found(self):
        self.assertIsNone(self._cut([{"role": "assistant", "content": "x"}], 0))


class TestSessionStats(unittest.TestCase):
    """get_session_stats must carry the fields app.js reads."""

    def setUp(self):
        # Patched and restored. Left in place it leaked into every later test in the
        # session: config.resolve_model stayed a one-key lambda for the rest of the
        # run, which reads as an unrelated failure in another file.
        orig = chat.config.resolve_model
        self.addCleanup(setattr, chat.config, "resolve_model", orig)
        chat.config.resolve_model = lambda ref=None: {"contextWindow": 100000}

    def test_shape(self):
        rec = {"id": "s1", "model": "p/m", "tool_calls": 3,
               "messages": [{"role": "user", "content": "hello there"}],
               "usage": {"tokens": {"input": 10, "output": 5, "total": 15},
                         "context": {"tokens": 15, "window": 100000, "percent": 0.0}}}
        d = chat._stats_payload(rec)
        self.assertEqual(d["totalMessages"], 1)
        self.assertEqual(d["toolCalls"], 3)
        self.assertEqual(d["tokens"]["total"], 15)
        self.assertEqual(d["contextUsage"]["contextWindow"], 100000)
        self.assertIsNotNone(d["contextUsage"]["percent"])


class TestMeta(unittest.TestCase):
    """hello/get_state must report a live turn so the client can resume it."""

    def setUp(self):
        orig = chat.config.resolve_model
        self.addCleanup(setattr, chat.config, "resolve_model", orig)
        chat.config.resolve_model = lambda ref=None: {"contextWindow": 1000}
        self.rec = {"id": "s1", "model": "p/m", "messages": [], "title": "T"}

    def test_idle(self):
        m = chat._meta(self.rec, None)
        self.assertFalse(m["busy"])
        self.assertFalse(m["starting"])

    def test_starting(self):
        m = chat._meta(self.rec, {"busy": True, "starting": True})
        self.assertTrue(m["busy"])
        self.assertTrue(m["starting"])

    def test_midstream_is_busy_not_starting(self):
        m = chat._meta(self.rec, {"busy": True, "starting": False})
        self.assertTrue(m["busy"])
        self.assertFalse(m["starting"])


class TestSkills(unittest.TestCase):
    """The skills panel reads s.body and a separate knowledge list."""

    def test_split_and_body(self):
        orig = skills.load
        skills.load = lambda: [
            {"name": "fmt", "description": "formatting", "path": "/s/fmt.md",
             "body": "# Steps", "kind": "skill", "tokens": 5},
            {"name": "posix", "description": "shell notes", "path": "/k/posix.md",
             "body": "# posix", "kind": "knowledge", "tokens": 3},
        ]
        try:
            d = asyncio.run(api.skills_list())
        finally:
            skills.load = orig
        self.assertEqual([s["name"] for s in d["user_skills"]], ["fmt"])
        self.assertEqual([s["name"] for s in d["knowledge"]], ["posix"])
        self.assertEqual(d["user_skills"][0]["body"], "# Steps")


class TestAddModel(unittest.TestCase):
    """A manually added model must keep its context window."""

    def test_context_window_persisted(self):
        orig_reg, orig_save = config.registry, config.save_registry
        box = {"reg": {"default": "", "providers": {"p": {"baseUrl": "http://x", "models": []}}}}
        config.registry = lambda: box["reg"]
        config.save_registry = lambda r: box.__setitem__("reg", r)
        try:
            r = asyncio.run(api.add_model("p", Req(
                {"id": "m", "name": "M", "reasoning": True, "contextWindow": "131072"})))
        finally:
            config.registry, config.save_registry = orig_reg, orig_save
        self.assertTrue(r["ok"])
        m = box["reg"]["providers"]["p"]["models"][0]
        self.assertEqual(m["contextWindow"], 131072)
        self.assertEqual(m["maxTokens"], 0)


class TestProjectEnrichment(unittest.TestCase):
    """full=1 must add the isRepo/branch/github_full_name keys git.js reads."""

    def test_fields(self):
        orig_vc, orig_remotes = vcs.vc, vcs.remotes

        def fake_vc(cwd, args):
            if cwd == "/plain":
                return {"ok": False, "stdout": ""}
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return {"ok": True, "stdout": "/repo\n"}
            if args[:1] == ["status"]:
                return {"ok": True, "stdout": "## main...origin/main\n"}
            return {"ok": False, "stdout": ""}

        vcs.vc = fake_vc
        vcs.remotes = lambda root: [{"name": "origin",
                                     "url": "https://github.com/acme/widget.git"}]
        try:
            out = api._enrich_projects([
                {"name": "widget", "path": "/repo", "root": "/"},
                {"name": "plain", "path": "/plain", "root": "/"}])
        finally:
            vcs.vc, vcs.remotes = orig_vc, orig_remotes
        w = next(p for p in out if p["name"] == "widget")
        p = next(p for p in out if p["name"] == "plain")
        self.assertTrue(w["isRepo"])
        self.assertEqual(w["branch"], "main")
        self.assertEqual(w["github_full_name"], "acme/widget")
        self.assertFalse(p["isRepo"])
        self.assertEqual(p["branch"], "")
        self.assertEqual(p["github_full_name"], "")


class TestModelMetadata(unittest.TestCase):
    """Context windows are scraped from whatever field the endpoint uses."""

    def test_context_key_variants(self):
        for key, val in (("context_length", 128000), ("max_model_len", 8192),
                         ("contextWindow", "32768"), ("n_ctx", 4096)):
            with self.subTest(key=key):
                self.assertEqual(engine._meta_int({key: val}, engine._CONTEXT_KEYS), int(val))

    def test_nested_meta(self):
        self.assertEqual(
            engine._meta_int({"meta": {"n_ctx": "2048"}}, engine._CONTEXT_KEYS), 2048)

    def test_ignores_bool_and_garbage(self):
        self.assertEqual(engine._meta_int({"context_length": True}, engine._CONTEXT_KEYS), 0)
        self.assertEqual(engine._meta_int({"context_length": "abc"}, engine._CONTEXT_KEYS), 0)
        self.assertEqual(engine._meta_int({}, engine._CONTEXT_KEYS), 0)

    def test_remote_models_derives_ids(self):
        orig = engine.remote_model_details
        engine.remote_model_details = lambda base, key="": [
            {"id": "a", "contextWindow": 1, "maxTokens": 0},
            {"id": "b", "contextWindow": 0, "maxTokens": 0}]
        try:
            self.assertEqual(engine.remote_models("http://x"), ["a", "b"])
        finally:
            engine.remote_model_details = orig


class TestProbeShape(unittest.TestCase):
    """harness.js reads r.new and r.models from /probe."""

    def test_returns_new_and_models(self):
        orig_reg, orig_rm = config.registry, engine.remote_models
        config.registry = lambda: {"default": "", "providers": {
            "p": {"baseUrl": "http://x", "models": [{"id": "old"}]}}}
        engine.remote_models = lambda base, key="": ["old", "fresh1", "fresh2"]
        try:
            r = asyncio.run(api.probe_provider("p"))
        finally:
            config.registry, engine.remote_models = orig_reg, orig_rm
        self.assertTrue(r["ok"])
        self.assertEqual(r["models"], ["old", "fresh1", "fresh2"])
        self.assertEqual(r["new"], ["fresh1", "fresh2"])


class TestWorkspaceDiscovery(unittest.TestCase):
    """No assumed layout: a folder is a workspace only because it was chosen."""

    def test_project_roots_does_not_autoscan(self):
        orig_prefs = config.prefs
        orig_env = os.environ.pop("TACIT_PROJECTS_ROOTS", None)
        config.prefs = lambda: {}
        try:
            # no ~/Projects, ~/Dev, ~/Code guessing any more — just the home dir
            self.assertEqual(config.project_roots(), [str(config.USER_HOME)])
        finally:
            config.prefs = orig_prefs
            if orig_env is not None:
                os.environ["TACIT_PROJECTS_ROOTS"] = orig_env

    def test_fs_list_lists_only_subdirs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "alpha"))
            os.makedirs(os.path.join(tmp, "beta"))
            with open(os.path.join(tmp, "file.txt"), "w"):
                pass
            d = config.fs_list(tmp)
            self.assertTrue(d["ok"])
            self.assertEqual(sorted(x["name"] for x in d["dirs"]), ["alpha", "beta"])
            self.assertEqual(d["parent"], str(config.Path(d["path"]).parent))

    def test_fs_list_missing_dir(self):
        self.assertFalse(config.fs_list("/definitely/not/here/xyz")["ok"])

    def test_fs_roots_nonempty(self):
        self.assertTrue(config.fs_roots())


class TestRemoteParsing(unittest.TestCase):
    """Remote URL -> owner/name, kept in lockstep with the Git panel's regex."""

    def test_forms(self):
        cases = {
            "https://github.com/acme/widget.git": "acme/widget",
            "https://github.com/acme/widget": "acme/widget",
            "https://github.com/acme/widget/": "acme/widget",
            "http://github.com/acme/widget.git": "acme/widget",
            "git@github.com:acme/widget.git": "acme/widget",
            "ssh://git@github.com/acme/widget.git": "acme/widget",
            "https://***@github.com/acme/widget.git": "acme/widget",
            "https://token@github.com/acme/my.repo-name.git": "acme/my.repo-name",
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                got = hosting.parse_remote(url)
                self.assertEqual(got["full_name"] if got else "", want)

    def test_non_github(self):
        for url in ("https://gitlab.com/acme/widget.git", "/home/me/repos/thing", ""):
            with self.subTest(url=url):
                self.assertIsNone(hosting.parse_remote(url))


class TestZZModulePatchesAreRestored(unittest.TestCase):
    """Sorted last on purpose: a patch that leaks is caught by whatever runs after it.

    Two setUps in this file replaced config.resolve_model and never put it back, so
    a test in another file later in the session saw a one-key lambda where a real
    model dict belonged and failed for reasons that pointed nowhere near the cause.
    """

    def test_config_functions_are_the_real_ones(self):
        for fn in (config.resolve_model, config.registry, config.save_registry, config.prefs):
            self.assertTrue(callable(fn))
            self.assertNotEqual(fn.__name__.startswith("<lambda>"), True,
                                f"{fn} was left patched by an earlier test")
        self.assertEqual(config.resolve_model.__name__, "resolve_model")

    def test_engine_functions_are_the_real_ones(self):
        self.assertEqual(engine.remote_models.__name__, "remote_models")
        self.assertEqual(engine.remote_model_details.__name__, "remote_model_details")


class TestProviderReconciliation(unittest.TestCase):
    """Probe has to report both directions.

    Gathering was additive only: it told you what was new and never what had gone,
    so a model the provider retired stayed in the picker indefinitely and failed at
    send time with a 410. Three such rows were sitting in the real registry.
    """

    def _run(self, local, remote, call, *args):
        box = {"reg": {"default": "", "providers": {"p": {
            "baseUrl": "http://x", "models": [dict(m) for m in local]}}}}
        orig = (config.registry, config.save_registry, engine.remote_models)
        config.registry = lambda: box["reg"]
        config.save_registry = lambda r: box.__setitem__("reg", r)
        engine.remote_models = lambda base, key="": list(remote)
        try:
            out = asyncio.run(call(*args))
        finally:
            config.registry, config.save_registry, engine.remote_models = orig
        return out, box["reg"]["providers"]["p"]["models"]

    def test_a_dropped_model_is_reported_and_marked(self):
        r, rows = self._run([{"id": "alive"}, {"id": "dead"}], ["alive"],
                            api.probe_provider, "p")
        self.assertEqual(r["gone"], ["dead"])
        self.assertEqual(r["new"], [])
        self.assertTrue(next(m for m in rows if m["id"] == "dead").get("stale"))
        self.assertFalse(next(m for m in rows if m["id"] == "alive").get("stale"))

    def test_a_model_that_comes_back_loses_the_mark(self):
        r, rows = self._run([{"id": "back", "stale": True}], ["back"],
                            api.probe_provider, "p")
        self.assertEqual(r["gone"], [])
        self.assertFalse(rows[0].get("stale"))

    def test_a_stale_model_is_not_made_the_default(self):
        r, _ = self._run([{"id": "dead", "stale": True}], ["alive"],
                         api.set_default, Req({"model": "p/dead"}))
        # _fail answers with a JSONResponse, so the answer has to be read off the
        # body. Asserting on the object itself would pass for any failure at all.
        self.assertIn("no longer offered", r.body.decode("utf-8"))

    def test_prune_removes_only_what_the_provider_dropped(self):
        r, rows = self._run([{"id": "alive"}, {"id": "dead", "stale": True}],
                            ["alive"], api.prune_models, "p")
        self.assertEqual(r["removed"], ["dead"])
        self.assertEqual([m["id"] for m in rows], ["alive"])


if __name__ == "__main__":
    unittest.main()

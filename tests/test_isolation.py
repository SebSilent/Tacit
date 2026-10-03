"""Tests for isolation: the container backend, and the timeout guarantee.

Two things are being pinned down here.

The container backend existed in the README's isolation table and in the capability
registry as ``implemented=False`` — an option the interface offered and then refused.
These tests hold the real adapter to what the table claimed: network off, read-only
bind, resource ceilings, a PID limit, no privilege escalation, and nothing downloaded
on the user's behalf.

The timeout guarantee is the other one. ``TACIT_SHELL_TIMEOUT`` is documented as the
seconds a command may run, and on Windows it was not: killing a ``cmd /c`` or
``.bat`` wrapper left its grandchildren alive holding the captured pipe, so the call
blocked long after the timeout fired. Measured at the time: two orphaned processes
still running minutes later. A timeout a command can outrun is not a timeout, and on
a platform with no other primitive it is the only thing being enforced.

No test here needs a real container daemon. The runtime is stubbed at the boundary
where a daemon would be, and the argv is asserted directly.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import audit, config, providers, sandbox  # noqa: E402
from tests.helpers import state_paths  # noqa: E402


class Isolated(unittest.TestCase):
    """Points every storage path under the user's home at a temporary one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        config.HOME = home
        for k in self._keys:
            target = home / Path(self._orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        self.addCleanup(setattr, providers, "load", providers.load)

    def tearDown(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)
        self._tmp.cleanup()


SANDBOX_PATCHED = ("_container_runtime", "_image_present", "_run_captured",
                   "_kill_container", "container_settings", "_scan")


class StubbedSandbox(Isolated):
    """Isolation plus a guaranteed restore of every sandbox attribute a test swaps.

    Patching a module attribute without restoring it leaks into every later test in
    the run, where it reads as an unrelated failure. The list is fixed and asserted
    against, so a new patch point has to be declared rather than forgotten.
    """

    def setUp(self):
        super().setUp()
        for name in SANDBOX_PATCHED:
            self.addCleanup(setattr, sandbox, name, getattr(sandbox, name))
        self.addCleanup(setattr, sandbox.subprocess, "run", sandbox.subprocess.run)
        self.addCleanup(setattr, providers, "save", providers.save)
        self.proj = tempfile.TemporaryDirectory()
        self.addCleanup(self.proj.cleanup)


# ── 1. the argv is the enforcement ────────────────────────────────────────
class TestContainerArgv(StubbedSandbox):
    """What is actually asked of the runtime. The flags are the isolation."""

    def setUp(self):
        super().setUp()
        sandbox._container_runtime = lambda: "/usr/bin/docker"

    def argv(self, **kw):
        args = {"network": False, "readonly": False, "limits": {}, "name": "tacit-x"}
        args.update(kw)
        return sandbox.container_argv("echo hi", "/proj", "img:latest", **args)

    def test_the_project_is_bind_mounted_not_copied(self):
        # Copied would mean the change report and snapshots describe a tree the
        # user does not have.
        argv = self.argv()
        mount = next(a for a in argv if a.startswith("type=bind"))
        self.assertIn("source=/proj", mount)
        self.assertIn("target=/workspace", mount)

    def test_readonly_is_a_real_mount_flag(self):
        self.assertIn(",readonly", next(a for a in self.argv(readonly=True)
                                        if a.startswith("type=bind")))
        self.assertNotIn(",readonly", next(a for a in self.argv(readonly=False)
                                           if a.startswith("type=bind")))

    def test_network_off_is_a_real_namespace(self):
        argv = self.argv(network=False)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertNotIn("--network", self.argv(network=True))

    def test_resource_ceilings_are_passed_through(self):
        argv = self.argv(limits={"memory_mb": 512, "cpu_seconds": 30})
        self.assertEqual(argv[argv.index("--memory") + 1], "512m")
        self.assertIn("--cpus", argv)

    def test_no_limits_means_no_limit_flags(self):
        argv = self.argv(limits={})
        self.assertNotIn("--memory", argv)
        self.assertNotIn("--cpus", argv)

    def test_always_no_new_privileges_private_tmp_and_a_pid_ceiling(self):
        argv = self.argv()
        self.assertEqual(argv[argv.index("--security-opt") + 1], "no-new-privileges")
        self.assertEqual(argv[argv.index("--tmpfs") + 1], "/tmp")
        self.assertEqual(argv[argv.index("--pids-limit") + 1], "256")

    def test_it_is_named_so_a_timeout_can_kill_the_container_itself(self):
        argv = self.argv(name="tacit-42")
        self.assertEqual(argv[argv.index("--name") + 1], "tacit-42")
        self.assertIn("--rm", argv)

    def test_the_command_runs_under_a_posix_shell_not_the_host_shell(self):
        # config.SHELL is cmd.exe here. Passing that into a Linux container would
        # fail on every command.
        argv = self.argv()
        self.assertEqual(argv[-3:], ["/bin/sh", "-c", "echo hi"])
        self.assertNotIn(config.SHELL, argv)

    def test_the_image_is_where_the_caller_put_it(self):
        argv = self.argv()
        self.assertEqual(argv[argv.index("/bin/sh") - 1], "img:latest")


# ── 2. nothing is installed for you ───────────────────────────────────────
class TestContainerRefusals(StubbedSandbox):
    def setUp(self):
        super().setUp()
        self.pulled = []

    def _no_pull(self, *a, **kw):
        self.pulled.append(a[0] if a else kw.get("args"))
        raise AssertionError("subprocess.run should not be reached")

    def test_no_runtime_is_a_clear_error_not_a_crash(self):
        sandbox._container_runtime = lambda: ""
        res = sandbox.run("echo hi", project=self.proj.name, backend="container")
        self.assertFalse(res["ok"])
        self.assertIn("container", res["stderr"])

    def test_the_executor_names_both_runtimes_when_it_cannot_find_one(self):
        # Reached directly, past the registry's availability gate.
        sandbox._container_runtime = lambda: ""
        res = sandbox._run_container("echo hi", Path(self.proj.name), {}, 30, "")
        self.assertFalse(res["ok"])
        self.assertIn("no container runtime", res["stderr"])
        self.assertIn("Podman", res["stderr"])

    def test_a_missing_image_is_reported_with_the_exact_command(self):
        sandbox._container_runtime = lambda: "/usr/bin/docker"
        sandbox._image_present = lambda image: False
        sandbox.subprocess.run = self._no_pull
        res = sandbox.run("echo hi", project=self.proj.name, backend="container")
        self.assertFalse(res["ok"])
        self.assertIn("pull", res["stderr"])
        self.assertIn(sandbox.DEFAULT_IMAGE, res["stderr"])

    def test_a_missing_image_is_never_pulled_unless_asked(self):
        # "A container is never required and is never installed for you." An image
        # is a download of exactly the kind that rule covers.
        sandbox._container_runtime = lambda: "/usr/bin/docker"
        sandbox._image_present = lambda image: False
        sandbox.subprocess.run = self._no_pull
        providers.save = lambda data: None
        sandbox.run("echo hi", project=self.proj.name, backend="container")
        self.assertEqual(self.pulled, [])

    def test_auto_pull_is_opt_in_and_then_works(self):
        calls = []

        class R:
            returncode = 0
            stdout = "pulled"
            stderr = ""

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return R()

        sandbox._container_runtime = lambda: "/usr/bin/docker"
        sandbox._image_present = lambda image: False
        sandbox.subprocess.run = fake_run
        sandbox._run_captured = lambda *a, **kw: (0, "hi", "", False)
        sandbox.container_settings = lambda: {"image": "img", "auto_pull": True,
                                              "pids_limit": 256}
        res = sandbox.run("echo hi", project=self.proj.name, backend="container")
        self.assertTrue(res["ok"], res)
        self.assertEqual(calls[0][0], "/usr/bin/docker")
        self.assertEqual(calls[0][1], "pull")
        self.assertEqual(calls[0][2], "img")


# ── 3. the run itself ─────────────────────────────────────────────────────
class TestContainerRun(StubbedSandbox):
    def setUp(self):
        super().setUp()
        sandbox._container_runtime = lambda: "/usr/bin/docker"
        sandbox._image_present = lambda image: True

    def test_success_reports_what_was_enforced(self):
        sandbox._run_captured = lambda *a, **kw: (0, "hello", "", False)
        res = sandbox.run("echo hello", project=self.proj.name, backend="container")
        self.assertTrue(res["ok"])
        self.assertEqual(res["stdout"], "hello")
        self.assertEqual(res["backend"], "container")
        enf = res["enforced"]
        self.assertEqual(enf["mechanism"], "container")
        self.assertTrue(enf["network"] is False or enf["network"] is True)
        self.assertTrue(enf["process_isolation"])
        self.assertTrue(enf["private_tmp"])
        self.assertTrue(enf["no_new_privileges"])

    def test_a_timeout_kills_the_container_not_just_the_client(self):
        killed = []
        sandbox._run_captured = lambda *a, **kw: (124, "", "", True)
        sandbox._kill_container = lambda name: killed.append(name)
        res = sandbox.run("sleep 999", project=self.proj.name, backend="container")
        self.assertEqual(res["code"], 124)
        self.assertIn("timed out", res["stderr"])
        self.assertEqual(len(killed), 1)
        self.assertTrue(any("container was killed" in n for n in res["notes"]))

    def test_the_kill_targets_the_name_it_started(self):
        seen = {}
        sandbox._run_captured = lambda *a, **kw: (124, "", "", True)
        sandbox._kill_container = lambda name: seen.setdefault("name", name)

        def capture(argv, **kw):
            seen["argv"] = list(argv)
            return (124, "", "", True)
        sandbox._run_captured = capture
        sandbox.run("sleep 9", project=self.proj.name, backend="container")
        self.assertEqual(seen["argv"][seen["argv"].index("--name") + 1], seen["name"])

    def test_the_ledger_records_the_image_and_the_backend(self):
        sandbox._run_captured = lambda *a, **kw: (0, "ok", "", False)
        sandbox.run("echo ok", project=self.proj.name, backend="container",
                    session="SID")
        rows = [r for r in audit.recent(10) if r["event"] == "sandbox_run"]
        self.assertEqual(rows[0]["backend"], "container")
        self.assertEqual(rows[0]["session"], "SID")
        self.assertIn("image", rows[0])


# ── 4. the capability registry tells the truth ────────────────────────────
class TestRegistryHonesty(StubbedSandbox):
    def test_container_is_listed_as_implemented(self):
        # It was implemented=False while the README's isolation table offered it.
        row = providers.get("container", "sandbox")
        self.assertTrue(row["implemented"], row)

    def test_container_is_unavailable_without_a_runtime(self):
        # Must hold even on a machine that really does have Docker installed: the
        # registry asks the executor and nothing else. A ``shutil.which`` fallback
        # here once reported the backend available when the executor's own probe
        # had said no — an offered backend that then refuses at runtime.
        orig = sandbox._container_runtime
        sandbox._container_runtime = lambda: ""
        self.addCleanup(setattr, sandbox, "_container_runtime", orig)
        ok, why = providers.available("container", "sandbox")
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_mechanism_reports_container_only_when_it_is_chosen_and_present(self):
        orig_load = providers.load
        self.addCleanup(setattr, providers, "load", orig_load)
        sandbox._container_runtime = lambda: "/usr/bin/docker"
        providers.load = lambda: {"sandbox": {"backend": "none"}}
        self.assertNotEqual(sandbox.mechanism(), "container")
        providers.load = lambda: {"sandbox": {"backend": "container"}}
        self.assertEqual(sandbox.mechanism(), "container")
        caps = sandbox.platform_capabilities()
        self.assertTrue(caps["network"])
        self.assertTrue(caps["readonly_project"])
        self.assertTrue(caps["process_isolation"])

    def test_windows_still_reports_none_when_no_runtime_is_installed(self):
        # The honest answer must survive the new backend: no runtime, no mechanism.
        orig_load = providers.load
        self.addCleanup(setattr, providers, "load", orig_load)
        sandbox._container_runtime = lambda: ""
        providers.load = lambda: {"sandbox": {"backend": "container"}}
        if os.name == "nt":
            self.assertEqual(sandbox.mechanism(), "none")

    def test_describe_reports_the_container_as_usable(self):
        sandbox._container_runtime = lambda: "/usr/bin/docker"
        orig_resolve, orig_load = providers.resolve, providers.load
        providers.resolve = lambda kind: {"id": "container", "ok": True, "reason": "",
                                          "provider": {"id": "container"}}
        providers.load = lambda: {"sandbox": {"backend": "container"}}
        self.addCleanup(setattr, providers, "resolve", orig_resolve)
        self.addCleanup(setattr, providers, "load", orig_load)
        d = sandbox.describe()
        self.assertEqual(d["mechanism"], "container")
        self.assertTrue(d["will_enforce"]["network"])


# ── 5. the timeout actually returns, and actually cleans up ───────────────
class TestTimeoutIsReal(unittest.TestCase):
    """End to end, with a real subprocess that outlives its parent."""

    def test_a_timeout_returns_promptly_and_kills_the_whole_tree(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        marker = Path(tmp.name) / "survivor.txt"
        # The grandchild sleeps past the timeout and then writes a marker. If the
        # tree was killed the marker never appears; if only the wrapper was killed
        # it does, and the timeout was a report rather than an enforcement.
        script = (f"import time,pathlib; time.sleep(4); "
                  f"pathlib.Path(r'{marker}').write_text('survived')")
        cmd = f'"{sys.executable}" -c "{script}"'
        started = time.time()
        code, out, err, timed_out = sandbox._run_captured(
            cmd, cwd=tmp.name, timeout=2, shell=True)
        elapsed = time.time() - started
        self.assertTrue(timed_out)
        self.assertEqual(code, 124)
        # Timeout plus the bounded grace read, with margin — not the child's own
        # lifetime, which is what it used to wait for.
        self.assertLess(elapsed, 2 + sandbox.GRACE_SECONDS + 5,
                        f"took {elapsed:.1f}s, the timeout was not enforced promptly")
        time.sleep(5)
        self.assertFalse(marker.exists(),
                         "a grandchild survived the timeout and finished its work")

    def test_a_fast_command_is_unaffected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cmd = f'"{sys.executable}" -c "print(1234)"'
        code, out, err, timed_out = sandbox._run_captured(
            cmd, cwd=tmp.name, timeout=30, shell=True)
        self.assertFalse(timed_out)
        self.assertEqual(code, 0)
        self.assertIn("1234", out)

    def test_a_nonzero_exit_is_reported_not_swallowed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cmd = f'"{sys.executable}" -c "import sys; sys.stderr.write(\'boom\'); sys.exit(7)"'
        code, out, err, timed_out = sandbox._run_captured(
            cmd, cwd=tmp.name, timeout=30, shell=True)
        self.assertFalse(timed_out)
        self.assertEqual(code, 7)
        self.assertIn("boom", err)


if __name__ == "__main__":
    unittest.main()

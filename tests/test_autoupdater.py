"""Stage 7: the autoupdater's transport is two plain GETs — no git, no keys.

The HTTP seam (`_http_get`) is stubbed per test, so nothing here reaches the
network. The archive is built in memory with `tarfile` in the GitHub shape
(one root directory, files under it), which lets the apply path run against a
real checkout tree: a temp `config.ROOT` with a VERSION file, tracked source,
a protected state file and an upstream-vanished file.
"""

import asyncio
import io
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from backend import autoupdater, config
from tests.helpers import state_paths


def _tar(entries: dict[str, bytes], root: str = "Tacit-master") -> bytes:
    """A GitHub-style tarball in memory: one root dir, files under it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in entries.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class AutoupdaterTest(unittest.TestCase):
    """The version transport, against a throwaway checkout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name) / "home"
        root = Path(self._tmp.name) / "checkout"
        home.mkdir()
        root.mkdir()
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        self._root = config.ROOT
        self.addCleanup(self._restore)
        config.HOME = home
        config.ROOT = root
        for k in self._keys:
            target = home / Path(self._orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        # A throwaway checkout: a version, tracked source, a protected state
        # file, and a file the "upstream" tree will not contain.
        (root / "VERSION").write_text("1.0.0", encoding="utf-8")
        (root / "backend").mkdir()
        (root / "backend" / "old.py").write_text("stale\n", encoding="utf-8")
        (root / "static").mkdir()
        (root / "static" / "app.js").write_text("console.log('v1')\n", encoding="utf-8")
        (root / "models.json").write_text('{"user": "state"}\n', encoding="utf-8")
        # The HTTP seam: every fetch must be one the test armed.
        self._gets: dict[str, object] = {}
        self._orig_get = autoupdater._http_get

        def fake_get(url, timeout=30.0):
            if url not in self._gets:
                raise AssertionError(f"unexpected fetch: {url}")
            payload = self._gets[url]
            if isinstance(payload, Exception):
                raise payload
            return payload

        autoupdater._http_get = fake_get
        self.addCleanup(setattr, autoupdater, "_http_get", self._orig_get)

    def _restore(self):
        config.HOME = self._home
        config.ROOT = self._root
        for k, v in self._orig.items():
            setattr(config, k, v)

    def _newer_archive(self) -> bytes:
        return _tar({
            "VERSION": b"2.0.0\n",
            "backend/new.py": b"fresh\n",
            "static/app.js": b"console.log('v2')\n",
            "models.json": b'{"upstream": "would clobber user state"}\n',
            "notes.bin": b"untracked extension\n",
        })

    def test_check_detects_a_newer_version_and_counts_the_archive(self):
        self._gets = {autoupdater.VERSION_URL: b"2.0.0\n",
                      autoupdater.TARBALL_URL: self._newer_archive()}
        r = asyncio.run(autoupdater.check_for_updates())
        self.assertTrue(r["ok"], str(r)[:200])
        self.assertTrue(r["update_available"])
        self.assertEqual(r["current_version"], "1.0.0")
        self.assertEqual(r["latest_version"], "2.0.0")
        # VERSION itself rides in the archive and differs — it counts.
        self.assertEqual(r["changed_count"], 2)
        self.assertEqual(r["new_count"], 1)
        self.assertEqual({e["path"] for e in r["changed_files"]},
                         {"static/app.js", "VERSION"})
        self.assertEqual(r["new_files"][0]["path"], "backend/new.py")
        # The protected state file and the untracked extension are nobody's
        # business: absent from every list, untouched on disk.
        self.assertNotIn("models.json", str(r["changed_files"] + r["new_files"]))
        self.assertNotIn("notes.bin", str(r["changed_files"] + r["new_files"]))
        self.assertEqual((config.ROOT / "models.json").read_text(encoding="utf-8"),
                         '{"user": "state"}\n')

    def test_an_up_to_date_check_fetches_only_the_version(self):
        self._gets = {autoupdater.VERSION_URL: b"1.0.0\n"}
        r = asyncio.run(autoupdater.check_for_updates())
        self.assertTrue(r["ok"])
        self.assertFalse(r["update_available"])
        # The tarball was never fetched: the cheap check stays cheap.

    def test_pull_applies_files_and_skips_protected_paths(self):
        self._gets = {autoupdater.VERSION_URL: b"2.0.0\n",
                      autoupdater.TARBALL_URL: self._newer_archive()}
        r = asyncio.run(autoupdater.pull_updates())
        self.assertTrue(r["ok"], str(r)[:200])
        self.assertTrue(r["updated"])
        self.assertEqual(r["version"], "2.0.0")
        self.assertIn("backend/new.py", r["updated_files"])
        self.assertIn("static/app.js", r["updated_files"])
        self.assertEqual((config.ROOT / "backend" / "new.py").read_text(encoding="utf-8"),
                         "fresh\n")
        self.assertEqual((config.ROOT / "static" / "app.js").read_text(encoding="utf-8"),
                         "console.log('v2')\n")
        # The VERSION file rides in the archive: the checkout is now the
        # version it was updated to.
        self.assertEqual((config.ROOT / "VERSION").read_text(encoding="utf-8").strip(),
                         "2.0.0")
        # User state untouched.
        self.assertEqual((config.ROOT / "models.json").read_text(encoding="utf-8"),
                         '{"user": "state"}\n')
        # A file upstream no longer has is reported, never deleted — the
        # archive cannot tell "upstream removed this" from "the user added it".
        self.assertIn("backend/old.py", r["removed_upstream"])
        self.assertTrue((config.ROOT / "backend" / "old.py").exists())
        self.assertEqual(autoupdater.get_status()["last_version"], "2.0.0")

    def test_a_dry_run_writes_nothing(self):
        self._gets = {autoupdater.VERSION_URL: b"2.0.0\n",
                      autoupdater.TARBALL_URL: self._newer_archive()}
        r = asyncio.run(autoupdater.pull_updates(dry_run=True))
        self.assertTrue(r["ok"])
        self.assertFalse(r["updated"])
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["would_update"], 3)
        self.assertEqual((config.ROOT / "static" / "app.js").read_text(encoding="utf-8"),
                         "console.log('v1')\n")
        self.assertFalse((config.ROOT / "backend" / "new.py").exists())
        self.assertFalse((config.ROOT / "notes.bin").exists())
        self.assertEqual((config.ROOT / "VERSION").read_text(encoding="utf-8").strip(),
                         "1.0.0")
        self.assertEqual(autoupdater.get_status()["last_version"], "")

    def test_a_failed_download_changes_nothing(self):
        self._gets = {autoupdater.VERSION_URL: b"2.0.0\n",
                      autoupdater.TARBALL_URL: RuntimeError("connection reset")}
        r = asyncio.run(autoupdater.pull_updates())
        self.assertFalse(r["ok"])
        self.assertIn("archive download failed", r.get("error", ""))
        self.assertEqual((config.ROOT / "static" / "app.js").read_text(encoding="utf-8"),
                         "console.log('v1')\n")
        self.assertFalse((config.ROOT / "backend" / "new.py").exists())
        self.assertEqual(autoupdater.get_status()["last_version"], "")

    def test_a_missing_local_version_is_an_honest_error(self):
        (config.ROOT / "VERSION").unlink()
        self._gets = {autoupdater.VERSION_URL: b"2.0.0\n"}
        r = asyncio.run(autoupdater.check_for_updates())
        self.assertFalse(r["ok"])
        self.assertIn("VERSION", r.get("error", ""))

    def test_the_surface_survives_the_transport_swap(self):
        self.assertTrue(autoupdater.set_enabled(False)["ok"])
        self.assertFalse(autoupdater.get_status()["enabled"])
        self.assertTrue(autoupdater.set_enabled(True)["ok"])
        self.assertTrue(autoupdater.set_check_interval(10)["ok"])
        self.assertEqual(autoupdater.get_status()["auto_check_interval"], 60)
        # An install from the git era keeps its last-known value across the
        # field rename.
        config.save_prefs({"autoupdater": {"last_commit": "abc1234"}})
        self.assertEqual(autoupdater.get_status()["last_version"], "abc1234")

    def test_auto_check_respects_the_interval_and_the_switch(self):
        self._gets = {autoupdater.VERSION_URL: b"1.0.0\n"}
        autoupdater.set_check_interval(3600)
        # A check just ran: record it, and the interval must hold the next one.
        state = autoupdater.load_state()
        state["last_check"] = time.time()
        autoupdater.save_state(state)
        self.assertIsNone(asyncio.run(autoupdater.auto_check_if_needed()),
                          "the interval holds: no second check within the hour")
        autoupdater.set_enabled(False)
        state = autoupdater.load_state()
        state["last_check"] = 0
        autoupdater.save_state(state)
        self.assertIsNone(asyncio.run(autoupdater.auto_check_if_needed()),
                          "the switch off is honoured even when the interval has passed")


if __name__ == "__main__":
    unittest.main()
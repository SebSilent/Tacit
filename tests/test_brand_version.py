"""The brand line states the version of this checkout (the deleted plan item 5).

`static/index.html` used to hardcode `<span class="brand-sub">Default Harness</span>`.
Nothing in `app.js` or `platform.js` ever wrote that element, so it was not the
active profile; it was a fixed string that reads like a claim about one, and it
was false for every install using a profile other than "default". The version, by
contrast, is a fact the server already knows: `VERSION` sits at the repo root and
`autoupdater._local_version()` reads it, because the updater has to compare it
against the remote before pulling anything.

What this pins:

* `/api/info` carries `version`, and it is the *same string the updater compares*,
  not a second reader of the file. Two readers drift; one reader cannot.
* A checkout with no `VERSION` yields `""`; the client then says "version
  unknown" instead of showing a blank that looks like a broken page.
* Settings > Server shows both sides of the update comparison. The old "Up to
  date" label named only the remote version, so a person could not tell which
  checkout had been checked.
* The markup no longer contains the old claim.

The client half is checked at source level: this suite has no browser, and an
assertion about a DOM that cannot run here would be a claim, not a test.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import autoupdater, config  # noqa: E402


class TestVersionIsReadOnce(unittest.TestCase):
    """The file has one reader, and it is the updater's."""

    def test_the_public_reader_returns_the_file_verbatim(self):
        text = (config.ROOT / "VERSION").read_text(encoding="utf-8")
        self.assertEqual(autoupdater.local_version(), text.strip())
        self.assertTrue(autoupdater.local_version(),
                        "the shipped checkout must carry a version")

    def test_no_module_but_the_updater_reads_the_version_file(self):
        # A second `config.ROOT / "VERSION"` anywhere else is a second truth: it
        # would keep working after the updater's reader changed shape, and the two
        # would disagree in whichever way nobody thought to test.
        readers = []
        for path in sorted((ROOT / "backend").rglob("*.py")):
            if path.name == "autoupdater.py":
                continue
            if re.search(r"""["']VERSION["']""", path.read_text(encoding="utf-8")):
                readers.append(str(path.relative_to(ROOT)))
        self.assertEqual(readers, [], f"second reader of the VERSION file: {readers}")

    def test_a_checkout_without_a_version_file_yields_empty(self):
        # Empty, not a guess and not an exception: the install predates tracking.
        original = config.ROOT
        with tempfile.TemporaryDirectory() as td:
            config.ROOT = Path(td)
            try:
                self.assertEqual(autoupdater.local_version(), "")
                self.assertEqual(autoupdater.get_status()["local_version"], "")
            finally:
                config.ROOT = original

    def test_the_status_payload_carries_the_local_side(self):
        original = config.ROOT
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
            config.ROOT = root
            try:
                self.assertEqual(autoupdater.get_status()["local_version"], "9.9.9")
            finally:
                config.ROOT = original


class TestInfoEndpoint(unittest.TestCase):
    """`/api/info` is what the page reads, so the version has to be in it."""

    def _get(self):
        from fastapi.testclient import TestClient
        from backend.main import app
        return TestClient(app).get("/api/info")

    def test_info_carries_the_version_the_updater_compares(self):
        body = self._get().json()
        self.assertIn("version", body)
        self.assertEqual(body["version"], autoupdater.local_version())

    def test_info_reports_no_version_for_an_untracked_checkout(self):
        original = config.ROOT
        with tempfile.TemporaryDirectory() as td:
            config.ROOT = Path(td)
            try:
                body = self._get().json()
                self.assertEqual(body["version"], "")
            finally:
                config.ROOT = original


class TestBrandSlot(unittest.TestCase):
    """The two ends of the wire still speak the same key."""

    def _read(self, rel):
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_the_markup_no_longer_claims_a_harness(self):
        html = self._read("static/index.html")
        self.assertNotIn("Default Harness", html)
        self.assertIn('id="brandVersion"', html)

    def test_the_client_fills_the_slot_from_the_field_the_server_sends(self):
        js = self._read("static/app.js")
        # Pin the exact key on both ends: "version" here and "version" there is a
        # contract, and a rename on one side would otherwise pass a prefix grep.
        self.assertIn("getElementById('brandVersion')", js)
        self.assertRegex(js, r"""brand\.textContent\s*=\s*info\.version\s*\|\|""")
        self.assertIn("version unknown", js)

    def test_the_slot_is_actually_made_visible(self):
        # The span ships `hidden`, and style.css:787 makes `[hidden]`
        # `display: none !important`. A writer that only sets textContent puts
        # the version in the DOM where nobody can ever see it, and nothing in
        # this file would notice, because these assertions read source text.
        # That is the shape of bug this pins: the feature is written, tested and
        # permanently invisible.
        js = self._read("static/app.js")
        self.assertRegex(js, r"brand\.hidden\s*=\s*false")
        css = self._read("static/style.css")
        self.assertRegex(css, r"\[hidden\]\s*\{[^}]*display:\s*none",
                         "if [hidden] stopped hiding, this test's premise moved")

    def test_the_panel_shows_which_checkout_was_checked(self):
        js = self._read("static/platform.js")
        self.assertIn("au.local_version", js)
        self.assertRegex(js, r"""statusText\.textContent\s*=\s*`Local \$\{local\}""")

    def test_the_server_side_field_names_match_the_client(self):
        # The handler builds the dict; the client reads it. Read both literally.
        api = self._read("backend/routers/api.py")
        self.assertIn('"version": autoupdater.local_version()', api)
        updater = self._read("backend/autoupdater.py")
        self.assertIn('"local_version": _local_version()', updater)


if __name__ == "__main__":
    unittest.main(verbosity=2)

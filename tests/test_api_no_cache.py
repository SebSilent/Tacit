"""Item 3, the half the store fix does not reach: the response a browser caches.

The anchor this started at, before the header existed:

    `Cache-Control: no-cache` was set for `index.html` only. `/api/sessions` was not
    covered by that header.

Checked, and it was not covered: `/api/sessions/registry` came back with no
Cache-Control, no ETag, no Last-Modified. A response with no freshness information
at all is the one a browser is freest to keep. `app.js:197` reads exactly that
endpoint with a bare `fetch()` to build the session list, so after a delete the
page could repaint a session the disk no longer has; the reported symptom, with
`store.remove()` behaving perfectly the whole time. `transport.js`'s `getJSON`
sends a request-side `Cache-Control: no-cache`, which is a hint to proxies and is
not what governs a browser's cache; the response header is.

The rule is path-based, so the test that matters most here is the one against a
route that does not exist: it proves the header comes from the middleware and not
from whichever endpoint happened to set it. A new `/api/...` endpoint is covered
by construction, and if the rule ever becomes per-route again, that test says so.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402
from tests.helpers import state_paths  # noqa: E402


class IsolatedStore(unittest.TestCase):
    """Point every storage path at a temp home, so this reads no real session data."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from backend.main import app

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
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
        self.addCleanup(self._restore)
        self.client = TestClient(app)

    def _restore(self):
        config.HOME = self._home
        for key, value in self._orig.items():
            setattr(config, key, value)

    def header(self, path):
        return self.client.get(path).headers.get("Cache-Control")


class TheListTheBrowserKeeps(IsolatedStore):
    """The endpoints the session list is built from."""

    def test_registry_is_not_cacheable(self):
        # app.js:197; the payload that decides which sessions the sidebar shows.
        # This is the one that reappeared.
        r = self.client.get("/api/sessions/registry")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(r.headers.get("Cache-Control"), "no-cache")

    def test_the_flat_session_list_is_not_cacheable(self):
        self.assertEqual(self.header("/api/sessions"), "no-cache")

    def test_the_per_session_state_probe_is_not_cacheable(self):
        # app.js:1655: the authoritative "is this tab's session still alive".
        self.assertEqual(self.header("/api/sessions/state"), "no-cache")

    def test_info_is_not_cacheable(self):
        self.assertEqual(self.header("/api/info"), "no-cache")


class TheRuleIsPathBased(IsolatedStore):
    def test_an_unknown_api_route_still_gets_the_header(self):
        """The header must come from the path rule, not from a route that set it.

        This is what makes the coverage total: an endpoint added next month is
        no-cache without anyone remembering to ask, and a per-route regression
        shows up here as a 404 with no header.
        """
        r = self.client.get("/api/no-such-endpoint-for-this-test")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.headers.get("Cache-Control"), "no-cache")


class WhatStaysCacheable(IsolatedStore):
    """The page is already covered; the assets are covered a different way."""

    def test_the_page_is_no_cache(self):
        # PLAN:156; nothing in the suite pinned this before, so a refactor of the
        # FileResponse call could drop it silently.
        self.assertEqual(self.header("/"), "no-cache")

    def test_static_assets_keep_the_version_query_mechanism(self):
        """/static is deliberately outside the rule: `?v=NN` + ETag revalidation is
        how a restart swaps app.js without a hard refresh (restart.ps1's premise).
        Asserting the absence here is a plant; if someone widens the middleware to
        /static, every page load re-downloads ~450 KB and this says so."""
        r = self.client.get("/static/transport.js")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.headers.get("Cache-Control"))
        self.assertIsNotNone(r.headers.get("ETag"),
                             "no ETag means no revalidation and no version story")


if __name__ == "__main__":
    unittest.main()

"""The mock on the landing page must not be able to fall behind the app.

`site/demo.html` is the interface with no server behind it, and the claim only holds while
it IS the interface. The first version was a hand-written drawing of it, and it drifted the
moment the app grew a panel: the app gained the Git overlay, 46 ids, and the mock kept
showing a sidebar that no longer matched the thing it advertised.

So the page is generated from `static/index.html` by `scripts/demo_shell.py`, and this file
fences off the three ways that arrangement can rot:

* an anchor in `demo_shell.PATCHES` that no longer matches the app exactly once, which
  would silently drop a patch (it now raises, and the build stops);
* an id in the app that does not reach the mock, which is what a hand-copied shell always
  loses;
* a query in `site/assets/demo.js` for a node that neither the shell nor the driver ever
  creates, which is a null pointer at page load and a blank panel instead of an error;
* `site/assets/app.css`, which is a byte copy of `static/style.css` and is a lie the moment
  the original changes and the build is not run again.
"""

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import demo_shell  # noqa: E402  (the build's own module, imported by path like the site does)

APP_SHELL = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
DEMO_JS = (ROOT / "site" / "assets" / "demo.js").read_text(encoding="utf-8")

ID_ATTR = re.compile(r'id="([\w-]+)"')
JS_QUERY = re.compile(r"\$\('#([\w-]+)'\)")


def ids_of(text):
    return set(ID_ATTR.findall(text))


class PatchAnchors(unittest.TestCase):
    def test_every_anchor_still_matches_the_app_once(self):
        normalised = demo_shell.STRAY_INDENT.sub(
            r"\1", demo_shell.VERSION_QUERY.sub("", APP_SHELL)
        )
        for old, _new in demo_shell.PATCHES:
            self.assertEqual(
                normalised.count(old), 1,
                "patch anchor is stale or ambiguous: %.60r" % old,
            )

    def test_build_shell_runs_on_the_current_app(self):
        page = demo_shell.build_shell(APP_SHELL)
        self.assertGreater(len(page), len(APP_SHELL) // 2)


class ShellCarriesTheApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = demo_shell.build_shell(APP_SHELL)

    def test_no_app_id_is_lost(self):
        missing = ids_of(APP_SHELL) - ids_of(self.page)
        self.assertEqual(missing, set(), "the mock lost these ids from the app shell")

    def test_nothing_reaches_a_server(self):
        for name in ("transport.js", "app.js", "git.js", "harness.js", "platform.js",
                     "assistant.js", "delegation.js", "/ws", "localhost"):
            self.assertNotIn(name, self.page)

    def test_it_loads_the_mock_instead(self):
        self.assertIn('src="assets/demo.js"', self.page)
        self.assertIn('href="assets/app.css"', self.page)
        self.assertIn("window.TACIT_PROMPTS = @@PROMPTS@@;", self.page)

    def test_the_extra_ids_are_only_the_mock_controls(self):
        extra = ids_of(self.page) - ids_of(APP_SHELL)
        self.assertEqual(extra, {"meta-theme", "panel-prompts", "demoPause",
                                 "demoSpeed", "demoReplay"})

    def test_the_vendored_stylesheet_is_the_real_one(self):
        """`assets/app.css` is a copy, so it is only honest while it is identical."""
        css = (ROOT / "site" / "assets" / "app.css").read_bytes()
        self.assertEqual(css, (ROOT / "static" / "style.css").read_bytes())


class DriverQueriesRealNodes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = demo_shell.build_shell(APP_SHELL)

    def test_every_selector_exists_or_is_made_by_the_driver(self):
        made_by_driver = set(ID_ATTR.findall(DEMO_JS)) | set(
            re.findall(r"\.id = '([\w-]+)'", DEMO_JS)
        )
        unmet = JS_QUERY.findall(DEMO_JS)
        unmet = [i for i in unmet if i not in ids_of(self.page) and i not in made_by_driver]
        self.assertEqual(sorted(set(unmet)), [])

    def test_the_driver_writes_no_class_the_stylesheet_does_not_know(self):
        css = (ROOT / "site" / "assets" / "app.css").read_text(encoding="utf-8")
        demo_css = (ROOT / "site" / "assets" / "demo.css").read_text(encoding="utf-8")
        styled = set(re.findall(r"\.([A-Za-z][\w-]*)", css + demo_css))
        # Only classes the driver assigns through classList, where a typo is invisible.
        toggled = set(re.findall(r"classList\.(?:add|remove|toggle)\('([\w-]+)'", DEMO_JS))
        self.assertGreater(len(toggled), 5, "this check went vacuous")
        self.assertEqual({c for c in toggled if c not in styled}, set())


if __name__ == "__main__":
    unittest.main()

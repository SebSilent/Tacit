"""Hover-preview must preview without deciding.

The Appearance panel shows a theme while the cursor is on its card. The failure
this fences off is the one that is invisible until it isn't: a preview that
persists turns "browse eight palettes" into "eight writes to localStorage, and
whichever one you happened to be hovering when the tab closed is now your theme."

So:

* `previewTheme` sets the document attribute and NOTHING else; no `localStorage`,
  no write to the committed `theme` variable;
* `endPreview` restores from `theme`, which is the committed value precisely
  because preview never touched it;
* every path that destroys the hovered card without a `pointerleave` drops the
  preview by hand: the panel repaint (click) and `loadPanel` (tab switch). A
  missed one leaves the app sitting on an unchosen theme with the flag claiming
  there was never a preview;
* the keyboard gets it too; `focus`/`blur`, or the feature is mouse-only.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HARNESS = (ROOT / "static" / "harness.js").read_text(encoding="utf-8")


def fn(src, name):
    """The body of `function name(...) { ... }`, brace-matched."""
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{" % re.escape(name), src)
    if not m:
        return None
    i, depth = m.end() - 1, 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.end() - 1:i + 1]
        i += 1
    return None


class TestPreviewDoesNotDecide(unittest.TestCase):
    def test_preview_never_persists(self):
        body = fn(APP, "previewTheme")
        self.assertIsNotNone(body, "previewTheme is gone from app.js")
        self.assertNotIn("localStorage", body,
                         "a hover wrote to storage; browsing is not deciding")
        self.assertNotIn("applyTheme", body,
                         "preview delegates to applyTheme, which persists")
        # `theme =` as a STATEMENT, not as `dataset.theme =`; the substring alone
        # also matches the very assignment that makes a preview a preview.
        self.assertIsNone(re.search(r"(?<![\w.])theme\s*=(?!=)", body),
                          "preview reassigned the committed theme, so endPreview "
                          "has nothing to restore")

    def test_end_preview_restores_the_committed_theme(self):
        body = fn(APP, "endPreview")
        self.assertIsNotNone(body, "endPreview is gone from app.js")
        self.assertIn("= theme", body, "endPreview does not restore `theme`")
        self.assertNotIn("localStorage", body,
                         "endPreview reads storage instead of the committed value")

    def test_the_export_carries_both_halves(self):
        """A preview you cannot leave is worse than no preview."""
        m = re.search(r"window\.TacitTheme\s*=\s*\{(.*?)\}", APP, re.S)
        self.assertIsNotNone(m, "the TacitTheme export is gone")
        for key in ("preview", "endPreview"):
            self.assertIn(key + ":", m.group(1),
                          f"TacitTheme does not export {key}")


class TestPreviewAlwaysEnds(unittest.TestCase):
    def test_the_card_wires_both_pointer_and_keyboard(self):
        for ev in ("pointerenter", "pointerleave", "focus", "blur"):
            self.assertIn('"%s"' % ev, HARNESS.replace("'", '"'),
                          f"no {ev} handler on the theme card")

    def test_every_card_destroying_path_stops_the_preview(self):
        """The two repaints that swallow `pointerleave`."""
        render = fn(HARNESS, "renderAppearancePanel")
        self.assertIsNotNone(render, "renderAppearancePanel is gone")
        self.assertIn("stopThemePreview()", render,
                      "the panel repaints without dropping the preview")
        load = fn(HARNESS, "loadPanel")
        self.assertIsNotNone(load, "loadPanel is gone")
        self.assertIn("stopThemePreview()", load,
                      "switching tabs without dropping the preview leaves the "
                      "app on an unchosen theme")
        close = fn(HARNESS, "closeOverlay")
        self.assertIsNotNone(close, "closeOverlay is gone")
        self.assertIn("stopThemePreview()", close,
                      "closing the overlay while hovering strands the preview")

    def test_the_guards_can_fail(self):
        """Mutated COPY only; the shipped files must stay as the tests found them."""
        persisting = APP.replace(
            "function previewTheme(id) {",
            "function previewTheme(id) { localStorage.setItem('tacit.theme', id);", 1)
        self.assertNotIn("localStorage", fn(APP, "previewTheme"))
        self.assertIn("localStorage", fn(persisting, "previewTheme"),
                      "a persisting preview went unnoticed")

        # loadPanel losing its stop, the exact bug the guard exists for. The anchor
        # is the call plus the first branch after it, and the branch order changed
        # when every tab platform.js owns began refreshing on reopen. What is
        # planted is the same: a loadPanel that returns without dropping the preview.
        lost = HARNESS.replace("  stopThemePreview();\n  if (tab === 'providers')",
                               "  if (tab === 'providers')", 1)
        self.assertNotEqual(lost, HARNESS, "the loadPanel anchor moved")
        self.assertIn("stopThemePreview()", fn(HARNESS, "loadPanel"))
        self.assertNotIn("stopThemePreview()", fn(lost, "loadPanel"),
                         "loadPanel lost its stop and nothing noticed")


if __name__ == "__main__":
    unittest.main()

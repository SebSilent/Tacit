"""Every cross-module `window.Tacit*` read must be covered by that namespace's exports.

`static/` is hand-written and has no build step, no bundler and no type checker. The
six served modules after `app.js` talk to it, and to each other, through seven global
namespaces (`window.Tacit`, `window.TacitTheme`, `window.TacitAssistant`,
`window.TacitDelegation`, `window.TacitGit`, `window.TacitHarness`,
`window.TacitPlatform`). Nothing in the suite looked at that surface before this file:
a rename on one side, or a key dropped from an object literal, turns a settings panel or
a git panel into a silent no-op in the browser — the JS twin of `var(--danger, #e5534b)`,
the token that does not exist and so always fell back, which `test_color_tokens.py`
catches on the CSS side.

Two things this guard has to parse honestly, because both fooled a first measurement and
each would have produced a *false defect report* against correct code:

  - **shorthand keys.** `window.TacitAssistant = { handle, sessionChanged }` lists its
    members with no `:`. A `key:`-only matcher sees an empty export set and calls every
    read broken.
  - **`Object.assign(window.Tacit || {}, { … })`.** The first `{` after the `=` is the
    `window.Tacit || {}` *argument*, not the literal. Brace-matching from there yields an
    empty body and silently loses all eight `window.Tacit` members.

Reads are collected in all four forms they appear in — `N.k`, `window.N.k`,
`N['k']`, and through a local alias (`const T = window.TacitTheme` in `harness.js`) —
and the alias only counts inside the file that declares it, or a one-letter name like
`T` would invent reads elsewhere.

The dead exports (`apply`, `load`, `togglePanel`, `clear`, `open`, `close`, `refresh`,
`run`, `openSnapshots`) are *not* treated as orphans: they are deliberate public surface
for the panel buttons and for `index.html`. The invariant is one-directional — every read
resolves — and the export set is pinned so the surface changes by review, not by accident.

**There is deliberately no load-order test.** `index.html` loads `app.js` first, then
`git`/`harness`/`platform`/`assistant`/`delegation`, and it is tempting to guard "the
owner loads before its readers". Measured, that is false of the code as it works:
`app.js` reads `window.TacitAssistant` and `window.TacitDelegation`, whose owners load
*later*, at `:464`, `:467`, `:646`, `:780`, `:880`, `:888` — every one inside a function
and every one feature-detected as `window.N && window.N.k`, so it resolves at call time,
not load time. `harness.js:79`'s `const T = window.TacitTheme` is likewise inside
`renderAppearancePanel()`. No read here captures at evaluation, so script order is not the
invariant, and a guard for it would fail correct code.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
JS = {p.name: p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("*.js"))}

NAMESPACES = ["Tacit", "TacitTheme", "TacitAssistant", "TacitDelegation",
              "TacitGit", "TacitHarness", "TacitPlatform"]

# The surface as measured on 2026-10-13. Pinned so a dropped or renamed member is a
# reviewed change rather than a panel that quietly stops responding.
EXPECTED_EXPORTS = {
    "Tacit": {"getSid", "getWorkdir", "md", "pickDir", "refreshInfo",
              "refreshSoundPref", "sendMessage", "toast"},
    "TacitTheme": {"apply", "current", "endPreview", "preview", "themes"},
    "TacitAssistant": {"handle", "load", "sessionChanged", "setExternalBusy",
                       "togglePanel"},
    "TacitDelegation": {"clear", "handle", "load", "maybeLaunch", "sessionChanged",
                        "togglePanel"},
    "TacitGit": {"close", "open", "refresh", "run"},
    "TacitHarness": {"close", "open"},
    "TacitPlatform": {"openSnapshots", "render"},
}

EXPECTED_READS = {
    "Tacit": {"getSid", "getWorkdir", "md", "pickDir", "refreshInfo",
              "refreshSoundPref", "sendMessage", "toast"},
    # `preview`/`endPreview` reviewed in the hover-preview change: the Appearance
    # panel reads them through the `const T =` alias. `apply` is exported but never
    # shows up here — the detector subtracts call/apply/bind as Function.prototype
    # names, so `T.apply(...)` is invisible to it. Known blind spot, not a missing read.
    "TacitTheme": {"current", "endPreview", "preview", "themes"},
    "TacitAssistant": {"handle", "sessionChanged", "setExternalBusy"},
    "TacitDelegation": {"handle", "maybeLaunch", "sessionChanged"},
    "TacitGit": set(),
    "TacitHarness": set(),
    "TacitPlatform": {"render"},
}


def strip_comments(text):
    """Blank /* … */ and // comments. Prose in this bundle names the very members the
    guard looks for (`// shared over window.Tacit.md`), so unstripped text invents reads."""
    out = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)//[^\n]*", "", out)


def _match_brace(text, i):
    """Index range of the balanced {...} starting at i (text[i] == '{')."""
    depth, j = 0, i
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return i, j
        j += 1
    return i, len(text) - 1


def _split_top_level(body):
    """Object-literal body split on commas that are not nested in () [] {}."""
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def literal_keys(body):
    """Property names of an object-literal body: `k: v`, bare `k,` shorthand, `get k()`,
    and quoted `'k':`."""
    keys = set()
    for part in _split_top_level(body):
        p = part.strip()
        m = re.match(r"^['\"]?(\w+)['\"]?\s*:", p)
        if m:
            keys.add(m.group(1))
            continue
        m = re.match(r"^(?:get|set)\s+(\w+)\s*\(", p)
        if m:
            keys.add(m.group(1))
            continue
        m = re.match(r"^(\w+)$", p)          # shorthand: `{ handle, load }`
        if m:
            keys.add(m.group(1))
    return keys


def exported(text, name):
    """Every member `window.<name>` gains in this text."""
    t = strip_comments(text)
    keys = set()
    for m in re.finditer(r"window\.%s\s*=\s*" % name, t):
        i = m.end()
        if t.startswith("Object.assign(", i):
            # the literal is an ARGUMENT; scan every brace inside the call, not the
            # first one after `=`, which belongs to `window.<name> || {}`
            open_paren = t.index("(", i)
            depth, k = 0, open_paren
            while k < len(t):
                if t[k] == "(":
                    depth += 1
                elif t[k] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            for b in re.finditer(r"\{", t[open_paren:k]):
                s, e = _match_brace(t, open_paren + b.start())
                keys |= literal_keys(t[s + 1:e])
        elif i < len(t) and t[i] == "{":
            s, e = _match_brace(t, i)
            keys |= literal_keys(t[s + 1:e])
    # dotted assignment: `window.Tacit.md = …`, but never `… === …`
    for m in re.finditer(r"window\.%s\.(\w+)\s*=(?!=)" % name, t):
        keys.add(m.group(1))
    return keys


def aliased(text, name):
    """Local names bound to the namespace: `const T = window.TacitTheme`."""
    t = strip_comments(text)
    return {m.group(1) for m in re.finditer(
        r"(?:const|let|var)\s+(\w+)\s*=\s*(?:window\.)?%s\b" % name, t)}


def _reads_off(t, base):
    """`base.k` and `base['k']` — bracket form included, or a rename written in one of
    the two styles is invisible to the scan."""
    keys = set()
    for m in re.finditer(r"\b%s\.(\w+)" % base, t):
        keys.add(m.group(1))
    for m in re.finditer(r"\b%s\[\s*['\"](\w+)" % base, t):
        keys.add(m.group(1))
    return keys


def read_members(text, name):
    """Members this text reads off the namespace, in every form it uses."""
    t = strip_comments(text)
    keys = _reads_off(t, r"(?:window\.)?%s" % name)
    # an alias only counts inside the file that declares it, and never picks up the
    # Function.prototype methods every callable has
    for alias in aliased(t, name):
        keys |= _reads_off(t, re.escape(alias)) - {"call", "apply", "bind"}
    return keys


def surface(files=None):
    """{namespace: (exports, reads)} over a dict of {file: text}."""
    files = files or JS
    out = {}
    for n in NAMESPACES:
        exp, red = set(), set()
        for text in files.values():
            exp |= exported(text, n)
            red |= read_members(text, n)
        out[n] = (exp, red)
    return out


class TestModuleSurface(unittest.TestCase):
    def test_every_read_resolves_to_an_export(self):
        """The invariant. A read with no export behind it is a silent no-op."""
        broken = {}
        for n, (exp, red) in sorted(surface().items()):
            missing = red - exp
            if missing:
                readers = sorted({f for f, t in JS.items() if read_members(t, n) & missing})
                broken[n] = (sorted(missing), readers)
        self.assertEqual(broken, {}, "cross-module reads with no export behind them")

    def test_the_measured_surface_is_unchanged(self):
        """Exports and reads pinned: the contract changes by review, not by accident."""
        s = surface()
        for n in NAMESPACES:
            self.assertEqual(s[n][0], EXPECTED_EXPORTS[n],
                             f"window.{n} export set changed: +{sorted(s[n][0] - EXPECTED_EXPORTS[n])} "
                             f"-{sorted(EXPECTED_EXPORTS[n] - s[n][0])}")
            self.assertEqual(s[n][1], EXPECTED_READS[n],
                             f"window.{n} read set changed: +{sorted(s[n][1] - EXPECTED_READS[n])} "
                             f"-{sorted(EXPECTED_READS[n] - s[n][1])}")

    def test_the_guard_reads_something(self):
        """Anti-vacuity. A matcher that silently finds nothing passes every test above."""
        s = surface()
        total_e = sum(len(e) for e, _ in s.values())
        total_r = sum(len(r) for _, r in s.values())
        self.assertGreaterEqual(total_e, 29, f"only {total_e} exports parsed")
        self.assertGreaterEqual(total_r, 15, f"only {total_r} reads parsed")
        # the two shapes that fooled the first measurement must be seen explicitly
        self.assertIn("toast", s["Tacit"][0], "shorthand key lost from Object.assign")
        self.assertIn("getSid", s["Tacit"][0], "Object.assign literal not parsed at all")
        self.assertIn("handle", s["TacitAssistant"][0], "shorthand literal not parsed")
        self.assertIn("themes", s["TacitTheme"][1], "aliased read (const T =) not followed")

    def test_no_unregistered_tacit_namespace(self):
        """A new global surface must join NAMESPACES or it escapes this file."""
        found = set()
        for text in JS.values():
            for m in re.finditer(r"window\.(Tacit\w*)\s*=", strip_comments(text)):
                found.add(m.group(1))
        self.assertEqual(found, set(NAMESPACES),
                         f"untracked namespace(s): {sorted(found - set(NAMESPACES))}; "
                         f"gone: {sorted(set(NAMESPACES) - found)}")

    def test_planted_breaks_are_caught(self):
        """Teeth, on in-memory copies only. Each plant is a real way to break a panel."""
        app, plat, harness = JS["app.js"], JS["platform.js"], JS["harness.js"]

        # 1. a typo'd read
        typo = plat.replace("window.Tacit.refreshSoundPref()",
                            "window.Tacit.refreshSoundPref2()", 1)
        self.assertNotEqual(typo, plat, "the read anchor moved")
        self.assertIn("refreshSoundPref2", surface({"p.js": typo})["Tacit"][1]
                      - surface({"p.js": typo})["Tacit"][0])

        # 2. the export dropped — the read survives, the panel does not
        dropped = app.replace("  refreshSoundPref: () => loadSoundPref(),\n", "", 1)
        self.assertNotEqual(dropped, app, "the export anchor moved")
        s = surface({"a.js": dropped, "p.js": plat})
        self.assertNotIn("refreshSoundPref", s["Tacit"][0])
        self.assertIn("refreshSoundPref", s["Tacit"][1])

        # 3. renamed on one side only
        renamed = app.replace("getSid: () => activeId,", "sessionId: () => activeId,", 1)
        self.assertNotEqual(renamed, app, "the rename anchor moved")
        s = surface({"a.js": renamed, "g.js": JS["git.js"]})
        self.assertIn("getSid", s["Tacit"][1] - s["Tacit"][0])

        # 4. bracket form, which a `N\\.\\w+` matcher alone would miss
        bracket = harness.replace("T.current()", "T['current']()", 1)
        self.assertNotEqual(bracket, harness, "the bracket anchor moved")
        self.assertIn("current", read_members(bracket, "TacitTheme"))

        # 5. an aliased read through `const T = window.TacitTheme`
        alias = harness.replace("T.themes", "T.pallettes", 1)
        self.assertNotEqual(alias, harness, "the alias anchor moved")
        self.assertIn("pallettes", surface({"h.js": alias})["TacitTheme"][1])

        # 6. a namespace nobody registered
        ghost = app + "\nwindow.TacitGhost = { boo: 1 };\n"
        found = {m.group(1) for m in re.finditer(r"window\.(Tacit\w*)\s*=",
                                                strip_comments(ghost))}
        self.assertEqual(found - set(NAMESPACES), {"TacitGhost"})


if __name__ == "__main__":
    unittest.main()

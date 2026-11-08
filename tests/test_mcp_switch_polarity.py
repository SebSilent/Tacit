"""The MCP row switch must ask for the state the row is not in.

Reported by hand: "the toggle looks always as on no matter how much I click it."
Nothing in the backend was wrong - `PATCH /api/mcp/servers/{id}` toggles correctly and
the tests in test_platform.py prove it, because they call the API directly and never go
through the switch. The switch was the whole bug:

    static/mcp.js  row()    class="mcp-row${s.enabled ? '' : ' off'}"
    static/mcp.js  onClick() toggle(id, !row.classList.contains('off'))

`off` is present when the server is disabled. So an enabled row produced
`!false` = enable **true**, and a disabled row produced `!true` = enable **false**: every
click re-asserted the state the row already sat in, in both directions. The panel then
re-rendered from the server, which agreed with itself perfectly, so the switch looked
idle rather than broken and no request ever failed.

A grep for `contains('off')` cannot tell those two lines apart - the broken and the fixed
one differ by one character. So this reads the three expressions out of the file and runs
them through node, the same parser the browser uses, and asks the only question that
matters: for a row that is on, what does the click request?
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MCP_JS = ROOT / "static" / "mcp.js"

# Each is the interpolation inside one attribute of the rendered row. Non-greedy up to
# the closing quote, which is safe here because all three are ternaries over quoted
# literals and none contains a brace or a double quote.
ROW_CLASS = re.compile(r'class="mcp-row\$\{(.+?)\}"')
BTN_CLASS = re.compile(r'class="tgl\$\{(.+?)\}"')
CLICK_ENABLE = re.compile(r"dataset\.act === 'toggle'\)\s+toggle\(id,\s*(.+?)\);")

_TAIL = """
const classFor = new Function('s', 'return ' + ROW + ';');
const btnFor = new Function('s', 'return ' + BTN + ';');
const enableFor = new Function('row', 'return ' + CLICK + ';');
const out = [];
for (const enabled of [true, false]) {
  const s = { enabled };
  // The row element's real class list: whatever the template interpolated onto
  // `mcp-row`, split the way the DOM would see it.
  const classes = ('mcp-row' + classFor(s)).trim().split(/\\s+/);
  const row = { classList: { contains: (c) => classes.includes(c) } };
  out.push({ enabled, row_class: classFor(s), btn_class: btnFor(s),
             requested: enableFor(row) });
}
console.log(JSON.stringify(out));
"""


def _expr(pattern, label):
    """Pull one expression out of the file, or fail rather than hand back nothing.

    A guard that silently extracts nothing tests nothing, and the row template is
    exactly the kind of line that gets reworded while its behaviour stays right."""
    src = MCP_JS.read_text(encoding="utf-8")
    m = pattern.search(src)
    if not m:
        raise AssertionError(
            f"{label} no longer matches in static/mcp.js; the row or the click handler "
            "changed shape and this guard is now reading nothing")
    return m.group(1)


def run_switch():
    payload = ("const ROW = %s, BTN = %s, CLICK = %s;\n" % (
        json.dumps(_expr(ROW_CLASS, "ROW_CLASS")),
        json.dumps(_expr(BTN_CLASS, "BTN_CLASS")),
        json.dumps(_expr(CLICK_ENABLE, "CLICK_ENABLE")))) + _TAIL
    fd, path = tempfile.mkstemp(suffix=".js", prefix="tacit_switch_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        res = subprocess.run(["node", path], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
    finally:
        os.unlink(path)
    if res.returncode != 0:
        raise AssertionError("node failed: " + res.stderr[:800])
    return {r["enabled"]: r for r in json.loads(res.stdout)}


@unittest.skipUnless(shutil.which("node"), "node not on PATH; switch logic untestable")
class McpSwitchPolarity(unittest.TestCase):
    def test_a_click_requests_the_other_state(self):
        """The one property that was inverted. Both directions, because the bug was
        symmetric and only one of them is what the user happened to notice."""
        got = run_switch()
        on = got[True]
        self.assertEqual(on["requested"], False,
                         "clicking a row that is ON asked for ON again - the switch "
                         "cannot turn a server off")
        off = got[False]
        self.assertEqual(off["requested"], True,
                         "clicking a row that is OFF asked for OFF again - the switch "
                         "cannot turn a server on")

    def test_the_row_and_the_switch_agree_about_which_state_is_which(self):
        """`off` on the row and `on` on the switch are two spellings of one fact.

        They are read from the same field today. If one were ever reworded to key off
        something else - the button's own class, a cached count - the click would flip
        against the picture, which is the same dead switch wearing a different hat."""
        got = run_switch()
        self.assertEqual(got[True]["row_class"], "")
        self.assertEqual(got[True]["btn_class"], " on")
        self.assertEqual(got[False]["row_class"], " off")
        self.assertEqual(got[False]["btn_class"], "")

    def test_the_switch_does_not_read_its_own_button(self):
        """Derive the next state from the row, not from the `.tgl on` class.

        Reading the button would work today and break the moment a row is rendered
        disabled but its switch still marked on - which is precisely the drift the test
        above is watching for."""
        click = _expr(CLICK_ENABLE, "CLICK_ENABLE")
        self.assertIn("row.classList", click,
                      f"the click derives state from {click!r} instead of the row")
        self.assertNotIn("btn.classList", click)


if __name__ == "__main__":
    unittest.main()

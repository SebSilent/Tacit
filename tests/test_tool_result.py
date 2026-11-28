"""P2.4 (PLAN 12): the structured ToolResult side-channel, migrated tools.

Shell had it; the file tools now carry their facts out-of-band too, and the
loop reads whichever shape the side-channel produced (top-level on the
streamed path, the `meta` key anywhere else), with the ERROR-prefix text rule
as the last resort it already was. The strings the model reads are identical
throughout: this migration changes what the harness knows, not what the
model sees. And a migrated tool's refusal can no longer be misread: a failed
write is intent, not delivery, whatever its text looked like.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from backend import agent, config
from tests.helpers import state_paths


class ToolResult(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        self.addCleanup(self._restore)
        config.HOME = home
        for k in self._keys:
            target = home / Path(self._orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        self.proj = home / "proj"
        self.proj.mkdir(exist_ok=True)

    def _restore(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)
        config.ensure_home()

    def _call(self, name, args):
        out: dict = {}
        ctx = {"project": str(self.proj), "ref": None, "depth": 0,
               "readonly": False, "session": "tr-1", "stop": None,
               "work": None, "allowed_tools": None}
        list(agent.call_tool(name, dict(args or {}), ctx, out,
                             {"id": "c1", "name": name}))
        return out

    # ── write_file: success and the exact same string it always said ──────
    def test_a_write_carries_its_facts_out_of_band(self):
        out = self._call("write_file",
                         {"path": "a.rs", "content": "fn main() {}\n" * 7})
        self.assertTrue(str(out["result"]).startswith("wrote "),
                        out["result"])
        meta = out.get("meta")
        self.assertEqual(meta.get("status"), "written")
        self.assertIs(meta.get("is_error"), False)
        self.assertEqual(meta.get("bytes"), 91)
        self.assertIn("a.rs", str(meta.get("path")))

    def test_an_edit_that_is_ambiguous_is_a_structured_error(self):
        p = self.proj / "b.py"
        p.write_text("x = call(1)\nx = call(2)\n" * 3, encoding="utf-8")
        out = self._call("edit_file",
                         {"path": "b.py", "find": "x = call(",
                          "replace": "y = call("})
        self.assertEqual(out["result"],
                         "ERROR: search text appears 6 times in b.py; make it "
                         "unique")
        meta = out.get("meta")
        self.assertEqual(meta.get("status"), "ambiguous")
        self.assertIs(meta.get("is_error"), True)
        self.assertEqual(meta.get("matches"), 6)

    def test_an_edit_that_finds_nothing_is_a_structured_error(self):
        p = self.proj / "c.py"
        p.write_text("keep\n", encoding="utf-8")
        out = self._call("edit_file",
                         {"path": "c.py", "find": "absent", "replace": "x"})
        self.assertEqual(out["result"], "ERROR: search text not found in c.py")
        self.assertEqual(out["meta"].get("status"), "not_found")
        self.assertIs(out["meta"].get("is_error"), True)

    def test_a_successful_edit_reports_one_replacement(self):
        p = self.proj / "d.py"
        p.write_text("alpha only here\n", encoding="utf-8")
        out = self._call("edit_file",
                         {"path": "d.py", "find": "alpha", "replace": "beta"})
        self.assertEqual(out["result"],
                         f"edited {_rel('d.py')} (1 replacement)")
        self.assertEqual(out["meta"].get("status"), "edited")
        self.assertIs(out["meta"].get("is_error"), False)

    def test_a_missing_file_is_the_same_string_with_facts_now(self):
        out = self._call("read_file", {"path": "ghost.py"})
        self.assertEqual(out["result"], "ERROR: ghost.py does not exist")
        self.assertEqual(out["meta"].get("status"), "missing")
        self.assertIs(out["meta"].get("is_error"), True)

    def test_a_read_carries_its_shape_out_of_band(self):
        p = self.proj / "e.md"
        p.write_text("\n".join(f"line {i}" for i in range(1, 26)),
                     encoding="utf-8")
        out = self._call("read_file", {"path": "e.md", "offset": 3, "limit": 5})
        body = str(out["result"])
        self.assertIn("lines 4-8 of 25 shown", body)
        meta = out.get("meta")
        self.assertEqual(meta.get("status"), "read")
        self.assertIs(meta.get("is_error"), False)
        self.assertEqual(meta.get("lines_total"), 25)
        self.assertEqual(meta.get("lines_shown"), 5)
        self.assertEqual(meta.get("offset"), 3)  # answered verbatim, as sent
        self.assertIs(meta.get("clipped"), False)

    def test_a_listing_reports_its_count(self):
        for name in ("aa.txt", "bb.txt"):
            (self.proj / name).write_text(name, encoding="utf-8")
        (self.proj / "sub").mkdir()
        out = self._call("list_files", {"path": "."})
        self.assertEqual(out["result"], "DIR  sub\nFILE aa.txt\nFILE bb.txt")
        meta = out.get("meta")
        self.assertEqual(meta.get("status"), "listed")
        self.assertIs(meta.get("is_error"), False)
        self.assertEqual(meta.get("count"), 3)

    def test_a_glob_names_truncation_in_the_structure(self):
        for i in range(3):
            (self.proj / f"g{i}.log").write_text("x", encoding="utf-8")
        out = self._call("glob_files", {"pattern": "g*.log"})
        self.assertEqual(out["meta"].get("count"), 3)
        self.assertIs(out["meta"].get("truncated"), False)
        bad = self._call("glob_files", {"pattern": ""})
        self.assertEqual(bad["result"], "ERROR: pattern required")
        self.assertEqual(bad["meta"].get("status"), "bad_pattern")
        self.assertIs(bad["meta"].get("is_error"), True)

    # ── bg jobs: the id is a key, not a string to be re-parsed ────────────
    def test_bg_start_reports_its_job_and_command_out_of_band(self):
        out = self._call("bg_start", {"command": "sleep 30"})
        text = str(out["result"])
        self.assertTrue(text.startswith("started ["), text)
        try:
            from backend import extras
            bid = text.split("[")[1].split("]")[0]
            self.assertEqual(out["meta"].get("job"), bid, text)
        finally:
            bid = text.split("[")[1].split("]")[0]
            extras.bg_stop(bid)
        self.assertIs(out["meta"].get("is_error"), False)
        self.assertEqual(out["meta"].get("status"), "started")
        self.assertIn("sleep 30", out.get("result") or "")

    def test_bg_stop_and_output_name_the_job(self):
        from backend import extras
        start = self._call("bg_start", {"command": "sleep 30"})
        bid = str(start["result"]).split("[")[1].split("]")[0]
        try:
            self.addCleanup(lambda: extras.bg_stop(bid))
            out = self._call("bg_output", {"id": bid, "tail": 100})
            self.assertEqual(out["meta"].get("job"), bid)
            self.assertEqual(out["meta"].get("status"), "output")
            stop = self._call("bg_stop", {"id": bid})
            self.assertEqual(stop["meta"].get("job"), bid)
            self.assertEqual(stop["meta"].get("status"), "stopped")
            self.assertIs(stop["meta"].get("is_error"), False,
                          "asking for the state it got is not a failure")
        finally:
            extras.bg_stop(bid)

    def test_fetch_carries_its_url_and_shape(self):
        out = self._call("fetch", {"url": "http://127.0.0.1:1/x"})
        self.assertIs(out["meta"].get("is_error"), True)
        self.assertEqual(out["meta"].get("url"), "http://127.0.0.1:1/x")
        self.assertTrue(str(out["result"]).startswith("ERROR:"))

    # ── the reader: both side-channel shapes, and the last resort ─────────
    def test_the_streamed_shape_is_lifted_from_the_top_level(self):
        m = agent._tool_meta("run_shell",
                             {"status": "completed", "is_error": False,
                              "exit_code": 0, "backend": "none"},
                             "exit code 0\nhi")
        self.assertEqual(m["status"], "completed")
        self.assertEqual(m["exit_code"], 0)

    def test_the_text_rule_survives_as_the_last_resort(self):
        out = {"result": "ERROR: something unexpected"}
        m = agent._tool_meta("weird_tool", out, out["result"])
        self.assertIs(m.get("is_error"), True)
        ok_tool = agent._tool_meta("plain_tool", {}, "a plain answer")
        self.assertIs(ok_tool.get("is_error"), False)

    def test_the_card_comes_from_the_side_channel_now(self):
        card = agent._shell_card_fields(
            "run_shell", "exit code 0\nhi",
            meta={"status": "completed", "is_error": False, "exit_code": 0,
                  "backend": "bubblewrap",
                  "changed": {"count": 2, "added": ["a"], "modified": ["b"],
                              "removed": []}})
        self.assertEqual(card.get("backend"), "bubblewrap")
        self.assertEqual(card.get("changed"), 2)
        none_card = agent._shell_card_fields(
            "run_shell", "", meta={"backend": "none"})
        self.assertEqual(none_card.get("backend"), "none",
                         "an explicit backend fact earns its badge")


def _rel(name):
    # The tools return paths relative to the working root; the test's cwd
    # differences make the exact string vary, so the assertion reconstructs
    # what the renderer will say.
    from backend import config as _c
    root = str(_c.work_root(None, "tr-1"))
    return name.replace("\\", "/")


if __name__ == "__main__":
    unittest.main()

"""The evaluation pipeline, proven offline (P0B.3).

These tests run the real runner: real headless subprocesses, real checkers,
real manifests, against the fake provider. They prove the pipeline and the
failure classification, not model capability: a solved offline task means the
plumbing carried a scripted answer to a verified artifact, nothing more.
"""

import json
import tempfile
import unittest
from pathlib import Path

from backend import manifest
from scripts import eval_matrix


class TestEvalMatrixOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name)
        cls.tasks = eval_matrix.load_tasks()
        cls.report = eval_matrix.run_matrix(cls.tasks, mode="offline", out_dir=cls.out)
        cls.rows = {r["task"]: r for r in cls.report["rows"]}
        cls.run_dir = cls.out / cls.report["run_id"]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_report_and_raw_artifacts_exist(self):
        self.assertTrue((self.run_dir / "report.json").is_file())
        self.assertTrue((self.run_dir / "report.md").is_file())
        for task_id in ("write-greeting", "fix-off-by-one", "rename-helper", "docs-note"):
            row = self.rows[task_id]
            self.assertTrue(Path(row["manifest"]).is_file(), task_id)
            self.assertTrue(Path(row["workdir"]).is_dir(), task_id)
            self.assertTrue((self.run_dir / task_id / "report_row.json").is_file())

    def test_scripted_work_is_verified_by_the_checkers(self):
        for task_id in ("write-greeting", "fix-off-by-one", "rename-helper", "docs-note"):
            row = self.rows[task_id]
            self.assertFalse(row["infra_failure"], f"{task_id}: {row['error']}")
            self.assertTrue(row["solved"], f"{task_id}: {row['checker_output']}")
        self.assertEqual((Path(self.rows["write-greeting"]["workdir"]) / "hello.txt")
                         .read_text(encoding="utf-8"), "hello\n")

    def test_a_plumbing_failure_is_classified_as_infrastructure(self):
        row = self.rows["infra-provider-down"]
        self.assertTrue(row["infra_failure"])
        self.assertFalse(row["solved"])
        conf = {c["task"]: c["classified_as_infra"] for c in self.report["conformance"]}
        self.assertTrue(conf.get("infra-provider-down"))

    def test_splits_are_aggregated_separately(self):
        splits = self.report["splits"]
        self.assertIn("tuning", splits)
        self.assertIn("held_out", splits)
        self.assertEqual(splits["tuning"]["tasks"], 4)
        self.assertEqual(splits["held_out"]["tasks"], 1)
        self.assertEqual(splits["tuning"]["infra_failures"], 1)
        self.assertEqual(splits["tuning"]["solved"], 3)
        self.assertEqual(splits["held_out"]["solved"], 1)

    def test_a_live_only_task_is_skipped_offline(self):
        row = self.rows["research-summary"]
        self.assertTrue(row.get("skipped"))

    def test_the_docs_task_did_not_execute_anything(self):
        row = self.rows["docs-note"]
        loaded = json.loads(Path(row["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(loaded["kind"], "benchmark")
        names = [t["name"] for t in loaded["tool_calls"]]
        self.assertNotIn("run_shell", names,
                         "a documentation-only edit must not trigger execution")
        self.assertIn("edit_file", names)
        self.assertTrue(loaded["file_mutations"], "the edit's evidence must be recorded")

    def test_manifests_carry_the_run_identity(self):
        row = self.rows["write-greeting"]
        loaded = json.loads(Path(row["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(loaded["schema_version"], manifest.SCHEMA_VERSION)
        self.assertTrue(loaded["source_fingerprint"])
        self.assertTrue(loaded["config_hash"])
        self.assertEqual(loaded["terminal"]["status"], "completed")
        self.assertGreater(loaded["usage"]["fresh"], 0)


if __name__ == "__main__":
    unittest.main()
import os
import sys
import tempfile
import unittest

from backend import config


class TestShellTimeoutFeedBack(unittest.TestCase):
    """A 90%-done computation used to look identical to a hung no-op: the partial output was
    discarded and the message didn't say whose cap it was. feal's attacks die this way - the
    model reruns the same >30s command five times and burns the cell."""

    def setUp(self):
        self.orig = config.SHELL_TIMEOUT
        config.SHELL_TIMEOUT = 2
        slow = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        slow.write("import sys, time\n"
                   "print('diffs-collected')\n"
                   "print('candidates=17\tpartial: 4294901760', flush=True)\n"
                   "time.sleep(30)\n")
        slow.close()
        self.cmd = f'"{sys.executable}" "{slow.name}"'
        self.tmp = slow.name

    def tearDown(self):
        config.SHELL_TIMEOUT = self.orig
        os.unlink(self.tmp)

    def test_a_timeout_carries_the_partial_output_and_the_cap_is_named(self):
        from backend.sandbox import run
        res = run(self.cmd, project=None, timeout=2)
        self.assertFalse(res["ok"])
        self.assertIn("timed out after 2s", res["stderr"])
        self.assertIn("shell's cap", res["stderr"],
                      "the model has to know the kill is ours, not the task's budget")
        self.assertIn("cannot pass", res["stderr"],
                      "and that rerunning it unchanged is not a plan")
        self.assertIn("candidates=17", res["stdout"],
                      "the progress printed before the kill is kept")
        self.assertIn("partial: 4294901760", res["stdout"])


if __name__ == "__main__":
    unittest.main()
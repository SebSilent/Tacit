import os
import unittest

from backend import sandbox


@unittest.skipUnless(os.name != "nt", "signal deaths are a POSIX return-code convention")
class TestSignalDeath(unittest.TestCase):
    """A shell killed by a signal reads as `exit code -11` (or 139 through a
    POSIX shell's 128+signum convention) - numbers a model and a person both
    have to decode. The result names the signal instead, so a terminal's
    `Program 'fish' crashed.` beside a Tacit command can be paired with the
    tool's own account of what happened."""

    def test_a_segfaulting_command_is_reported_by_signal_name(self):
        import sys
        res = sandbox.run(
            f'"{sys.executable}" -c "import ctypes; ctypes.string_at(1)"',
            project=None, timeout=30)
        # The sandbox contract: `ok` means the command ran, the exit code is
        # its own field. The failure surfaces through the structured result.
        self.assertTrue(res["ok"])
        notes = " ".join(res.get("notes") or [])
        self.assertIn("SIGSEGV", notes, res.get("notes"))
        # Direct watch reports -signum; a POSIX shell's own exit for a child
        # killed by a signal is 128+signum. Either reading is honest; both
        # must name the signal above.
        self.assertIn(res["code"], (-11, 139), res)

    def test_the_name_helper(self):
        self.assertEqual(sandbox._signal_name(-11), "SIGSEGV")
        self.assertEqual(sandbox._signal_name(-9), "SIGKILL")
        self.assertEqual(sandbox._signal_name(139), "SIGSEGV")
        self.assertEqual(sandbox._signal_name(3), "")
        self.assertEqual(sandbox._signal_name(1200), "")
        self.assertEqual(sandbox._signal_name(None), "")
        self.assertEqual(sandbox._signal_name("x"), "")


if __name__ == "__main__":
    unittest.main()
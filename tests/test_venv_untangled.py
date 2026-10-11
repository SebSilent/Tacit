"""Tacit's virtualenv belongs to Tacit, and is built by a base Python.

How the entanglement happened: `python` on PATH is whichever virtualenv was
activated last. An installer that takes the first one it finds and runs
`-m venv` with it produces a venv whose pyvenv.cfg names *that* interpreter as
its origin. Tacit then carries a reference to a project the user never chose --
and which they may later uninstall, taking the recorded interpreter with it.

The guard has two halves: the installers must refuse a foreign venv's python, and
the venv on disk must not name one in its provenance.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


OWN_VENV = (ROOT / ".venv").resolve().as_posix().lower()

# The value this guard was written for: a pyvenv.cfg `command` line recorded by a
# real install, whose interpreter belongs to another project's virtualenv.
HERMES_COMMAND = (r"C:\Users\Silen\AppData\Local\hermes\hermes-agent\venv"
                  r"\Scripts\python.exe -m venv C:\Projects\Tacit\.venv")
BASE_COMMAND = (r"C:\Users\Silen\AppData\Roaming\uv\python\cpython-3.11-windows"
                r"-x86_64-none\python.exe -m venv C:\Projects\Tacit\.venv")


def _is_foreign_venv_interpreter(raw: str) -> bool:
    """True when the *interpreter* in a value lives inside a virtualenv.

    Only the interpreter is judged. A `command` line also names the target
    directory, and exempting on that substring is what let the original hermes
    line look clean: it ends in "...\\Tacit\\.venv", so a whole-string check finds
    ".venv" in it and waves it through.

    A venv's own python sits under Scripts/ (Windows) or bin/ (POSIX) beneath a
    venv directory; a base install is a bare python.exe in its own tree.
    """
    parts = str(raw or "").strip().split()
    if not parts:
        return False
    p = parts[0].replace("\\", "/").lower()
    if "/scripts/python" not in p and "/bin/python" not in p:
        return False
    return not p.startswith(OWN_VENV)


class TestTheGuardItself(unittest.TestCase):
    """The detector is under test too. A guard that cannot see the case it was
    written for is not a guard, and this one nearly was."""

    def test_it_flags_the_line_that_motivated_this_file(self):
        self.assertTrue(_is_foreign_venv_interpreter(HERMES_COMMAND),
                        "the hermes command line is exactly what must be caught")

    def test_it_accepts_a_base_python(self):
        self.assertFalse(_is_foreign_venv_interpreter(BASE_COMMAND),
                         "a base interpreter is fine, even when the target is .venv")

    def test_it_judges_the_interpreter_not_the_target(self):
        self.assertTrue(_is_foreign_venv_interpreter(
            r"C:\somewhere\venv\Scripts\python.exe"),
            "a bare venv interpreter with no target at all is still foreign")


class TestInstallerRefusesForeignVenv(unittest.TestCase):
    def test_powershell_installer_requires_a_base_python(self):
        src = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("sys.prefix == sys.base_prefix", src,
                      "install.ps1 must not build Tacit's venv from another "
                      "project's virtualenv -- that is how pyvenv.cfg ends up "
                      "naming an interpreter Tacit does not own")

    def test_shell_installer_requires_a_base_python(self):
        src = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("sys.prefix == sys.base_prefix", src,
                      "install.sh has the same trap and the same fix")

    def test_neither_installer_says_a_venv_is_a_base_python(self):
        """The probes must reject, not merely detect: a version check that also
        passes for a venv is the bug, not a mitigation."""
        for name in ("install.ps1", "install.sh"):
            src = (ROOT / name).read_text(encoding="utf-8")
            for line in src.splitlines():
                if "version_info >= (3,10)" in line:
                    self.assertIn("sys.prefix == sys.base_prefix", line,
                                  "%s still accepts any python on PATH" % name)


class TestVenvProvenance(unittest.TestCase):
    def setUp(self):
        self.cfg = ROOT / ".venv" / "pyvenv.cfg"

    def test_the_venv_does_not_name_a_foreign_interpreter(self):
        if not self.cfg.exists():
            self.skipTest("no .venv here (installed elsewhere, or not created yet)")
        text = self.cfg.read_text(encoding="utf-8")
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip() not in ("executable", "command", "home"):
                continue
            if _is_foreign_venv_interpreter(value):
                self.fail("pyvenv.cfg %s names %r -- an interpreter inside another "
                          "project's virtualenv. Tacit must be built by a base "
                          "Python so uninstalling that project cannot reach it."
                          % (key.strip(), value.strip()))

    def test_the_running_interpreter_is_not_another_projects_venv(self):
        import sys
        base = str(getattr(sys, "_base_executable", "") or "")
        self.assertFalse(_is_foreign_venv_interpreter(base),
                         "Tacit is running on an interpreter owned by another "
                         "project: %s" % base)


if __name__ == "__main__":
    unittest.main()

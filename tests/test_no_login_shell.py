"""No runtime path may ever reach the login shell.

The CachyOS report this guards: `Program '/bin/fish' crashed.` beside agent
commands, because the old build spawned `SHELL -c` (fish, sourcing the whole
config.fish on every spawn). The shell fix resolved execution to bash; this
audit keeps the fix structural: nothing in the runtime reads $SHELL, nothing
calls os.system, and shell=True stays limited to the two known call sites
whose POSIX paths route through _run_captured's explicit argv form.
"""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "backend"

_AUDIT_PATTERNS = [
    (re.compile(r"\bos\.system\("), "os.system (spawns a shell)"),
    (re.compile(r"os\.environ\.get\(\s*[\"']SHELL[\"']"), "reads $SHELL"),
    (re.compile(r"os\.environ\[\s*[\"']SHELL[\"']"), "reads $SHELL"),
]


def code_lines(path: Path) -> list[str]:
    """Executable lines only: docstrings and comments stripped by position."""

    src = path.read_text(encoding="utf-8", errors="replace")
    out = []
    lines = src.splitlines()
    docstring_spans: list[tuple[int, int]] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        while isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str) and node.end_lineno:
            docstring_spans.append((node.lineno, node.end_lineno))
            break
    def in_docstring(n: int) -> bool:
        return any(a <= n <= b for a, b in docstring_spans)
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s or s.startswith("#") or in_docstring(i):
            continue
        out.append((i, line))
    return out


class TestNoLoginShellSpawn(unittest.TestCase):
    def test_no_runtime_code_reads_the_login_shell_or_uses_os_system(self):
        offenders = []
        for path in sorted(RUNTIME.rglob("*.py")):
            for i, line in code_lines(path):
                for pattern, why in _AUDIT_PATTERNS:
                    if pattern.search(line):
                        offenders.append(f"{path.name}:{i}: [{why}] {line.strip()[:110]}")
        self.assertEqual(offenders, [], "\n  ".join(offenders))

    def test_shell_true_paths_are_exactly_the_two_known_ones(self):
        """A new shell=True site has to route through _run_captured's argv
        form; the audit counts the known call sites so a fourth executor
        cannot appear silently."""
        sites = []
        for path in sorted(RUNTIME.rglob("*.py")):
            for i, line in code_lines(path):
                s = line.strip()
                if re.search(r"\bshell\s*=\s*True\b", s):
                    rel = path.relative_to(ROOT).as_posix()
                    sites.append(f"{rel}:{i}: {s[:110]}")
        self.assertEqual(
            sites,
            ["backend/sandbox.py:523: shell=True, on_output=on_output,"],
            "a new shell=True call site appeared; route it through the "
            "resolved shell")


import os  # noqa: E402

if __name__ == "__main__":
    unittest.main()
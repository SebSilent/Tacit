"""The test suite runs turns on the thread path.

Stage 3 made the process-per-turn worker the default, and a worker subprocess
cannot see a fake engine patched into this process — every engine-faking test
would spawn a real subprocess against the real `~/.tacit` and hang waiting for
events that never come. So the suite pins the escape hatch before any test
module is imported: `TACIT_TURN_WORKER=thread` restores the in-process path
the tests were written against.

The stage-3 tests that exercise the worker itself override this explicitly and
drive a real subprocess with a scripted transcript, so the process path is
still tested — just not by the suites that fake the engine.
"""
import os

os.environ.setdefault("TACIT_TURN_WORKER", "thread")
"""Shared helpers for the Tacit test suite."""

from pathlib import Path


def state_paths(config) -> tuple:
    """Every config path that lives under the user's home, derived not listed.

    A hand-written list drifts. The sessions directory was missing from it, so
    tests that believed they were isolated wrote fixture sessions into the real
    ~/.tacit/sessions, and the interface then showed them as the user's own.

    Deriving from the config module means a new state path cannot be forgotten.
    Paths outside the home (the project root, the static directory, bundled
    assets) are deliberately excluded, because tests must be able to read those.
    """
    home = Path(config.HOME)
    out = []
    for key, value in vars(config).items():
        if not key.isupper() or key == "HOME" or not isinstance(value, Path):
            continue
        try:
            value.relative_to(home)
        except ValueError:
            continue
        out.append(key)
    return tuple(sorted(out))


def drain_turns(timeout: float = 5.0) -> None:
    """Finish every turn driver thread before the test gives its state paths back.

    `state_paths` redirects the store by setting module attributes, and the
    teardown puts them back. `store._path` resolves a session's path at write
    time (backend/store.py:9-21, the house rule that a cached path once made a
    redirected test write into the real ~/.tacit). Those two facts combine
    badly: a driver thread still running at teardown writes its epilogue:
    `store.save`, then `_autonomy_bump` → `store.patch` (backend/session_runtime.py:761-783),
    which lands after the restore, in the real home. `store.save` also re-appends
    a missing index row (backend/store.py:144), so the leak is not one stray
    file: it re-lists the session, the interface serves it, and a person who
    deleted it watches it come back every time the suite runs.

    So the drain runs first, and it is a cleanup rather than a wait in the test
    body: a test that asserts on a turn it left running is exactly the one that
    would otherwise leak.
    """
    import time
    from backend import session_runtime, session_state
    session_runtime.shutdown()
    deadline = time.monotonic() + timeout
    while session_state.all_states() and time.monotonic() < deadline:
        time.sleep(0.02)
    session_state.reset()

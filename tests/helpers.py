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

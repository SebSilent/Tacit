"""Clicking CHAT/AGENT changes the mode of the session you are looking at. It must
never start a session.

Stage 1b put a `client_new_session` send inside the mode handler on the stated ground
that the transcript could not cross a mode boundary. It can: that is what the app did
before Stage 1b, and it worked. So the requirement is purely subtractive, and the
regression is exactly one line - if anybody re-adds a creation call to this handler,
the selector stops being a selector and every mode click throws away your transcript.

The client half is only correct because of a server half, and both are pinned here.
A socket holds a `rec` it read when it opened; switching in place means that copy is
minutes old by the time you type, so the turn asks the disk which mode it is in
(`session_runtime.py`, the `rec["mode"] = disk["mode"]` copy-over, in a read the
approved-plan pointer now shares). Pin the client without pinning that and the click
looks like it worked while the next turn runs on the old mode.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
RUNTIME = (ROOT / "backend" / "session_runtime.py").read_text(encoding="utf-8")
API = (ROOT / "backend" / "routers" / "api.py").read_text(encoding="utf-8")

# Anything in the handler that would put a new session on the page.
CREATION = ("client_new_session", "new_session", "adoptSession(", "newSession(", "create=1")


def mode_handler(js):
    """The body of the `#modeSeg` click handler, braces balanced."""
    i = js.index("$('#modeSeg').addEventListener('click'")
    j = js.index("{", i)
    depth = 0
    for k in range(j, len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[j:k + 1]
    raise AssertionError("unterminated mode handler")


def creates_a_session(js):
    return any(tok in mode_handler(js) for tok in CREATION)


def mode_is_set_in_place(js):
    body = mode_handler(js)
    return ("s.mode = btn.dataset.mode;" in body
            and "syncSessionMetadata(s)" in body
            and "localStorage.setItem('tacit.mode', s.mode);" in body)


def toast_promises_no_new_session(js):
    return "fresh session" not in mode_handler(js).lower()


def mode_comes_from_disk(src):
    """The turn's mode comes from a read it does here, not from the socket's copy.

    Anchored on the copy-over, not on `disk = store.get(sid) or {}` alone: that line
    also appears in the assistant path, so pinning it would pass even if the turn
    stopped taking the disk's mode. One read now serves the mode *and* the
    approved-plan pointer, which is why the anchor is the assignment and not the old
    single-purpose `disk_mode = ...` line.
    """
    return ('if disk.get("mode"):' in src
            and 'rec["mode"] = disk["mode"]' in src)


class TestModeSwitchInPlace(unittest.TestCase):

    def test_a_mode_change_never_creates_a_session(self):
        self.assertFalse(creates_a_session(APP),
                         "the mode selector is starting sessions again")

    def test_the_mode_is_set_on_the_session_in_view(self):
        self.assertTrue(mode_is_set_in_place(APP),
                        "the click no longer sets and lands the mode itself")

    def test_the_toast_does_not_promise_a_session_that_was_not_made(self):
        self.assertTrue(toast_promises_no_new_session(APP),
                        "the copy still claims a fresh session")

    def test_the_next_turn_asks_the_disk_which_mode_it_is_in(self):
        self.assertTrue(mode_comes_from_disk(RUNTIME),
                        "the socket's stale record is being trusted again")

    def test_the_metadata_endpoint_carries_the_mode(self):
        self.assertIn('mode=body.get("mode")', API,
                      "the click has nowhere to land the mode")

    def test_the_guards_can_fail(self):
        plant = APP.replace(
            "  toast(s.mode === 'agent' ? 'Agent mode: tools on'",
            "  if (ws && ws.readyState === 1) ws.send(JSON.stringify("
            "{ type: 'client_new_session', model: s.model, mode: s.mode }));\n"
            "  toast(s.mode === 'agent' ? 'Agent mode: tools on'", 1)
        self.assertNotEqual(plant, APP, "plant 1 did not land")
        self.assertTrue(creates_a_session(plant),
                        "the creation guard cannot see a re-added send")

        # Back to the bug: the socket's own minutes-old copy decides the mode.
        plant2 = RUNTIME.replace('rec["mode"] = disk["mode"]',
                                 'rec["mode"] = rec.get("mode")', 1)
        self.assertNotEqual(plant2, RUNTIME, "plant 2 did not land")
        self.assertFalse(mode_comes_from_disk(plant2),
                         "the disk guard passes on a socket that reads its own copy")


if __name__ == "__main__":
    unittest.main()

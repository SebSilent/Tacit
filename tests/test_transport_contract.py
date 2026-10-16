"""The wire contract, pinned against the server rather than against the page.

`static/transport.js` owns the socket URL, the event names and the command names, and
`static/app.js` reads them instead of keeping a second copy. The failure this guards is
the quiet one: a name that drifts from the backend's own literals leaves a listener
waiting for an event that is never sent, or a command the dispatch silently drops. So
this file checks the contract against the only authority there is - the backend's
literals - not against the page that uses it.

Deliberately DOM-free: no browser, no node, no page loaded. It reads source text and the
server's published types, which is what makes it able to run in CI at all.

(Its predecessor arrived with the `/mobile` page, which is gone. The contract did not:
one page still reads it, and the pin is what keeps that honest.)
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSPORT = (ROOT / "static" / "transport.js").read_text(encoding="utf-8")
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CHAT = (ROOT / "backend" / "routers" / "chat.py").read_text(encoding="utf-8")
BACKEND = "".join(p.read_text(encoding="utf-8", errors="replace")
                  for p in (ROOT / "backend").rglob("*.py"))


def names(js, const):
    m = re.search(r"const " + const + r" = \[(.*?)\];", js, re.S)
    assert m, f"{const} is not declared as a plain array in transport.js"
    return re.findall(r"'([a-z_]+)'", m.group(1))


EVENTS = names(TRANSPORT, "EVENTS")
COMMANDS = names(TRANSPORT, "COMMANDS")

# Every kind chat.py actually dispatches a socket message on.
DISPATCHED = (set(re.findall(r'kind ==\s*"([a-z_]+)"', CHAT))
              | {k for g in re.findall(r'kind in \(([^)]*)\)', CHAT)
                 for k in re.findall(r'"([a-z_]+)"', g)})


def code(src):
    """Source with the JS comments removed.

    A guard that trips on *prose about* the rule teaches people to write worse comments
    instead of better code - an earlier draft of this file failed on a comment that
    quoted the very string it was forbidding. So the scan reads code and only code.
    """
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"//[^\n]*", "", src)


class TestProtocolIsOneContract(unittest.TestCase):

    def test_the_lists_are_not_empty(self):
        """Anti-vacuity: a regex that found no array would make every test below pass."""
        self.assertGreaterEqual(len(EVENTS), 15, f"only {len(EVENTS)} events parsed")
        self.assertGreaterEqual(len(COMMANDS), 10, f"only {len(COMMANDS)} commands parsed")

    def test_every_event_the_contract_declares_is_published(self):
        missing = [e for e in EVENTS
                   if not re.search(r'"type":\s*"' + re.escape(e) + r'"', BACKEND)]
        self.assertEqual(missing, [],
                         f"transport.js promises events no backend code publishes: {missing}")

    def test_every_command_the_contract_declares_is_dispatched(self):
        missing = [c for c in COMMANDS if c not in DISPATCHED]
        self.assertEqual(missing, [],
                         f"transport.js offers commands chat.py never handles: {missing}")

    def test_the_two_names_that_look_right_are_not_in_the_contract(self):
        """`trace` is the turn worker's stdout event, folded into text/reason before it
        reaches a socket. `proc_exit` is a dead branch in app.js - nothing in backend/
        emits it. Both were in the first draft of the contract and both are wrong."""
        self.assertNotIn("trace", EVENTS,
                         "a page listening for `trace` would wait forever")
        self.assertNotIn("proc_exit", EVENTS)
        self.assertNotIn("image", COMMANDS,
                         "`image` is an attachment shape inside a prompt, not a command")

    def test_the_page_builds_no_socket_url_of_its_own(self):
        self.assertNotIn("/ws/", code(APP),
                         "app.js is assembling a websocket path again")
        self.assertIn("function wsUrl(s)", TRANSPORT,
                      "the shared builder moved out of transport.js")

    def test_the_page_loads_the_contract_before_its_own_code(self):
        t, m = INDEX_HTML.find("transport.js"), INDEX_HTML.find("markdown.js")
        self.assertGreater(t, 0, "index.html does not load transport.js")
        self.assertGreater(m, 0, "index.html does not load markdown.js")
        self.assertLess(min(t, m), INDEX_HTML.find("app.js?v="),
                        "index.html runs app.js before the modules it reads")

    def test_the_page_delegates_rather_than_keeping_copies(self):
        self.assertIn("window.TacitTransport.wsUrl(s)", APP)
        self.assertIn("window.TacitTransport.uuid()", APP)
        self.assertIn("window.TacitMarkdown.md(src)", APP)
        self.assertNotIn("const TBL_SEP", APP, "a second renderer grew back in app.js")

    def test_the_workspace_is_normalised_before_the_socket_opens(self):
        """The registry row calls it `project`; wsUrl sends `workdir`, and the server
        reads a missing workdir as *leave alone* but an empty one as *clear it*.
        Connecting a raw registry row would wipe the session's workspace."""
        self.assertIn("s.workdir || s.project", APP,
                      "app.js connects without normalising project -> workdir")


class TestTheGuardHasTeeth(unittest.TestCase):

    def test_a_stale_event_name_is_caught(self):
        bad = TRANSPORT.replace("'reason', 'done',", "'reason', 'done', 'made_up_event',", 1)
        self.assertNotEqual(bad, TRANSPORT, "plant 1 did not land")
        pub = [e for e in re.findall(r"'([a-z_]+)'",
                 re.search(r"const EVENTS = \[(.*?)\];", bad, re.S).group(1))
               if not re.search(r'"type":\s*"' + re.escape(e) + '"', BACKEND)]
        self.assertIn("made_up_event", pub, "an invented event slipped through")

    def test_a_command_the_server_dropped_is_caught(self):
        bad = CHAT.replace('kind == "compact"', 'kind == "compact2"', 1)
        self.assertNotEqual(bad, CHAT, "plant 2 did not land")
        dispatched = {k for k in re.findall(r'kind ==\s*"([a-z_]+)"', bad)}
        self.assertNotIn("compact", dispatched,
                         "the command list would still promise a dropped command")

    def test_a_page_that_builds_its_own_url_is_caught(self):
        bad = APP.replace("window.TacitTransport.wsUrl(s)",
                          'x = "/ws/" + s.id; window.TacitTransport.wsUrl(s)', 1)
        self.assertNotEqual(bad, APP, "plant 3 did not land")
        self.assertIn("/ws/", code(bad), "the /ws/ scan cannot see a hand-built socket URL")


if __name__ == "__main__":
    unittest.main()

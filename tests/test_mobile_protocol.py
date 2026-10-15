"""The wire contract, pinned against the server rather than against either page.

PLAN item 8's "Done when" asks for exactly this: *a protocol test that neither frontend
can break by accident*. Two frontends that each re-type the socket URL, the event names
and the command names drift within a week, and the drift is invisible until a turn
half-works on one device. So the contract lives in `static/transport.js`, and this file
checks it against the only authority there is - the backend's own literals.

Deliberately DOM-free: no browser, no node, no page loaded. It reads source text and the
server's published types, which is what makes it able to run in CI at all.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSPORT = (ROOT / "static" / "transport.js").read_text(encoding="utf-8")
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
MOBILE = (ROOT / "static" / "mobile.js").read_text(encoding="utf-8")
MOBILE_HTML = (ROOT / "static" / "mobile.html").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
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

    Not decoration: the first run of this file failed because a *comment* in mobile.js
    said "nothing in this file builds a `/ws/` string". A guard that trips on prose about
    the rule teaches people to write worse comments instead of better code, so the scan
    reads code and only code.
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
        emits it. Both were in the first draft of this file and both are wrong."""
        self.assertNotIn("trace", EVENTS,
                         "a page listening for `trace` would wait forever")
        self.assertNotIn("proc_exit", EVENTS)
        self.assertNotIn("image", COMMANDS,
                         "`image` is an attachment shape inside a prompt, not a command")

    def test_neither_page_builds_a_socket_url(self):
        for label, src in (("app.js", APP), ("mobile.js", MOBILE)):
            self.assertNotIn("/ws/", code(src),
                             f"{label} is assembling a websocket path again")
        self.assertIn("function wsUrl(s)", TRANSPORT,
                      "the shared builder moved out of transport.js")

    def test_both_pages_load_the_contract_before_their_own_code(self):
        for label, html in (("index.html", INDEX_HTML), ("mobile.html", MOBILE_HTML)):
            t, m = html.find("transport.js"), html.find("markdown.js")
            self.assertGreater(t, 0, f"{label} does not load transport.js")
            self.assertGreater(m, 0, f"{label} does not load markdown.js")
            last = max(html.find("app.js?v="), html.find("mobile.js?v="))
            self.assertLess(min(t, m), last,
                            f"{label} loads a page script before the contract it reads")

    def test_the_desktop_page_delegates_rather_than_keeping_copies(self):
        self.assertIn("window.TacitTransport.wsUrl(s)", APP)
        self.assertIn("window.TacitTransport.uuid()", APP)
        self.assertIn("window.TacitMarkdown.md(src)", APP)
        self.assertNotIn("const TBL_SEP", APP, "a second renderer grew back in app.js")


class TestMobileFollowsTheSameRules(unittest.TestCase):
    """The mobile page is new, so every rule it inherits from a desktop fix is pinned
    here once, rather than assumed to be obvious to whoever writes the next screen."""

    def test_the_workspace_is_normalised_before_the_socket_opens(self):
        """The registry row calls it `project`; wsUrl sends `workdir`, and the server
        reads a missing workdir as *leave alone* but an empty one as *clear it*.
        Connecting a raw registry row would wipe the session's workspace."""
        for label, src in (("app.js", APP), ("mobile.js", MOBILE)):
            self.assertIn("s.workdir || s.project", src,
                          f"{label} connects without normalising project -> workdir")

    def test_a_mode_change_on_mobile_never_creates_a_session(self):
        """Item 2 applies to both frontends. The mobile page must not rediscover the
        Stage 1b trick of switching by starting over."""
        self.assertNotIn("client_new_session", MOBILE)
        body = MOBILE[MOBILE.index("async function setMode"):]
        body = body[:body.index("\n  }")]
        for forbidden in ("_new", "create=1", "newSession("):
            self.assertNotIn(forbidden, body,
                             f"the mobile mode switch is creating sessions ({forbidden})")

    def test_the_new_session_action_is_the_only_create_path(self):
        self.assertIn("_new: true", MOBILE,
                      "nothing marks a page-made session, so nothing may send create=1")
        self.assertEqual(MOBILE.count("_new: true"), 1,
                         "a second create path appeared on the mobile page")

    def test_timestamps_are_drawn_permanently_not_on_hover(self):
        """Item 5's decision is load-bearing on touch: a time behind a pointer is a
        time nobody on a phone can read."""
        self.assertIn("timeOf(msg.ts)", MOBILE)
        self.assertNotIn(":hover .m-time", (ROOT / "static" / "mobile.html")
                         .read_text(encoding="utf-8"))

    def test_user_text_never_enters_innerhtml(self):
        """md() is the sanctioned sink for model text. The person's own words are
        drawn as text, and this stops a future edit from routing them through HTML."""
        self.assertIn("body.textContent = msg.content", MOBILE)


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
        bad = MOBILE.replace("sock = T.openSocket(s, {",
                             'sock = new WebSocket("/ws/" + s.id); sock2 = T.openSocket(s, {', 1)
        self.assertNotEqual(bad, MOBILE, "plant 3 did not land")
        self.assertIn("/ws/", bad, "the /ws/ scan cannot see a hand-built socket URL")


if __name__ == "__main__":
    unittest.main()

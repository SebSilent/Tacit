"""The message-array invariant at the transport boundary.

A llama.cpp server with a `{%- if not loop.first %}{{- raise_exception('System
message must be at the beginning.') }}` chat template answers HTTP 500 on
position alone: any system message at an index > 0, or a second one anywhere,
fails the whole request in template rendering, before the model is consulted.
The array is normalized once, at `backend.ai.engine`, so every caller (turn
loop, summariser, Assistant, evaluator) is covered.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.ai import engine


def _system_rows(arr):
    return [(i, m.get("role")) for i, m in enumerate(arr) if m.get("role") == "system"]


class NormalizerShapes(unittest.TestCase):
    """The measured matrix, plus the coercions."""

    def test_system_first_stays_untouched(self):
        inbox = [{"role": "system", "content": "s"},
                 {"role": "user", "content": "u"}]
        out = engine.normalize_messages(inbox)
        self.assertEqual(out, inbox, "an already-valid shape must not change")

    def test_user_first_hoists_the_system_to_zero(self):
        out = engine.normalize_messages([{"role": "user", "content": "hi"},
                                         {"role": "system", "content": "x"}])
        self.assertEqual(_system_rows(out), [(0, "system")])
        self.assertEqual(out[1], {"role": "user", "content": "hi"})
        self.assertEqual(out[0]["content"], "x")

    def test_two_system_rows_merge_in_arrival_order(self):
        out = engine.normalize_messages([
            {"role": "system", "content": "a"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "m"},
            {"role": "user", "content": "u2"},
            {"role": "system", "content": "b"}])
        self.assertEqual(_system_rows(out), [(0, "system")])
        self.assertEqual(out[0]["content"], "a\n\nb")
        self.assertEqual([m["role"] for m in out[1:]],
                         ["user", "assistant", "user"])

    def test_identical_system_fragments_dedupe(self):
        out = engine.normalize_messages([
            {"role": "system", "content": "same"},
            {"role": "user", "content": "u"},
            {"role": "system", "content": "same"}])
        self.assertEqual(out[0]["content"], "same")
        rows = [m for m in out if m["role"] == "system"]
        self.assertEqual(len(rows), 1)

    def test_tool_pairing_survives_the_collapse(self):
        calls = [{"id": "c1", "type": "function",
                  "function": {"name": "t", "arguments": "{}"}}]
        inbox = [{"role": "system", "content": "s"},
                 {"role": "user", "content": "u"},
                 {"role": "assistant", "content": "", "tool_calls": calls},
                 {"role": "tool", "tool_call_id": "c1", "content": "r"},
                 {"role": "user", "content": "next"}]
        out = engine.normalize_messages(inbox)
        self.assertEqual(_system_rows(out), [(0, "system")])
        self.assertEqual([m["role"] for m in out[1:]],
                         ["user", "assistant", "tool", "user"])
        self.assertEqual(out[2]["tool_calls"], calls)

    def test_developer_maps_to_system(self):
        out = engine.normalize_messages([{"role": "developer", "content": "d"}])
        self.assertEqual(out, [{"role": "system", "content": "d"}])

    def test_an_unknown_role_is_carried_as_user_not_dropped(self):
        out = engine.normalize_messages([{"role": "carrier-pigeon", "content": "x"}])
        self.assertEqual(out, [{"role": "user", "content": "x"}])

    def test_empty_array_is_refused_before_any_socket(self):
        with self.assertRaises(engine.EngineError) as cm:
            engine._pre([])
        self.assertIn("No messages", str(cm.exception))

    def test_normalization_is_idempotent(self):
        once = engine.normalize_messages([{"role": "user", "content": "u"},
                                          {"role": "system", "content": "x"},
                                          {"role": "system", "content": "x"}])
        again = engine.normalize_messages(once)
        self.assertEqual(once, again)


class TemplateRender(unittest.TestCase):
    """The exact raise the user measured, reproduced with a mirror template."""

    TEMPLATE = """{%- for message in messages -%}
{%- if message.role == 'system' and not loop.first -%}
{{- raise_exception('System message must be at the beginning.') }}
{%- endif -%}
{%- if message.role not in ['system', 'user', 'assistant', 'tool'] -%}
{{- raise_exception('Unexpected message role.') }}
{%- endif -%}
{%- endfor -%}
{%- if messages|length == 0 -%}
{{- raise_exception('No messages provided.') }}
{%- endif -%}
OK"""

    def setUp(self):
        try:
            import jinja2
        except ImportError:
            self.skipTest("jinja2 is not installed")
        from jinja2 import Environment

        def boom(msg):
            raise ValueError(str(msg))

        self.env = Environment()
        self.env.globals["raise_exception"] = boom
        self.tpl = self.env.from_string(self.TEMPLATE)

    def _render(self, messages):
        return self.tpl.render(messages=messages)

    def test_the_measured_failure_shape_raises_without_the_normalizer(self):
        with self.assertRaises(ValueError) as cm:
            self._render([{"role": "user", "content": "hi"},
                          {"role": "system", "content": "x"}])
        self.assertIn("System message must be at the beginning", str(cm.exception))

    def test_every_normalized_shape_renders_clean(self):
        shapes = [
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            [{"role": "user", "content": "u"}, {"role": "system", "content": "x"}],
            [{"role": "system", "content": "a"}, {"role": "user", "content": "u1"},
             {"role": "assistant", "content": "m"}, {"role": "user", "content": "u2"},
             {"role": "system", "content": "b"}],
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
             {"role": "assistant", "content": "", "tool_calls": [
                 {"id": "c1", "type": "function",
                  "function": {"name": "t", "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "c1", "content": "r"},
             {"role": "user", "content": "next"}],
            [{"role": "developer", "content": "d"}],
        ]
        for shape in shapes:
            with self.subTest(shape=shape):
                out = engine.normalize_messages(shape)
                self.assertEqual(self._render(out), "OK")


class TransportCapture(unittest.TestCase):
    """What actually goes over the wire."""

    def setUp(self):
        self.captured: list = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.captured.append(json.loads(self.rfile.read(length) or b"{}"))
                self.send_response(200)
                payload = json.dumps({
                    "choices": [{"message": {"role": "assistant",
                                             "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                              "total_tokens": 2}}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        self._orig_target = engine._target
        engine._target = lambda ref=None: {
            "model": "m", "ref": "p/m",
            "baseUrl": f"http://127.0.0.1:{self.port}/v1",
            "apiKey": "", "provider": "p"}
        self.addCleanup(setattr, engine, "_target", self._orig_target)

    def test_the_wire_never_carries_a_mid_list_system_row(self):
        engine.chat([{"role": "user", "content": "u1"},
                     {"role": "system", "content": "late"},
                     {"role": "system", "content": "late"}],
                    ref="p/m")
        msgs = self.captured[0]["messages"]
        self.assertEqual([m["role"] for m in msgs if m["role"] == "system"],
                         ["system"])
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "late")
        self.assertEqual(msgs[1:], [{"role": "user", "content": "u1"}])


class GatedRetry(unittest.TestCase):
    """One resend for the template 500, then surfaced with the body."""

    def setUp(self):
        self._orig_target = engine._target
        self.addCleanup(setattr, engine, "_target", self._orig_target)
        self.hits = {"n": 0}
        outer = self
        self.replies: list = []
        replies = self.replies

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.hits["n"] += 1
                status, payload = (replies.pop(0) if replies
                                   else (500, b"{}"))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        engine._target = lambda ref=None: {
            "model": "m", "ref": "p/m",
            "baseUrl": f"http://127.0.0.1:{self.port}/v1",
            "apiKey": "", "provider": "p"}

    def test_a_template_500_resends_once_and_succeeds(self):
        ok = json.dumps({"choices": [{"message": {"role": "assistant",
                                                  "content": "ok"},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                   "total_tokens": 2}}).encode()
        self.replies.extend([
            (500, b'{"error": {"message": "System message must be at the '
                  b'beginning."}}'),
            (200, ok)])
        res = engine.chat([{"role": "user", "content": "u"},
                           {"role": "system", "content": "late"}], ref="p/m")
        self.assertEqual(res["content"], "ok")
        self.assertEqual(self.hits["n"], 2, "one resend, not a loop")

    def test_a_second_template_500_is_surfaced_with_the_body(self):
        self.replies.extend([
            (500, b'{"error": {"message": "System message must be at the '
                  b'beginning."}}'),
            (500, b'{"error": {"message": "System message must be at the '
                  b'beginning."}}')])
        with self.assertRaises(engine.EngineError) as cm:
            engine.chat([{"role": "user", "content": "u"}], ref="p/m")
        self.assertIn("after normalization", str(cm.exception))
        self.assertIn("beginning", str(cm.exception))
        self.assertEqual(self.hits["n"], 2, "surfaced, never looped")

    def test_other_500s_are_not_retried(self):
        self.replies.extend([(500, b'{"error": {"message": "internal boom"}}')])
        with self.assertRaises(engine.EngineError) as cm:
            engine.chat([{"role": "user", "content": "u"}], ref="p/m")
        self.assertNotIn("after normalization", str(cm.exception))
        self.assertEqual(self.hits["n"], 1)


if __name__ == "__main__":
    unittest.main()

"""Tests for the provider wire protocol and the WebSocket contract.

These are the two layers that had no coverage at all. `ai/engine.py` is the only
code that touches a provider's actual bytes, and every quirk it absorbs — reasoning
under three different keys, tool-call arguments arriving in fragments, a usage
chunk that comes after [DONE] — was untested, so any of them could regress
silently. The WebSocket is the only path the interface has into a turn.

Both are driven against fakes: no test here reaches a network or a real model.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import anthropic, config, thinking  # noqa: E402
from backend.ai import engine  # noqa: E402
from tests.helpers import state_paths  # noqa: E402


def sse(*payloads) -> list[str]:
    """Build an SSE body from dicts, the way a provider sends one."""
    out = []
    for p in payloads:
        out.append("data: " + json.dumps(p))
        out.append("")          # providers blank-line separate events
    out.append("data: [DONE]")
    return out


def delta(content=None, reasoning=None, tool_calls=None, finish=None):
    d = {}
    if content is not None:
        d["content"] = content
    if reasoning is not None:
        d.update(reasoning)
    if tool_calls is not None:
        d["tool_calls"] = tool_calls
    choice = {"index": 0, "delta": d}
    if finish:
        choice["finish_reason"] = finish
    return {"choices": [choice]}


def tc(index, id=None, name=None, arguments=None):
    fn = {}
    if name is not None:
        fn["name"] = name
    if arguments is not None:
        fn["arguments"] = arguments
    return {"index": index, **({"id": id} if id else {}), "function": fn}


class FakeResponse:
    def __init__(self, lines, status=200, body=b""):
        self._lines, self.status_code, self._body = lines, status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        return self._body


class FakeClient:
    """Stands in for httpx.Client and records the payload that was sent."""

    sent = []

    def __init__(self, lines=None, status=200, body=b""):
        self._lines, self._status, self._body = lines or [], status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        FakeClient.sent.append(json)
        return FakeResponse(self._lines, self._status, self._body)


def install(lines, status=200, body=b""):
    """Point engine.stream_chat at a canned SSE stream; returns recorded payloads."""
    FakeClient.sent = []
    original = engine.httpx.Client
    engine.httpx.Client = lambda **kw: FakeClient(lines, status, body)
    return original


def model(reasoning=False, window=0, max_tokens=0, provider="p"):
    return {"ref": "p/m", "provider": provider, "model": "m", "baseUrl": "http://x",
            "apiKey": "", "contextWindow": window, "maxTokens": max_tokens,
            "reasoning": reasoning, "thinkingValues": [], "thinkingDefault": None}


class EngineStreamTest(unittest.TestCase):
    def setUp(self):
        self._resolve = config.resolve_model
        config.resolve_model = lambda ref=None: model()
        self._send = thinking.send
        thinking.send = lambda ref, level: None
        self.addCleanup(setattr, config, "resolve_model", self._resolve)
        self.addCleanup(setattr, thinking, "send", self._send)
    def run_stream(self, lines, status=200, body=b"", **kwargs):
        original = engine.httpx.Client
        install(lines, status, body)
        self.addCleanup(setattr, engine.httpx, "Client", original)
        return list(engine.stream_chat([{"role": "user", "content": "hi"}], **kwargs))

    # ── content and reasoning ────────────────────────────────────────────
    def test_text_deltas_are_forwarded_in_order(self):
        events = self.run_stream(sse(delta("Hello"), delta(", world"),
                                   delta(None, finish="stop")))
        text = "".join(e["delta"] for e in events if e["type"] == "text")
        self.assertEqual(text, "Hello, world")

    def test_all_three_reasoning_key_spellings_are_recognised(self):
        # Providers disagree: reasoning_content, reasoning, thinking.
        for key in ("reasoning_content", "reasoning", "thinking"):
            with self.subTest(key=key):
                events = self.run_stream(
                    sse(delta(None, {key: "because "}), delta(None, {key: "reasons"}),
                        delta(None, finish="stop")))
                reason = "".join(e["delta"] for e in events if e["type"] == "reason")
                self.assertEqual(reason, "because reasons")

    def test_empty_strings_are_not_forwarded_as_deltas(self):
        events = self.run_stream(sse(delta(""), delta("x"), delta(None, finish="stop")))
        self.assertEqual([e["delta"] for e in events if e["type"] == "text"], ["x"])

    # ── tool calls ───────────────────────────────────────────────────────
    def test_fragmented_tool_call_arguments_are_reassembled(self):
        # Arguments arrive split across chunks; the index is what joins them.
        events = self.run_stream(sse(
            delta(None, tool_calls=[tc(0, id="call_1", name="read_file",
                                       arguments='{"path": "a')]),
            delta(None, tool_calls=[tc(0, arguments='.py", "offset":')]),
            delta(None, tool_calls=[tc(0, arguments=" 10}")]),
            delta(None, finish="tool_calls")))
        calls = next(e["calls"] for e in events if e["type"] == "tool_calls")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "call_1")
        self.assertEqual(calls[0]["name"], "read_file")
        self.assertEqual(json.loads(calls[0]["arguments"]),
                         {"path": "a.py", "offset": 10})

    def test_parallel_tool_calls_stay_separate_and_ordered(self):
        events = self.run_stream(sse(
            delta(None, tool_calls=[tc(1, id="b", name="list_files", arguments="{}")]),
            delta(None, tool_calls=[tc(0, id="a", name="read_file", arguments="{}")]),
            delta(None, finish="tool_calls")))
        calls = next(e["calls"] for e in events if e["type"] == "tool_calls")
        self.assertEqual([c["id"] for c in calls], ["a", "b"])

    def test_a_name_split_across_chunks_is_joined(self):
        events = self.run_stream(sse(
            delta(None, tool_calls=[tc(0, id="a", name="read_")]),
            delta(None, tool_calls=[tc(0, name="file", arguments="{}")]),
            delta(None, finish="tool_calls")))
        calls = next(e["calls"] for e in events if e["type"] == "tool_calls")
        self.assertEqual(calls[0]["name"], "read_file")

    def test_a_call_with_no_id_still_gets_one(self):
        # An id-less call would break the transcript: every tool result is keyed
        # by its call id, so a missing one cannot be paired back up.
        events = self.run_stream(sse(
            delta(None, tool_calls=[tc(0, name="read_file", arguments="{}")]),
            delta(None, finish="tool_calls")))
        calls = next(e["calls"] for e in events if e["type"] == "tool_calls")
        self.assertTrue(calls[0]["id"])

    # ── usage, finish, framing ───────────────────────────────────────────
    def test_usage_is_surfaced_even_when_it_arrives_after_the_last_choice(self):
        lines = sse(delta("hi"), delta(None, finish="stop"))
        lines.insert(-1, "data: " + json.dumps(
            {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2,
                                      "total_tokens": 13}}))
        events = self.run_stream(lines)
        usage = next(e["usage"] for e in events if e["type"] == "usage")
        self.assertEqual(usage, {"input": 11, "output": 2, "total": 13,
                                 "cache_read": 0, "cache_write": 0})

    def test_missing_usage_reports_zeros_rather_than_nothing(self):
        events = self.run_stream(sse(delta("hi"), delta(None, finish="stop")))
        usage = next(e["usage"] for e in events if e["type"] == "usage")
        self.assertEqual(usage, {"input": 0, "output": 0, "total": 0,
                                 "cache_read": 0, "cache_write": 0})

    def test_finish_reason_and_model_are_on_the_done_event(self):
        events = self.run_stream(sse(delta("hi"), delta(None, finish="length")))
        done = next(e for e in events if e["type"] == "done")
        self.assertEqual(done["finish"], "length")
        self.assertEqual(done["model"]["ref"], "p/m")

    def test_a_malformed_chunk_is_skipped_not_fatal(self):
        lines = ["data: {not json", "", "data: " + json.dumps(delta("ok")), "",
                 "data: [DONE]"]
        events = self.run_stream(lines)
        self.assertEqual("".join(e["delta"] for e in events if e["type"] == "text"), "ok")

    def test_non_data_lines_and_keepalives_are_ignored(self):
        lines = [": keepalive", "", "event: message",
                 "data: " + json.dumps(delta("ok")), "", "data: [DONE]"]
        events = self.run_stream(lines)
        self.assertEqual("".join(e["delta"] for e in events if e["type"] == "text"), "ok")

    def test_an_empty_choices_array_does_not_crash(self):
        events = self.run_stream(sse({"choices": []}, delta("ok"),
                                     delta(None, finish="stop")))
        self.assertEqual("".join(e["delta"] for e in events if e["type"] == "text"), "ok")

    # ── errors ───────────────────────────────────────────────────────────
    def test_http_error_carries_status_and_body(self):
        with self.assertRaises(engine.EngineError) as caught:
            self.run_stream(["data: ignored"], status=429, body=b'{"error":"rate limited"}')
        self.assertEqual(caught.exception.status, 429)
        self.assertIn("rate limited", caught.exception.body)

    def test_no_model_configured_is_a_clear_error(self):
        config.resolve_model = lambda ref=None: None
        with self.assertRaises(engine.EngineError) as caught:
            list(engine.stream_chat([{"role": "user", "content": "hi"}]))
        self.assertIn("no model configured", str(caught.exception))

    def test_no_endpoint_is_a_clear_error(self):
        m = model()
        m["baseUrl"] = ""
        config.resolve_model = lambda ref=None: m
        with self.assertRaises(engine.EngineError) as caught:
            list(engine.stream_chat([{"role": "user", "content": "hi"}]))
        self.assertIn("no endpoint", str(caught.exception))


class PayloadTest(unittest.TestCase):
    """What is actually put on the wire."""

    def setUp(self):
        # Saved and restored properly: an earlier version restored a stub, which
        # leaked into every later test module and broke the real thinking tests.
        self._send = thinking.send
        self.addCleanup(setattr, thinking, "send", self._send)

    def test_tools_implies_auto_tool_choice(self):
        tools = [{"type": "function", "function": {"name": "read_file"}}]
        body = engine._payload(model(), [{"role": "user", "content": "hi"}],
                               True, tools, None, None, None)
        self.assertEqual(body["tools"], tools)
        self.assertEqual(body["tool_choice"], "auto")

    def test_no_tools_means_no_tool_choice_key(self):
        body = engine._payload(model(), [{"role": "user", "content": "hi"}],
                               True, None, None, None, None)
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)

    def test_a_thinking_model_gets_the_same_temperature_as_everyone(self):
        """0.2 is not an oversight about thinking models; it is the measured winner.

        Leaving this model at its vendor default tripled what each round wrote (300 output tokens
        per round to 899) and the hard set went from 3/9 to 1/9 at about three times the tokens per
        cell. Thinking more per round is not the same as thinking better.
        """
        body = engine._payload(model(reasoning=True), [], False, None, None, None, None)
        self.assertEqual(body["temperature"], config.TEMPERATURE)

    def test_a_model_entry_can_state_its_own_temperature(self):
        m = model(reasoning=True)
        m["temperature"] = 1.0
        self.assertEqual(engine._temperature(m, None), 1.0)
        self.assertEqual(engine._temperature(model(reasoning=True), None), config.TEMPERATURE)

    def test_a_model_can_declare_that_its_gateway_manages_temperature(self):
        # Kimi / Moonshot gateways select temperature server-side for thinking models, and the
        # reference client for that family sends no temperature key at all. A model entry says so
        # with `temperatureDefault: false`; the number still wins whenever a human or a call site
        # asked for one, because this is a claim about a gateway, not a preference.
        m = model(reasoning=True)
        m["temperatureDefault"] = False
        self.assertIsNone(engine._temperature(m, None))
        self.assertEqual(engine._temperature(m, 0.7), 0.7)
        self.assertEqual(engine._temperature(model(reasoning=True), None), config.TEMPERATURE)
        body = engine._payload(m, [], False, None, None, None, None)
        self.assertNotIn("temperature", body)

    def test_an_operator_setting_beats_a_model_entry(self):
        # `TACIT_TEMPERATURE` is a person asking for a number; a catalog field is a guess about a
        # model. The person wins, which is also how every other setting in this app resolves.
        from backend import config as cfg
        original, cfg.TEMPERATURE_SET = cfg.TEMPERATURE_SET, True
        try:
            self.assertEqual(engine._temperature({"reasoning": True, "temperature": 1.0}, None),
                             cfg.TEMPERATURE)
        finally:
            cfg.TEMPERATURE_SET = original

    def test_the_call_site_beats_everything(self):
        from backend import config as cfg
        original, cfg.TEMPERATURE_SET = cfg.TEMPERATURE_SET, True
        try:
            self.assertEqual(engine._temperature({"temperature": 1.0}, 0.3), 0.3)
        finally:
            cfg.TEMPERATURE_SET = original

    def test_streaming_asks_for_usage(self):
        body = engine._payload(model(), [], True, None, None, None, None)
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_configured_temperature_is_the_default(self):
        body = engine._payload(model(), [], False, None, None, None, None)
        self.assertEqual(body["temperature"], config.TEMPERATURE)

    def test_an_explicit_temperature_wins(self):
        body = engine._payload(model(), [], False, None, 0.9, None, None)
        self.assertEqual(body["temperature"], 0.9)

    def test_max_tokens_falls_back_to_the_model(self):
        body = engine._payload(model(max_tokens=4096), [], False, None, None, None, None)
        self.assertEqual(body["max_tokens"], 4096)

    def test_reasoning_effort_is_omitted_for_a_model_that_does_not_think(self):
        # Sending a key the provider does not know can get the request rejected
        # outright, so a non-reasoning model is not told to reason.
        thinking.send = lambda ref, level: "high"
        body = engine._payload(model(reasoning=False), [], False, None, None, None, "high")
        self.assertNotIn("reasoning_effort", body)

    def test_reasoning_effort_is_sent_to_a_model_that_thinks(self):
        thinking.send = lambda ref, level: "high"
        body = engine._payload(model(reasoning=True), [], False, None, None, None, "high")
        self.assertEqual(body["reasoning_effort"], "high")

    def test_off_is_sent_even_to_a_model_not_flagged_as_reasoning(self):
        # "none" is the one value worth sending regardless: a model that thinks by
        # default is only turned off by asking, and a model that does not think
        # ignores it.
        thinking.send = lambda ref, level: "none"
        body = engine._payload(model(reasoning=False), [], False, None, None, None, "off")
        self.assertEqual(body["reasoning_effort"], "none")

    def test_ollama_gets_the_level_under_its_own_key(self):
        # Measured on ollama_cloud/kimi-k2.7-code: reasoning_effort=medium returned no
        # reasoning at all while thinking=medium returned 7,147 characters of it. The
        # level has to go under the name that endpoint reads, or pinning it is a no-op
        # that looks like a setting we control.
        thinking.send = lambda ref, level: "medium"
        body = engine._payload(model(reasoning=True, provider="ollama_cloud"),
                               [], False, None, None, None, "medium")
        self.assertEqual(body["thinking"], "medium")
        self.assertNotIn("reasoning_effort", body)

    def test_a_boolean_switch_gets_the_switch_not_a_level_name(self):
        # thinkingValues [false, true] is an on/off switch. Passing "medium" through it is
        # the bug: measured on ollama_cloud/kimi-k2.7-code, thinking="medium" produced 297
        # reasoning characters while sending nothing produced 399. An unsupported word is
        # not inert here - it costs thinking.
        card = {"reasoning": True, "thinkingValues": [False, True], "thinkingGraded": False}
        self.assertIs(thinking.send(card, "medium"), True)
        self.assertIs(thinking.send(card, "high"), True)
        self.assertIs(thinking.send(card, "on"), True)
        self.assertEqual(thinking.send(card, "off"), "none",
                         "the off switch still has to be reachable by name")
        self.assertIsNone(thinking.send(card, "default"))

    def test_the_boolean_switch_reaches_the_body_as_a_boolean(self):
        orig = thinking.send
        self.addCleanup(setattr, thinking, "send", orig)
        card = model(reasoning=True, provider="ollama_cloud")
        card["thinkingValues"] = [False, True]
        card["thinkingGraded"] = False
        body = engine._payload(card, [], False, None, None, None, "medium")
        self.assertIs(body["thinking"], True,
                      "the model's own default, asked for explicitly - not a word it must guess")

    def test_a_graded_ladder_keeps_its_rung(self):
        card = {"reasoning": True, "thinkingValues": ["low", "medium", "high"],
                "thinkingGraded": True}
        self.assertEqual(thinking.send(card, "medium"), "medium")
        self.assertEqual(thinking.send(card, "on"), "low")

    def test_an_unprobed_model_is_not_clamped_to_anything(self):
        # Nothing was ever measured, so nothing is claimed: the name goes through as asked.
        self.assertEqual(thinking.send({"thinkingValues": []}, "medium"), "medium")

    def test_ollama_off_is_a_boolean_not_a_word(self):
        # That endpoint switches thinking with true/false, so "none" has to arrive as
        # the boolean rather than as a string it does not parse.
        thinking.send = lambda ref, level: "none"
        body = engine._payload(model(reasoning=True, provider="ollama_cloud"),
                               [], False, None, None, None, "none")
        self.assertIs(False, body["thinking"])

    def test_other_providers_keep_the_openai_key(self):
        # The name is the provider's, not ours: an endpoint that does accept
        # reasoning_effort must not have it renamed under it.
        thinking.send = lambda ref, level: "high"
        body = engine._payload(model(reasoning=True, provider="openai"),
                               [], False, None, None, None, "high")
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertNotIn("thinking", body)


class RemoteModelDetailsTest(unittest.TestCase):
    """Context windows come from the provider in several different shapes."""

    def _probe(self, payload):
        class Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return payload

        class Client:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None):
                return Resp()

        original = engine.httpx.Client
        engine.httpx.Client = Client
        self.addCleanup(setattr, engine.httpx, "Client", original)
        return engine.remote_model_details("http://x")

    def test_bare_ids_are_accepted(self):
        rows = self._probe({"data": [{"id": "m1"}, "m2"]})
        self.assertEqual([r["id"] for r in rows], ["m1", "m2"])
        self.assertEqual(rows[0]["contextWindow"], 0)

    def test_openrouter_style_context_length(self):
        rows = self._probe({"data": [{"id": "m", "context_length": 131072}]})
        self.assertEqual(rows[0]["contextWindow"], 131072)

    def test_vllm_style_nested_max_model_len(self):
        rows = self._probe({"data": [{"id": "m", "meta": {"max_model_len": "262144"}}]})
        self.assertEqual(rows[0]["contextWindow"], 262144)

    def test_ollama_style_details_nested(self):
        rows = self._probe({"models": [{"name": "m",
                                         "details": {"context_length": 1000000}}]})
        self.assertEqual(rows[0]["contextWindow"], 1000000)

    def test_max_tokens_variants(self):
        rows = self._probe({"data": [{"id": "a", "max_output_tokens": 32768},
                                     {"id": "b", "max_completion_tokens": 8192}]})
        self.assertEqual([r["maxTokens"] for r in rows], [32768, 8192])

    def test_booleans_are_not_mistaken_for_numbers(self):
        rows = self._probe({"data": [{"id": "m", "context_length": True}]})
        self.assertEqual(rows[0]["contextWindow"], 0)


class ScriptedProvider:
    """A canned model for the WebSocket tests: one tool call, then an answer."""

    def __init__(self):
        self.step = 0

    def __call__(self, messages, **kwargs):
        self.step += 1
        if kwargs.get("tools") and self.step == 1:
            yield {"type": "tool_calls", "calls": [{
                "id": "call_ws1", "name": "read_file",
                "arguments": json.dumps({"path": "alpha.py"})}]}
        else:
            yield {"type": "text", "delta": "alpha.py defines a() returning 1."}
        yield {"type": "usage",
               "usage": {"input": 1200, "output": 40, "total": 1240}}
        yield {"type": "done", "finish": "stop",
               "model": {"ref": "p/m"}}


class WebSocketTurnTest(unittest.TestCase):
    """The interface's only path into a turn, driven end to end.

    A turn is a thread feeding an asyncio queue feeding a socket, and nothing
    covered the seams: whether the transcript is persisted per step, whether
    metrics reach the record, whether a stale session id is refused rather than
    silently created.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {k: getattr(config, k) for k in self._keys}
        self._home = config.HOME
        config.HOME = home
        for k in self._keys:
            target = home / Path(self._orig[k]).name
            setattr(config, k, target)
            if k.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)
        config.ensure_home()
        config.save_registry({"default": "p/m", "providers": {
            "p": {"baseUrl": "http://x", "apiKey": "k",
                  "models": [{"id": "m", "contextWindow": 100000}]}}})
        self.proj = tempfile.TemporaryDirectory()
        (Path(self.proj.name) / "alpha.py").write_text("def a():\n    return 1\n",
                                                       encoding="utf-8")
        from backend.ai import engine as _engine
        self._stream = _engine.stream_chat
        _engine.stream_chat = ScriptedProvider()
        self.addCleanup(setattr, _engine, "stream_chat", self._stream)

    def tearDown(self):
        config.HOME = self._home
        for k, v in self._orig.items():
            setattr(config, k, v)
        self._tmp.cleanup()
        self.proj.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        from backend.main import app
        return TestClient(app)

    def _drain(self, ws, until=("done",), limit=200):
        out = []
        for _ in range(limit):
            ev = ws.receive_json()
            out.append(ev)
            if ev.get("type") in until:
                break
        return out

    def test_a_stale_session_is_adopted_under_the_id_the_browser_holds(self):
        """A tab that outlived its store gets its session back, same id.

        The socket must not invent a session from a bare mention of an id, but
        the one case where the client is entitled to ask is when the server has
        answered 4404: the browser holds an id a real New-session click once
        created. Without this path the tab reconnects every 1.2s against a code
        that can never succeed, and the interface reads 'disconnected -
        retrying' while nothing is wrong except the server's memory.
        """
        from backend import store
        sid = "adopt-me-1"
        self.assertIsNone(store.get(sid))
        with self._client().websocket_connect(
                f"/ws/{sid}?create=1&mode=agent&thinking=default"
                f"&model=p/m&name=Old%20title&workdir=") as ws:
            ready = self._drain(ws, until=("session_ready",))[-1]
        self.assertEqual(ready["sid"], sid)            # not a fresh uuid
        self.assertEqual(ready["name"], "Old title")   # not 'New session'
        rec = store.get(sid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["thinking"], "default")

        # an ordinary reconnect now works, because the id is known
        with self._client().websocket_connect(f"/ws/{sid}") as ws:
            again = self._drain(ws, until=("session_ready",))[-1]
        self.assertEqual(again["sid"], sid)

    def test_a_full_turn_persists_per_step_with_its_tools(self):
        from backend import store
        with self._client().websocket_connect(
                "/ws/ws-e2e-1?create=1&mode=agent&thinking=default&model=p/m&workdir="
                + self.proj.name.replace("\\", "%5C")) as ws:
            hello = self._drain(ws, until=("session_ready",))
            self.assertEqual(hello[0]["type"], "hello")
            sid = hello[0]["sid"]
            ws.send_json({"type": "prompt", "message": "read alpha.py and tell me"})
            events = self._drain(ws)

        kinds = [e["type"] for e in events]
        self.assertIn("tool_start", kinds)
        self.assertIn("tool_end", kinds)
        self.assertIn("usage", kinds)
        self.assertEqual(kinds[-1], "done")

        rec = store.get(sid)
        msgs = rec["messages"]
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "read alpha.py and tell me")
        # One row per step, with the calls and results attached to the step that
        # made them. Flattening a turn into one message is what once erased the
        # agent's own history.
        with_tools = [m for m in msgs if m.get("tools")]
        self.assertEqual(len(with_tools), 1)
        self.assertEqual(with_tools[0]["tools"][0]["name"], "read_file")
        self.assertIn("def a()", with_tools[0]["tools"][0]["result"])
        self.assertTrue(any(m["role"] == "assistant" and (m.get("content") or "").strip()
                            and not m.get("tools") for m in msgs))

    def test_metrics_and_context_reach_the_record(self):
        from backend import store
        with self._client().websocket_connect(
                "/ws/ws-e2e-2?create=1&mode=agent&model=p/m&workdir="
                + self.proj.name.replace("\\", "%5C")) as ws:
            sid = self._drain(ws, until=("session_ready",))[0]["sid"]
            ws.send_json({"type": "prompt", "message": "read alpha.py"})
            self._drain(ws)
        rec = store.get(sid)
        self.assertGreater(rec["metrics"]["prompt_tokens"], 0)
        self.assertGreater(rec["metrics"]["tool_schema_tokens"], 0)
        self.assertIsNotNone(rec["usage"]["context"]["tokens"],
                             "the context meter must not be left blank")
        self.assertEqual(rec["usage"]["context"]["window"], 100000)

    def test_tool_calls_reach_the_audit_ledger_with_the_session(self):
        from backend import audit, store
        with self._client().websocket_connect(
                "/ws/ws-e2e-3?create=1&mode=agent&model=p/m&workdir="
                + self.proj.name.replace("\\", "%5C")) as ws:
            sid = self._drain(ws, until=("session_ready",))[0]["sid"]
            ws.send_json({"type": "prompt", "message": "read alpha.py"})
            self._drain(ws)
        del store
        rows = [r for r in audit.recent(50) if r["event"] == "tool_call"]
        self.assertTrue(rows, "no tool_call row was written")
        self.assertEqual(rows[0]["session"], sid)
        self.assertEqual(rows[0]["tool"], "read_file")

    def test_a_first_prompt_becomes_the_title(self):
        from backend import store
        with self._client().websocket_connect(
                "/ws/ws-e2e-4?create=1&mode=agent&model=p/m&workdir="
                + self.proj.name.replace("\\", "%5C")) as ws:
            sid = self._drain(ws, until=("session_ready",))[0]["sid"]
            ws.send_json({"type": "prompt",
                          "message": "Can you please read alpha.py and summarise it for me"})
            events = self._drain(ws)
        rec = store.get(sid)
        self.assertNotEqual(rec["title"], "New session")
        self.assertLessEqual(len(rec["title"]), 48)
        self.assertFalse(rec["title"].lower().startswith("can you please"))
        self.assertTrue(any(e["type"] == "state_delta" and e.get("name") for e in events))

    def test_an_unknown_session_is_refused_not_created(self):
        # Honouring a stale id grew the session list on every page load.
        from backend import store
        before = len(store.list_sessions())
        with self._client().websocket_connect("/ws/does-not-exist") as ws:
            pass
        self.assertEqual(len(store.list_sessions()), before)

    def test_chat_mode_offers_no_tools(self):
        from backend import store
        with self._client().websocket_connect(
                "/ws/ws-e2e-5?create=1&mode=chat&model=p/m&workdir="
                + self.proj.name.replace("\\", "%5C")) as ws:
            sid = self._drain(ws, until=("session_ready",))[0]["sid"]
            ws.send_json({"type": "prompt", "message": "hello"})
            events = self._drain(ws)
        self.assertNotIn("tool_start", [e["type"] for e in events])
        self.assertEqual(store.get(sid)["mode"], "chat")


# ── Anthropic transport and prompt caching ────────────────────────────────
ANTHROPIC_MODEL = {"ref": "anthropic/claude", "provider": "anthropic",
                   "model": "claude-x", "baseUrl": "https://api.anthropic.com",
                   "apiKey": "k", "contextWindow": 200000, "maxTokens": 8192,
                   "reasoning": True, "thinkingValues": [], "thinkingDefault": None}
OPENAI_MODEL = {"ref": "p/m", "provider": "p", "model": "m",
                "baseUrl": "https://ollama.com/v1", "apiKey": "",
                "contextWindow": 100000, "maxTokens": 4096, "reasoning": False,
                "thinkingValues": [], "thinkingDefault": None}
ROUTER_MODEL = {**OPENAI_MODEL, "ref": "openrouter/x", "provider": "openrouter",
                "baseUrl": "https://openrouter.ai/api/v1"}

SAMPLE = [
    {"role": "system", "content": "STANDING PROMPT"},
    {"role": "user", "content": "read a.py"},
    {"role": "assistant", "content": "on it", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "the body"},
    {"role": "tool", "tool_call_id": "c2", "content": "another result"},
]
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]


class TestFlavour(unittest.TestCase):
    """The right strategy for the endpoint actually in use, not one strategy for all."""

    def test_anthropic_is_detected(self):
        self.assertEqual(anthropic.flavour(ANTHROPIC_MODEL), "anthropic")

    def test_openrouter_is_detected(self):
        self.assertEqual(anthropic.flavour(ROUTER_MODEL), "openrouter")

    def test_an_ordinary_openai_endpoint_is_left_alone(self):
        self.assertEqual(anthropic.flavour(OPENAI_MODEL), "openai")

    def test_a_local_server_is_left_alone(self):
        self.assertEqual(anthropic.flavour(
            {"baseUrl": "http://192.168.1.5:8100/v1", "provider": "llamacpp"}), "openai")

    def test_auto_caches_only_where_it_is_understood(self):
        orig = config.PROMPT_CACHE
        config.PROMPT_CACHE = "auto"
        self.addCleanup(setattr, config, "PROMPT_CACHE", orig)
        self.assertTrue(anthropic.cache_enabled(ANTHROPIC_MODEL))
        self.assertTrue(anthropic.cache_enabled(ROUTER_MODEL))
        self.assertFalse(anthropic.cache_enabled(OPENAI_MODEL))

    def test_off_disables_everywhere_and_on_forces(self):
        orig = config.PROMPT_CACHE
        self.addCleanup(setattr, config, "PROMPT_CACHE", orig)
        config.PROMPT_CACHE = "off"
        self.assertFalse(anthropic.cache_enabled(ANTHROPIC_MODEL))
        config.PROMPT_CACHE = "on"
        self.assertTrue(anthropic.cache_enabled(OPENAI_MODEL))

    def test_endpoint_tolerates_a_baseurl_that_already_ends_in_v1(self):
        self.assertEqual(anthropic.endpoint({"baseUrl": "https://api.anthropic.com/v1"}),
                         "https://api.anthropic.com/v1/messages")
        self.assertEqual(anthropic.endpoint({"baseUrl": "https://api.anthropic.com"}),
                         "https://api.anthropic.com/v1/messages")
        self.assertEqual(anthropic.endpoint({"baseUrl": "https://gw.test/v1/"}),
                         "https://gw.test/v1/messages")

    def test_both_auth_headers_are_sent(self):
        # x-api-key is Anthropic's; a gateway in front may want Bearer instead.
        h = anthropic.headers(ANTHROPIC_MODEL)
        self.assertEqual(h["x-api-key"], "k")
        self.assertEqual(h["authorization"], "Bearer k")
        self.assertIn("anthropic-version", h)


class TestAnthropicPayload(unittest.TestCase):
    def _body(self, **kw):
        args = {"tools": TOOLS, "temperature": 0.2, "max_tokens": None,
                "reasoning_effort": None}
        args.update(kw)
        return anthropic.payload(ANTHROPIC_MODEL, SAMPLE, True, args.pop("tools"),
                                 args.pop("temperature"), args.pop("max_tokens"),
                                 args.pop("reasoning_effort"), **args)

    def test_system_is_lifted_out_of_the_messages(self):
        body = self._body()
        self.assertEqual(body["system"][0]["text"], "STANDING PROMPT")
        self.assertNotIn("system", [m["role"] for m in body["messages"]])

    def test_tools_use_input_schema_not_parameters(self):
        body = self._body()
        self.assertEqual(body["tools"][0]["name"], "read_file")
        self.assertIn("input_schema", body["tools"][0])
        self.assertNotIn("parameters", body["tools"][0])
        self.assertNotIn("function", body["tools"][0])
        self.assertEqual(body["tool_choice"], {"type": "auto"})

    def test_max_tokens_is_always_present(self):
        # Anthropic rejects the request outright without it.
        self.assertEqual(self._body()["max_tokens"], config.ANTHROPIC_MAX_TOKENS)
        self.assertEqual(self._body(max_tokens=1000)["max_tokens"], 1000)

    def test_consecutive_tool_results_merge_into_one_user_turn(self):
        # A tool result is not a role of its own; an unmerged run is a 400.
        body = self._body()
        merged = [m for m in body["messages"]
                  if m["role"] == "user" and isinstance(m["content"], list)
                  and m["content"] and m["content"][0].get("type") == "tool_result"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]["content"]), 2)
        self.assertEqual([b["tool_use_id"] for b in merged[0]["content"]], ["c1", "c2"])

    def test_an_assistant_tool_call_becomes_a_tool_use_block(self):
        body = self._body()
        assistant = [m for m in body["messages"] if m["role"] == "assistant"][0]
        kinds = [b["type"] for b in assistant["content"]]
        self.assertEqual(kinds, ["text", "tool_use"])
        self.assertEqual(assistant["content"][1]["input"], {"path": "a.py"})

    def test_invalid_tool_arguments_do_not_break_the_request(self):
        msgs = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "x", "arguments": "{not json"}}]}]
        body = anthropic.payload(ANTHROPIC_MODEL, msgs, False, None, None, None, None)
        self.assertEqual(body["messages"][0]["content"][0]["input"], {})

    def test_thinking_is_mapped_and_off_really_is_off(self):
        self.assertEqual(anthropic.payload(ANTHROPIC_MODEL, SAMPLE, False, None,
                                           None, None, "none").get("thinking"), None)
        on = anthropic.payload(ANTHROPIC_MODEL, SAMPLE, False, None, None,
                               8192, "high").get("thinking")
        self.assertEqual(on["type"], "enabled")
        self.assertGreater(on["budget_tokens"], 1024)

    def test_temperature_is_omitted_while_thinking_is_on(self):
        # Sending both is a 400; thinking requires the default of 1.
        body = anthropic.payload(ANTHROPIC_MODEL, SAMPLE, False, None, 0.2, 8192, "high")
        self.assertNotIn("temperature", body)
        self.assertIn("thinking", body)

    def test_bookkeeping_never_reaches_the_wire(self):
        body = self._body()
        self.assertIn("_cache_breakpoints", body)
        self.assertNotIn("_cache_breakpoints", anthropic.wire_body(body))


class TestCacheBreakpoints(unittest.TestCase):
    def test_the_stable_prefix_is_marked(self):
        body = anthropic.payload(ANTHROPIC_MODEL, SAMPLE, True, TOOLS, None, None, None)
        self.assertEqual(body["_cache_breakpoints"], 3)
        self.assertIn("cache_control", body["tools"][-1])
        self.assertIn("cache_control", body["system"][-1])

    def test_the_last_message_is_marked_for_incremental_caching(self):
        body = anthropic.payload(ANTHROPIC_MODEL, SAMPLE, True, TOOLS, None, None, None)
        last = body["messages"][-1]["content"][-1]
        self.assertIn("cache_control", last)

    def test_never_more_than_the_four_the_api_allows(self):
        msgs = list(SAMPLE) + [{"role": "user", "content": f"m{i}"} for i in range(10)]
        body = anthropic.payload(ANTHROPIC_MODEL, msgs, True, TOOLS, None, None, None)
        self.assertLessEqual(body["_cache_breakpoints"], 4)

    def test_cache_off_means_no_markers_anywhere(self):
        body = anthropic.payload(ANTHROPIC_MODEL, SAMPLE, True, TOOLS, None, None, None,
                                 cache=False)
        self.assertEqual(body["_cache_breakpoints"], 0)
        blob = json.dumps(body)
        self.assertNotIn("cache_control", blob)

    def test_an_openai_endpoint_gets_no_markers_at_all(self):
        # Automatic prefix caching needs nothing added, and an unknown field can get
        # the request rejected. This is the "best compatible" case: do nothing.
        body = engine._payload(OPENAI_MODEL, list(SAMPLE), True, TOOLS, None, None, None)
        self.assertNotIn("cache_control", json.dumps(body))

    def test_a_passthrough_gateway_marks_the_system_block_only(self):
        body = engine._mark_openai_cache(engine._payload(
            ROUTER_MODEL, list(SAMPLE), True, TOOLS, None, None, None))
        marked = [m for m in body["messages"]
                  if isinstance(m.get("content"), list)
                  and any("cache_control" in b for b in m["content"] if isinstance(b, dict))]
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0]["role"], "system")

    def test_marking_does_not_mutate_the_callers_transcript(self):
        # The agent loop holds that list. Rewriting it in place meant the
        # retry-without-markers path rebuilt its "plain" body from the already
        # marked copy and failed the same way twice.
        msgs = [{"role": "system", "content": "STANDING"},
                {"role": "user", "content": "hi"}]
        before = json.dumps(msgs)
        engine._mark_openai_cache(engine._payload(ROUTER_MODEL, msgs, True, None,
                                                  None, None, None))
        self.assertEqual(json.dumps(msgs), before)
        self.assertIsInstance(msgs[0]["content"], str)

    def test_a_cache_rejection_is_recognised_and_other_errors_are_not(self):
        self.assertTrue(anthropic.is_cache_rejection(
            400, "unexpected field: cache_control"))
        self.assertTrue(anthropic.is_cache_rejection(400, "unsupported parameter"))
        self.assertFalse(anthropic.is_cache_rejection(400, "invalid api key"))
        self.assertFalse(anthropic.is_cache_rejection(429, "cache_control rate limited"))
        self.assertFalse(anthropic.is_cache_rejection(500, "cache_control exploded"))


class TestAnthropicStream(unittest.TestCase):
    """The typed event stream, translated back into Tacit's events."""

    def test_text_and_thinking_are_separated(self):
        state = anthropic.StreamState()
        out = []
        for ev in ({"type": "content_block_start", "index": 0,
                    "content_block": {"type": "thinking"}},
                   {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "because "}},
                   {"type": "content_block_start", "index": 1,
                    "content_block": {"type": "text"}},
                   {"type": "content_block_delta", "index": 1,
                    "delta": {"type": "text_delta", "text": "the answer"}}):
            out.extend(state.feed(ev))
        self.assertEqual([e["delta"] for e in out if e["type"] == "reason"], ["because "])
        self.assertEqual([e["delta"] for e in out if e["type"] == "text"], ["the answer"])

    def test_fragmented_tool_input_json_is_reassembled(self):
        state = anthropic.StreamState()
        for ev in ({"type": "content_block_start", "index": 0,
                    "content_block": {"type": "tool_use", "id": "tu_1",
                                      "name": "read_file"}},
                   {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
                   {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": ' "a.py"}'}}):
            list(state.feed(ev))
        calls = state.tool_calls()
        self.assertEqual(calls[0]["id"], "tu_1")
        self.assertEqual(calls[0]["name"], "read_file")
        self.assertEqual(json.loads(calls[0]["arguments"]), {"path": "a.py"})

    def test_broken_tool_json_is_not_passed_on_as_valid(self):
        state = anthropic.StreamState()
        for ev in ({"type": "content_block_start", "index": 0,
                    "content_block": {"type": "tool_use", "id": "tu", "name": "x"}},
                   {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": "{trunc"}}):
            list(state.feed(ev))
        self.assertEqual(json.loads(state.tool_calls()[0]["arguments"]), {})

    def test_usage_is_merged_across_message_start_and_message_delta(self):
        # Input arrives first, output at the end; neither alone is the total.
        state = anthropic.StreamState()
        list(state.feed({"type": "message_start", "message": {"usage": {
            "input_tokens": 1000, "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 50}}}))
        list(state.feed({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                         "usage": {"output_tokens": 80}}))
        self.assertEqual(state.usage["input"], 1000)
        self.assertEqual(state.usage["output"], 80)
        self.assertEqual(state.usage["total"], 1080)
        self.assertEqual(state.usage["cache_read"], 900)
        self.assertEqual(state.usage["cache_write"], 50)
        self.assertEqual(state.stop_reason, "end_turn")

    def test_a_stream_error_event_is_raised_not_swallowed(self):
        state = anthropic.StreamState()
        with self.assertRaises(RuntimeError):
            list(state.feed({"type": "error", "error": {"message": "overloaded"}}))

    def test_pings_and_unknown_events_are_ignored(self):
        state = anthropic.StreamState()
        self.assertEqual(list(state.feed({"type": "ping"})), [])
        self.assertEqual(list(state.feed({"type": "something_new"})), [])


if __name__ == "__main__":
    unittest.main()

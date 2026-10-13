"""A running command reports while it runs — and the model's view is unchanged.

The bug this pins is invisible to every existing test, including the one that
pinned the hang itself. `test_goal_turn_completes` asserts the turn *finishes*,
which it did while the UI showed nothing for three minutes: the tool returned
one string when the process exited, `tool_start` had already closed the card,
and `tool_end` was the first thing that could ever be said about it. A test that
only checks the final text cannot tell streaming from capture, because the text
is the same either way. So these tests assert *when* a thing arrived, not just
that it arrived.

Three layers, because the failure could live in any of them:

  sandbox    lines must reach the callback before the child exits — a buffered
             read would satisfy every assertion about the finished output and
             still show nothing while a build ran.
  _stream_tool  the rate limiter: coalesced, capped per event, alive when silent,
             and the abandoned-generator path that the inline call never had.
  call_tool  the gate: only a tool that declares `on_output` gets it, a
             `**kwargs` tool must not receive it, and no call id means no thread.

The last group is the one that matters most and is easiest to write badly: the
tool's return value is the model's input, so any streaming change that altered
that string would change agent behaviour while looking like a UI fix. Both the
sandbox and the agent layer assert the streamed result is *identical* to the
captured one.
"""

import contextvars
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The gap is the entire measurement. A command that prints, waits, and prints
# again produces the same final text whether it was streamed or captured; only
# the arrival times separate the two. Kept generous because a CI box that is
# slow at process startup must not turn a real pass into a flaky fail.
GAP = 0.8

TWO_LINES = (
    "import sys, time\n"
    "print('first', flush=True)\n"
    f"time.sleep({GAP})\n"
    "print('second', flush=True)\n"
)

SLEEPY = (
    "import sys, time\n"
    "print('early', flush=True)\n"
    "time.sleep(30)\n"
    "print('late', flush=True)\n"
)


def _script(body: str) -> str:
    """A temp file rather than `python -c`: sandbox hands the command string to
    cmd.exe on Windows, and nested quoting there is a test failure waiting for
    the wrong reason."""
    fd, path = tempfile.mkstemp(suffix=".py", prefix="tacit_stream_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def _command(path: str) -> str:
    return f'"{sys.executable}" "{path}"'


def _threads_named(prefix: str):
    return [t for t in threading.enumerate() if t.name.startswith(prefix)]


class SandboxStreamTests(unittest.TestCase):
    """Lines arrive before the child exits, and capture is untouched.

    These go through `sandbox.run`, the public entry, rather than the private
    captor: that helper's signature changed shape while this file was being
    written, and a test bound to a private call form fails for a reason that has
    nothing to do with what it is checking.
    """

    def setUp(self):
        self.paths = []

    def tearDown(self):
        for p in self.paths:
            try:
                os.remove(p)
            except OSError:
                pass

    def _make(self, body):
        path = _script(body)
        self.paths.append(path)
        return path

    def test_output_arrives_during_the_run_not_after_it(self):
        from backend import sandbox

        marks = []
        t0 = time.time()
        path = self._make(TWO_LINES)
        res = sandbox.run(_command(path), backend="none", timeout=20,
                          on_output=lambda k, t: marks.append(
                              (time.time() - t0, k, t)))
        whole = time.time() - t0
        self.assertEqual(res["code"], 0, res.get("stderr"))
        got = "".join(t for _, _, t in marks)
        self.assertIn("first", got)
        self.assertIn("second", got)
        # The discriminator. Under capture-then-return every callback would fire
        # after the process had already exited, so the first mark would land at
        # ~the total runtime. Streaming puts it in the first third.
        self.assertLess(marks[0][0], whole * 0.6,
                        "the first line was reported only after the command "
                        f"finished ({marks[0][0]:.2f}s of {whole:.2f}s) — that is "
                        "communicate(), not streaming")
        self.assertGreaterEqual(len([m for m in marks if m[1] == "stdout"]), 2)

    def test_a_callback_changes_the_result_not_at_all(self):
        """The tool's return value is the model's input. Streaming is allowed to
        change when a *person* reads it and nothing else, so the two runs are
        compared field by field rather than assumed equivalent."""
        from backend import sandbox

        path = self._make(TWO_LINES)
        cmd = _command(path)
        plain = sandbox.run(cmd, backend="none", timeout=20)
        streamed = sandbox.run(cmd, backend="none", timeout=20,
                               on_output=lambda k, t: None)
        self.assertIn("first", streamed["stdout"])
        self.assertIn("second", streamed["stdout"])
        for key in sorted(set(plain) | set(streamed)):
            if key == "duration_ms":
                continue  # the one field that is legitimately a stopwatch
            self.assertEqual(plain.get(key), streamed.get(key),
                             f"{key} differs between captured and streamed")

    def test_partial_output_survives_a_timeout_while_streaming(self):
        """A build killed at its deadline is the moment output is most wanted,
        and the one most likely to be lost by a refactor."""
        from backend import sandbox

        path = self._make(SLEEPY)
        marks = []
        res = sandbox.run(_command(path), backend="none", timeout=2,
                          on_output=lambda k, t: marks.append(t))
        self.assertFalse(res["ok"], "a killed command must not report success")
        self.assertIn("timed out", res["stderr"])
        self.assertIn("early", res["stdout"])
        self.assertNotIn("late", res["stdout"])
        self.assertIn("early", "".join(marks),
                      "the partial output was captured but never reported live")

    def test_streaming_does_not_reintroduce_an_inherited_stdin(self):
        """The stdin fix lives in the kwargs the captor builds, and the streamed
        path shares that dict. A reader rewrite that dropped it would hang every
        command again, and no other test here would notice."""
        import inspect

        from backend import sandbox
        src = inspect.getsource(sandbox._run_captured)
        self.assertIn("DEVNULL", src)
        self.assertNotIn('"stdin": subprocess.PIPE', src)
        # And the streamed branch must be the only thing that changes shape:
        # `_run_streamed` is reached through the same Popen, not a second one.
        self.assertIn("_run_streamed(proc", src)


class StreamToolTests(unittest.TestCase):
    """The rate limiter: coalesced, capped, beating, and safe to abandon."""

    def _run(self, fn, name="run_shell", call_id="call_1"):
        from backend import agent
        out: dict = {}
        events = list(agent._stream_tool(fn, {}, name, out, call_id))
        return events, out

    def test_events_carry_the_shape_the_card_reads(self):
        def fn(on_output=None):
            on_output("stdout", "hello\n")
            return "done"

        events, out = self._run(fn)
        self.assertTrue(events)
        for e in events:
            self.assertEqual(e["type"], "tool_progress")
            self.assertEqual(e["id"], "call_1")
            self.assertEqual(e["name"], "run_shell")
            self.assertIsInstance(e["chunk"], str)
            self.assertGreaterEqual(e["elapsed_s"], 0)
            self.assertGreaterEqual(e["chars"], 0)
        self.assertEqual(out["result"], "done",
                         "the tool's own return value is what the model sees")

    def test_fifty_lines_do_not_become_fifty_events(self):
        from backend import agent

        lines = [f"line {i}\n" for i in range(50)]

        def fn(on_output=None):
            for ln in lines:
                on_output("stdout", ln)
            return "done"

        events, _ = self._run(fn)
        self.assertLessEqual(len(events), 3,
                             f"{len(events)} events for 50 lines: the card would "
                             "flicker and the bus would carry per-line traffic")
        self.assertEqual("".join(e["chunk"] for e in events), "".join(lines),
                         "coalescing must delay output, never lose it")

    def test_a_burst_flushes_at_once_and_stays_under_the_event_cap(self):
        from backend import agent

        blob = "x" * 20000

        def fn(on_output=None):
            on_output("stdout", blob)
            return "done"

        events, _ = self._run(fn)
        self.assertGreaterEqual(len(events), 2,
                                "a 20KB burst should not wait for the window")
        for e in events:
            self.assertLessEqual(len(e["chunk"]), agent.PROGRESS_MAX_CHUNK,
                                 "one event grew past the cap — the final flush "
                                 "was leaking an unbounded chunk")
        self.assertEqual("".join(e["chunk"] for e in events), blob,
                         "capping an event must trim the window, not the output")

    def test_a_silent_run_still_beats(self):
        """A compile that prints nothing for two minutes is the case the spinner
        was already failing at: the card looked identical to a hang."""
        def fn(on_output=None):
            time.sleep(1.4)
            return "done"

        events, _ = self._run(fn)
        beats = [e for e in events if e["chunk"] == ""]
        self.assertTrue(beats, "a run that printed nothing produced no heartbeat")
        self.assertEqual(beats[0]["chars"], 0)
        self.assertGreaterEqual(beats[0]["elapsed_s"], 1.0,
                                "the heartbeat must carry elapsed time, or the "
                                "card cannot show it is still going")

    def test_the_worker_thread_sees_the_callers_context(self):
        """The tool runs on another thread, and contextvars do not cross threads
        on their own. Without copy_context a streamed tool would read defaults
        where the inline call read the turn's state — a tool that behaves
        differently because someone is watching."""
        probe = contextvars.ContextVar("tacit_stream_probe", default="unset")
        seen = {}

        def fn(on_output=None):
            seen["value"] = probe.get()
            return "ok"

        probe.set("set-by-the-turn")
        try:
            self._run(fn)
        finally:
            probe.set("unset")
        self.assertEqual(seen["value"], "set-by-the-turn",
                         "the tool ran without the caller's context")

    def test_an_exception_reaches_the_caller_unmapped(self):
        """Mapping to `ERROR: ...` stays in call_tool so one try block serves
        both the inline and the threaded path. A thread that swallowed the
        exception would hand back a missing result instead of a real error."""
        from backend import agent

        def fn(on_output=None):
            raise agent.ToolError("boom")

        with self.assertRaises(agent.ToolError):
            self._run(fn)

    def test_a_threaded_failure_maps_exactly_like_an_inline_one(self):
        """The two paths must agree on the string the model reads, or a tool that
        fails while someone watches reports something different from the same
        tool failing unnoticed."""
        from backend import agent

        def fn(command="", on_output=None):
            raise agent.ToolError("boom")

        agent.EXECS["probe_fail"] = fn
        try:
            out_streamed: dict = {}
            list(agent.call_tool("probe_fail", {"command": "x"},
                                 {"project": None, "session": ""},
                                 out_streamed, {"id": "c1"}))
            out_inline: dict = {}
            list(agent.call_tool("probe_fail", {"command": "x"},
                                 {"project": None, "session": ""},
                                 out_inline, None))
        finally:
            agent.EXECS.pop("probe_fail", None)
        self.assertEqual(out_streamed["result"], out_inline["result"])
        self.assertEqual(out_streamed["result"], "ERROR: boom")

    def test_abandoning_the_generator_neither_hangs_nor_leaks_a_thread(self):
        """The path the inline call could never take: a turn being torn down
        closes the generator mid-command. Blocking in `finally` to collect the
        result would put the hang right back where this feature removed it."""
        release = threading.Event()
        finished = threading.Event()

        def fn(on_output=None):
            for _ in range(60):
                if release.is_set():
                    break
                on_output("stdout", "tick\n")
                time.sleep(0.05)
            finished.set()
            return "done"

        from backend import agent
        out: dict = {}
        gen = agent._stream_tool(fn, {}, "run_shell", out, "c1")
        next(gen)  # one event proves it is really running
        workers = _threads_named("tacit-tool-")
        self.assertTrue(workers, "the tool did not run on its own thread")

        t0 = time.time()
        gen.close()
        self.assertLess(time.time() - t0, 1.0,
                        "close() waited on the running command — that is the "
                        "hang this feature exists to remove, one layer later")
        self.assertTrue(all(t.daemon for t in workers),
                        "a non-daemon worker would keep the process alive after "
                        "the turn that abandoned it")
        release.set()
        deadline = time.time() + 5
        while time.time() < deadline and not finished.is_set():
            time.sleep(0.05)
        self.assertTrue(finished.is_set(), "the abandoned command never finished")


class CallToolGateTests(unittest.TestCase):
    """Who gets a callback, and what happens when they do."""

    def setUp(self):
        from backend import agent
        self.agent = agent
        self.saved = dict(agent.EXECS)
        self.ctx = {"project": None, "session": ""}

    def tearDown(self):
        self.agent.EXECS.clear()
        self.agent.EXECS.update(self.saved)

    def _call(self, name, args, call):
        out: dict = {}
        parts = list(self.agent.call_tool(name, args, self.ctx, out, call))
        return parts, out

    def test_the_model_cannot_pass_a_callback(self):
        """`on_output` is injected by call_tool and absent from the schema, so
        the model has no way to fabricate a tool that streams."""
        self.assertNotIn("on_output", json.dumps(self.agent.TOOLS))

    def test_a_kwargs_tool_is_never_handled_the_callback(self):
        """`"on_output" in params` is a name lookup, so **kwargs does not match.
        Were it ever changed to a duck-type, a kwargs tool would receive a
        callable it must not have and quietly start threading."""
        seen = {}

        def tool(command="", **kwargs):
            seen.update(kwargs)
            return "ran"

        self.agent.EXECS["probe_kwargs"] = tool
        parts, out = self._call("probe_kwargs", {"command": "x"}, {"id": "c1"})
        self.assertNotIn("on_output", seen,
                         "the callback leaked into a **kwargs tool")
        self.assertEqual(out["result"], "ran")
        self.assertFalse([p for p in parts if p["type"] == "tool_progress"])

    def test_no_call_id_means_no_thread(self):
        """With no card to attribute the tail to, the run stays inline: no
        thread, no events, same result."""
        seen = {}

        def tool(command="", on_output=None):
            seen["streamed"] = on_output is not None
            return "ran"

        self.agent.EXECS["probe_decl"] = tool
        parts, out = self._call("probe_decl", {"command": "x"}, None)
        self.assertFalse(seen["streamed"], "a tool with no card was streamed anyway")
        self.assertEqual(out["result"], "ran")

    def test_a_declaring_tool_streams_and_returns_the_same_result(self):
        got = {}

        def tool(command="", on_output=None):
            on_output("stdout", "live\n")
            got["streamed"] = on_output is not None
            return "ran"

        self.agent.EXECS["probe_decl2"] = tool
        parts, out = self._call("probe_decl2", {"command": "x"}, {"id": "c2"})
        self.assertTrue(got["streamed"])
        events = [p for p in parts if p["type"] == "tool_progress"]
        self.assertTrue(events, "a tool with a card produced no live output")
        self.assertEqual(events[0]["id"], "c2")
        self.assertEqual(out["result"], "ran",
                         "the transcript gets the tool's own string, unchanged")

    def test_a_real_command_reports_before_it_finishes(self):
        """The end-to-end point of the whole feature, measured in seconds: a
        non-empty chunk must arrive while the process is still alive."""
        from backend import agent

        path = _script(TWO_LINES)
        try:
            out: dict = {}
            t0 = time.time()
            first = None
            for p in agent.call_tool("run_shell", {"command": _command(path)},
                                     self.ctx, out, {"id": "c3"}):
                if p["type"] == "tool_progress" and p["chunk"].strip() and first is None:
                    first = time.time() - t0
            whole = time.time() - t0
        finally:
            os.remove(path)
        self.assertIsNotNone(first, "a printing command streamed nothing")
        self.assertLess(first, whole * 0.6,
                        f"the first output arrived at {first:.2f}s of {whole:.2f}s "
                        "— streamed after the fact, which is what the card could "
                        "already do with tool_end")
        self.assertIn("first", out["result"])
        self.assertIn("second", out["result"])


class BoundaryTests(unittest.TestCase):
    """Progress is a viewer channel. It must not reach the model or the record."""

    def _src(self, rel):
        return (ROOT / rel).read_text(encoding="utf-8", errors="replace")

    def test_no_transcript_step_ever_carries_a_progress_chunk(self):
        """`trace.append` is the only way a step becomes a transcript row, and
        its payload is built from text/reason/tools. Read off the balanced call
        text, because a step dict spans lines and a one-line grep would bless
        whatever key is added to it next."""
        src = self._src("backend/agent.py")
        needle = "trace.append("
        found, pos = 0, 0
        while True:
            i = src.find(needle, pos)
            if i < 0:
                break
            pos = i + len(needle)
            if src[i - 1].isalnum() or src[i - 1] == "_":
                continue  # a longer name that ends in the same text
            found += 1
            depth, j = 0, pos - 1
            while j < len(src):
                depth += (src[j] == "(") - (src[j] == ")")
                j += 1
                if depth == 0:
                    break
            call = src[i:j]
            for bad in ("tool_progress", "chunk", "on_output"):
                self.assertNotIn(bad, call,
                                 f"a transcript step now mentions {bad!r}: {call[:160]}")
        self.assertGreater(found, 0, "no trace.append found — the guard went blind")

    def test_the_store_has_never_heard_of_a_progress_event(self):
        self.assertNotIn("tool_progress", self._src("backend/store.py"))

    def test_a_sub_agents_tail_never_reaches_the_parents_card(self):
        """Delegated shells stream too — `_run_parallel` hands each sub-agent a
        call id — so a `tool_progress` from inside one would be attributed to the
        parent's card by tool *name*, interleaving two commands' output. Checked
        by running the forwarder over a fake stream, because the drop is a
        property of which kinds it enumerates, not of a list someone must
        remember to update."""
        from backend import agent

        def fake_run_turn(*a, **kw):
            yield {"type": "text", "delta": "the report"}
            yield {"type": "tool_start", "name": "run_shell", "id": "inner"}
            yield {"type": "tool_progress", "id": "inner", "name": "run_shell",
                   "chunk": "leaked tail", "elapsed_s": 1.0, "chars": 11}
            yield {"type": "tool_end", "id": "inner", "result": "x", "is_error": False}

        saved, out = agent.run_turn, {}
        agent.run_turn = fake_run_turn
        try:
            events = list(agent.run_subagent(
                "dig around", {"project": None, "session": "s", "depth": 0},
                out, "parent_card"))
        finally:
            agent.run_turn = saved
        kinds = [e.get("type") for e in events]
        self.assertIn("text", kinds, "the forwarder never ran — guard is blind")
        self.assertIn("tool_start", kinds)
        self.assertNotIn("tool_progress", kinds,
                         "a sub-agent's live tail would interleave into the "
                         "parent's tool card")

    def test_the_client_does_not_add_progress_to_the_transcript_array(self):
        """`streaming.msg.tools` is what gets rendered into the saved message;
        the progress case must touch the DOM card and nothing else."""
        app = self._src("static/app.js")
        start = app.index("case 'tool_progress'")
        case = app[start:app.index("case 'tool_end'", start)]
        # Strip line comments first: this guard is about what the branch *does*,
        # and the prose right above it explaining why it does not touch the
        # transcript array would otherwise fail the check it is describing.
        code = "\n".join(l for l in case.splitlines()
                         if not l.strip().startswith("//"))
        self.assertNotIn("streaming.msg.tools", code,
                         "a live chunk became part of the message record")
        self.assertIn("progressToolCard(m)", code)


class ShellCardFacts(unittest.TestCase):
    """Self-contained: this module's own helpers are about the SSE stream, and
    importing them here would tie a card test to fixtures it does not need.
    """

    def setUp(self):
        import pathlib
        import sys
        self.root = pathlib.Path(__file__).resolve().parents[1]
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        from backend import agent, sandbox
        self.agent, self.sandbox = agent, sandbox
        self.js = (self.root / "static" / "app.js").read_text(encoding="utf-8")
        self.src = (self.root / "backend" / "agent.py").read_text(encoding="utf-8")

    """The plan wants a tool card to answer two questions it could not: which
    sandbox backend ran a command, and what changed on disk. Both were already
    computed — `sandbox.run` returns `backend` on every path and keeps a `changed`
    diff — and then dropped, because the loop forwarded only the result *string*
    the model reads.
    """

    def setUp(self):
        # This file's own helpers are named for the streaming tests; the guards
        # below read the client and the loop source, so they carry their own paths
        # rather than borrowing names that mean something else here.
        import pathlib
        import sys
        self.root = pathlib.Path(__file__).resolve().parents[1]
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        from backend import agent, sandbox
        self.agent, self.sandbox = agent, sandbox
        self.js = (self.root / "static" / "app.js").read_text(encoding="utf-8")
        self.src = (self.root / "backend" / "agent.py").read_text(encoding="utf-8")

    def test_the_helper_reads_backend_and_change_count(self):
        payload = json.dumps({"ok": True, "backend": "tacit-micro",
                              "changed": {"count": 3, "added": ["a.py"],
                                          "modified": [], "removed": []}})
        self.assertEqual(self.agent._shell_card_fields("run_shell", payload),
                         {"backend": "tacit-micro", "changed": 3})

    def test_a_read_only_command_says_nothing_about_files(self):
        # Zero is not a fact worth a badge: every read would shout about it.
        payload = json.dumps({"ok": True, "backend": "none", "changed": {"count": 0}})
        self.assertEqual(self.agent._shell_card_fields("run_shell", payload),
                         {"backend": "none"})

    def test_only_run_shell_has_these_facts(self):
        payload = json.dumps({"backend": "none", "changed": {"count": 9}})
        for name in ("read_file", "write_file", "fetch", "task"):
            self.assertEqual(self.agent._shell_card_fields(name, payload), {})

    def test_garbage_or_missing_json_yields_nothing_rather_than_raising(self):
        # A tool result is model-visible text; it is not always our own JSON.
        for bad in ("", "not json", "[]", '"a string"', "{}", '{"changed": "x"}'):
            self.assertEqual(self.agent._shell_card_fields("run_shell", bad), {})
        # `{"backend": 1}` is NOT in this list on purpose: a non-string backend is
        # still a backend, and the helper coerces it to "1". Asserting {} there
        # tested a preference, not a failure mode.
        for bad in (None, 7, {}, []):
            self.assertEqual(self.agent._shell_card_fields("run_shell", bad), {})

    def test_sandbox_run_really_reports_both(self):
        # The helper is worthless if the field is not actually there.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            res = self.sandbox.run("echo hi", project=d, backend="none")
        self.assertEqual(res.get("backend"), "none")
        self.assertIn("changed", res)
        self.assertEqual(self.agent._shell_card_fields("run_shell", json.dumps(res)),
                         {"backend": "none"})

    def test_both_loops_carry_the_fields_to_record_and_event(self):
        self.assertEqual(self.src.count('_card = _shell_card_fields(c["name"], result)'), 2)
        # Four, not two: each site writes the field into the stored record AND the
        # live event, and the substring appears in both lines. Pinning it at two
        # would fail on a healthy tree.
        self.assertEqual(self.src.count('"elapsed_ms": _ms, **_card}'), 4)
        self.assertEqual(self.src.count('"is_error": failed, "elapsed_ms": _ms, **_card}'), 2)

    def test_the_client_renders_both_from_one_builder(self):
        self.assertIn("function toolCardMeta(m)", self.js)
        self.assertIn("'sandbox: ' + m.backend", self.js)
        self.assertIn("files changed", self.js)
        # One builder, two call sites — the live card and the rebuilt transcript
        # card used to assemble this slot separately.
        self.assertEqual(self.js.count("toolCardMeta("), 3)
        # The duration formatting survives in exactly one place: inside the builder.
        # Asserting it is gone outright would be wrong — and asserting the call site
        # by name is what let the old per-site pin pass off this very body.
        self.assertEqual(self.js.count("setToolMeta(card, meta)"), 2)
        self.assertEqual(self.js.count("fmtMs(m.elapsed_ms)"), 1)
        self.assertNotIn("fmtMs(t.elapsed_ms)", self.js)

    def test_a_planted_second_meta_builder_is_caught(self):
        planted = self.js.replace("  const meta = toolCardMeta(m);",
                                  "  const meta = fmtMs(m.elapsed_ms);", 1)
        self.assertNotIn("  const meta = toolCardMeta(m);", planted)


if __name__ == "__main__":
    unittest.main()

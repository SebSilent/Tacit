"""Repeat identity, evidence, and salvage-history contracts (Phase 2.5/2.6).

The behaviors pinned here were measured failure modes: long arguments that
differ only at the tail were merged into one repeat key; a cached read answered
stale bytes after a mutation because nothing invalidated it; an exit-1 test run
read as verification because its text lacked an error marker; an extracted
salvage write made the provider reject the next request as malformed history;
and a reply holding several fenced blocks had its first block paired with a
file it did not belong to.
"""

import json
import unittest

PAD = "x" * 200

SALVAGE_SET = {"write_file", "edit_file", "run_shell"}


def _j(text: str) -> str:
    return json.dumps(text)


def agent_max_uncapped() -> int:
    from backend import agent
    return agent.MAX_UNCAPPED_RETRIES


class EvidenceDrive(unittest.TestCase):
    """The scripted drive: model rounds and tool results staged per call."""

    def _drive(self, scripted, tool_results=None, steps=12):
        """``scripted(ask, n, msgs)`` answers model round n. ``tool_results`` is
        keyed (tool, call_no) -> (result, meta or None); call_no counts executed
        calls of that tool from 1, so per-call staging stays exact."""
        from backend import agent
        orig_stream, orig_tool = agent.engine.stream_chat, agent.call_tool
        asked: list[str] = []
        offered: list[list[str]] = []
        runs: list[tuple] = []
        counters: dict = {}

        def fake_stream(msgs, **k):
            offered.append([t["function"]["name"] for t in (k.get("tools") or [])])
            ask = str(next((m for m in reversed(msgs) if m.get("role") == "user"), {})
                      .get("content") or "")
            asked.append(ask)
            return iter(scripted(ask, len(asked), msgs))

        def fake_tool(n, a, ctx, out, call=None):
            no = counters.get(n, 0) + 1
            counters[n] = no
            runs.append((n, str(a.get("path") or a.get("command") or "")))
            result, meta = (tool_results or {}).get((n, no), ("did it", None))
            if meta is not None:
                out["meta"] = dict(meta)
            out["result"] = result
            return iter([])

        agent.engine.stream_chat = fake_stream
        agent.call_tool = fake_tool
        try:
            msgs = [{"role": "system", "content": "base"},
                    {"role": "user", "content": "Do the thing."}]
            events = list(agent.run_turn(msgs, project=None, max_steps=steps))
        finally:
            agent.engine.stream_chat, agent.call_tool = orig_stream, orig_tool
        return {"asks": asked, "offered": offered, "runs": runs, "events": events,
                "msgs": msgs}

    @staticmethod
    def _tool(name, ident, arguments):
        return {"id": ident, "name": name, "arguments": arguments}

    @staticmethod
    def _round(*calls):
        return [{"type": "tool_calls", "calls": list(calls)},
                {"type": "done", "model": None}]

    def _text_round(self, text):
        return [{"type": "text", "delta": text}, {"type": "done", "model": None}]

    def _length_round(self, *calls):
        return [{"type": "tool_calls", "calls": list(calls)},
                {"type": "done", "finish": "length", "model": None}]


class TestRecoveryBounds(EvidenceDrive):
    def test_the_uncapped_retry_is_bounded_per_turn(self):
        """The uncapped retry rescues a round our ceiling severed. It is a
        bound, not a licence: at most MAX_UNCAPPED_RETRIES extra rounds, so a
        model that is severed every step pays a bounded surcharge."""
        severed = '{"command": "echo'

        def scripted(ask, n, msgs):
            return self._length_round(self._tool("run_shell", f"s{n}", severed))

        res = self._drive(scripted, steps=8)
        retried = [e.get("message") or "" for e in res["events"]
                   if e.get("type") == "notify" and
                   "retrying the round uncapped" in (e.get("message") or "")]
        self.assertEqual(len(retried), agent_max_uncapped(), repr(retried))


class TestRepeatIdentity(EvidenceDrive):
    def test_arguments_differing_only_at_the_tail_are_distinct_calls(self):
        """The old 400-character key merged two long calls that differed in
        their tail, so the second was refused as a repeat it was not."""
        def scripted(ask, n, msgs):
            if n == 1:
                return self._round(self._tool(
                    "run_shell", "s1", '{"command": %s}' % _j(f"{PAD} echo alpha")))
            if n == 2:
                return self._round(self._tool(
                    "run_shell", "s2", '{"command": %s}' % _j(f"{PAD} echo omega")))
            return self._text_round("done")

        res = self._drive(scripted, tool_results={
            ("run_shell", 1): ("alpha output", None),
            ("run_shell", 2): ("omega output", None),
        })
        shells = [r for r in res["runs"] if r[0] == "run_shell"]
        self.assertEqual(len(shells), 2,
                         f"two different commands must each execute: {res['runs']}")
        self.assertEqual([e for e in res["events"]
                          if "BLOCKED:" in str(e.get("result") or "")], [])

    def test_a_repeated_read_after_a_write_executes_again(self):
        """The repeat cache exists to stop reruns of calls whose result cannot
        change. A write is precisely a change: the identical read afterwards
        must execute, not quote the bytes that mutation replaced."""
        def scripted(ask, n, msgs):
            if n in (1, 2, 4):
                return self._round(self._tool("read_file", f"r{n}", '{"path": "f.txt"}'))
            if n == 3:
                return self._round(self._tool(
                    "write_file", "w3", '{"path": "f.txt", "content": "v2"}'))
            return self._text_round("done")

        res = self._drive(scripted, tool_results={
            ("read_file", 1): ("v1", None),
            ("read_file", 2): ("v1", None),
            ("write_file", 1): ("wrote f.txt", None),
            ("read_file", 3): ("v2", None),
        })
        executed_reads = [r for r in res["runs"] if r[0] == "read_file"]
        self.assertEqual(len(executed_reads), 3,
                         f"the post-write read must execute again: {res['runs']}")
        self.assertEqual([e for e in res["events"]
                          if "BLOCKED:" in str(e.get("result") or "")], [])


SHELL_FAIL = {"status": "completed", "is_error": True, "exit_code": 2}
SHELL_PASS = {"status": "completed", "is_error": False, "exit_code": 0}


class TestVerificationEvidence(EvidenceDrive):
    """Verified means: a write, then a run that followed it and did not fail."""

    def _delivery_steps(self):
        """Write, a couple of probes, then the model stops on its own - the
        normal end-of-turn path, which is where the verify phase lives."""
        def scripted(ask, n, msgs):
            if "Answer now without using any more tools" in ask:
                return self._text_round("reported")
            if "Write it now with write_file" in ask:
                return self._round(self._tool(
                    "run_shell", "rp", '{"command": "python3 attack.py"}'))
            if n == 1:
                return self._round(self._tool(
                    "write_file", "w1", '{"path": "attack.py", "content": "x"}'))
            if n in (2, 3, 4):
                return self._round(self._tool(
                    "run_shell", f"p{n}", '{"command": "python3 attack.py"}'))
            return self._text_round("reported")
        return scripted

    def test_a_failing_run_does_not_verify_the_written_file(self):
        """A test that exits 1 is not verification, whatever its text says."""
        staged = {("write_file", 1): ("wrote attack.py", None)}
        for n in range(1, 20):
            staged[("run_shell", n)] = ("FAILED: assertion", dict(SHELL_FAIL))
        res = self._drive(self._delivery_steps(), tool_results=staged)
        phase = [o for o in res["offered"] if o and set(o) <= SALVAGE_SET]
        self.assertTrue(phase, "a failing check earns the bounded verify round")

    def test_a_passing_run_after_the_write_verifies_without_a_phase(self):
        staged = {("write_file", 1): ("wrote attack.py", None),
                  ("run_shell", 1): ("passed", dict(SHELL_PASS))}
        res = self._drive(self._delivery_steps(), tool_results=staged)
        phase = [o for o in res["offered"] if o and set(o) <= SALVAGE_SET]
        self.assertEqual(phase, [],
                         "a write followed by a passing run needs no salvage phase")

    def test_a_run_from_before_the_write_verifies_nothing(self):
        """The run happened, then the file changed: its passing exit says
        nothing about the current state."""
        def scripted(ask, n, msgs):
            if "Answer now without using any more tools" in ask:
                return self._text_round("reported")
            if "Write it now with write_file" in ask:
                return self._round(self._tool(
                    "run_shell", "rp", '{"command": "python3 attack.py"}'))
            if n == 1:
                return self._round(self._tool(
                    "run_shell", "p1", '{"command": "python3 attack.py"}'))
            if n in (2, 3):
                return self._round(self._tool(
                    "write_file", f"w{n}", '{"path": "attack.py", "content": "x"}'))
            return self._text_round("reported")

        staged = {("run_shell", 1): ("passed", dict(SHELL_PASS)),
                  ("write_file", 1): ("wrote attack.py", None),
                  ("write_file", 2): ("wrote attack.py", None)}
        res = self._drive(scripted, tool_results=staged)
        phase = [o for o in res["offered"] if o and set(o) <= SALVAGE_SET]
        self.assertTrue(phase, "a run that preceded the write is not verification")

    def test_the_distinct_failure_breaker_warns_then_stops(self):
        """A wall of failing variants burns steps without new information; a
        genuine fix loop resets the counter on every success, so it never
        trips."""
        def scripted(ask, n, msgs):
            return self._round(self._tool(
                "run_shell", f"f{n}", '{"command": "probe %d"}' % n))

        staged = {("run_shell", n): ("failed", dict(SHELL_FAIL))
                  for n in range(1, 30)}
        res = self._drive(scripted, tool_results=staged, steps=30)
        notes = [e.get("message") or "" for e in res["events"]
                 if e.get("type") == "notify"]
        self.assertTrue(any("failed in a row" in n and "hypothesis" in n.lower()
                            for n in notes), "six failures in a row warn once")
        self.assertTrue(any("turn stopped early" in n for n in notes),
                        "twelve failures in a row end the turn")
        executed = [r for r in res["runs"] if r[0] == "run_shell"]
        self.assertEqual(len(executed), 12, "the stop lands at the breaker bound")


class TestSalvageHistory(EvidenceDrive):
    def _salvage_drive(self, salvage_reply):
        """A turn that reaches for a file and misses, then stuck-exits into a
        salvage round whose scripted model answer is `salvage_reply`."""
        def scripted(ask, n, msgs):
            if "Write it now with write_file" in ask:
                return salvage_reply()
            if "Answer now without using any more tools" in ask:
                return self._text_round("reported")
            if n == 1:
                return self._round(self._tool(
                    "write_file", "w0", '{"path": "attack.py", "content": ""}'))
            return self._round(self._tool(
                "run_shell", f"p{n}", '{"command": "python3 probe.py"}'))

        tool_results = {("write_file", 1): ("ERROR: refused", None),
                        ("write_file", 2): ("wrote attack.py", None)}
        return self._drive(scripted, tool_results=tool_results, steps=12)

    def test_an_extracted_write_keeps_the_history_protocol_valid(self):
        """A bare tool message after an assistant that carried no tool_calls
        made the next request fail the provider's own shape check. The
        extracted write now rides a synthetic write_file call id."""
        def reply():
            return self._text_round(
                "Here is the fix:\n```python\nimport sys\nprint(\"ok\")\n```")

        res = self._salvage_drive(reply)
        wrote = [r for r in res["runs"] if r[0] == "write_file" and r[1] == "attack.py"]
        self.assertTrue(wrote, "the fenced block must be extracted to the named file")
        tool_rows = [m for m in res["msgs"] if m.get("role") == "tool"]
        self.assertTrue(tool_rows, "the salvage must record its result")
        by_id = {}
        for m in res["msgs"]:
            for tc in (m.get("tool_calls") or []):
                by_id[tc["id"]] = m
        for m in tool_rows:
            parent = by_id.get(m.get("tool_call_id"))
            self.assertIsNotNone(
                parent, f"tool row {m.get('tool_call_id')!r} has no answering call")

    def test_multi_block_extraction_with_one_target_is_refused(self):
        """Two fenced blocks and one intended file is a pairing guess, and a
        guess writes somebody's code into somebody else's file."""
        def reply():
            return self._text_round(
                "Part one:\n```python\na = 1\n```\nPart two:\n```python\nb = 2\n```")

        res = self._salvage_drive(reply)
        notes = [e.get("message") or "" for e in res["events"]
                 if e.get("type") == "notify"]
        self.assertTrue(any("ambiguous" in n for n in notes), notes)
        writes = [r for r in res["runs"] if r[0] == "write_file"]
        self.assertEqual(len(writes), 1,
                         "only the model's own refused attempt ran; no extraction write")


if __name__ == "__main__":
    unittest.main()
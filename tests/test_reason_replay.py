"""The thinking attractor: spam reasoning replayed back until it filled the turn.

A thinking model sometimes answers a replayed chain of thought with a disclaimer
about the replay instead of reasoning about the task; "There is no previous
thinking to reproduce. This is the start of our conversation, and I have not yet
engaged in any internal reasoning about a user query." One such block is a
harmless bad sample; sent back as `reasoning_content` on the next step it becomes
the example the next step imitates, and the turn spends its step budget writing
variations of the disclaimer. One real session logged 188 consecutive rows of it
(343 messages, 124 transcript rows) and produced no answer.

The opening sentence is stable across that whole family; 188 of 188 spam rows in
the logged session opened with it, and a sweep of 35 variants across other
sessions shared it, so `clean_reason` replaces any block that OPENS with the
phrase by a neutral note before the text is stored or replayed. Replaced rather
than dropped because DeepSeek returns 400 for a tools request whose assistant
turns omit `reasoning_content` entirely.

Four places had to agree, and each is pinned here:
  * `clean_reason`; the gate itself, with the cases that must NOT fire (real
    reasoning, a mere quotation, an empty reason).
  * `transcript_messages(reasoning=True)`; what the next turn actually reads
    back; this is the link that turned one bad sample into an attractor.
  * the turn loop; a step's reasoning reaches the trace, the trace reaches the
    store, and the store reaches the next request, so the end-to-end assertion
    runs a turn with a fake engine and inspects what the next turn would send.
  * compaction: the tail kept through a compaction must still contain the
    newest real reasoning block. A tail that is only the newest user message
    shows a thinking model a transcript with no reasoning anywhere, and it
    answers by narrating the absence. The same failure, entered without any
    spam ever having been replayed.

No test here reaches a network or a real model.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import agent, config  # noqa: E402
from backend.ai import prompts  # noqa: E402
from tests.helpers import state_paths  # noqa: E402

SPAM = ("There is no previous thinking to reproduce. This is the start of our "
        "conversation, and I have not yet engaged in any internal reasoning "
        "about a user query.")
REAL = ("The user asked for the fix in the parser. The failing branch is the "
        "one that assumes `args` is always a dict; when the call was severed "
        "mid-arguments it is a string, so I should guard the isinstance first.")


class Isolated(unittest.TestCase):
    """Points every storage path under the user's home at a temporary one.

    Same reason as test_dump_hint.py: run_turn reads the real prefs file for the
    turn budget, so an operator's preference would decide whether these pass.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {key: getattr(config, key) for key in self._keys}
        self._home = config.HOME
        self.addCleanup(self._restore_config)
        config.HOME = home
        for key in self._keys:
            target = home / Path(self._orig[key]).name
            setattr(config, key, target)
            if key.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)

    def _restore_config(self):
        config.HOME = self._home
        for key, value in self._orig.items():
            setattr(config, key, value)


class TestCleanReason(unittest.TestCase):
    """The gate. It must catch the family, and it must not catch reasoning."""

    def test_the_disclaimer_is_replaced(self):
        self.assertEqual(agent.clean_reason(SPAM), agent.REASON_ELIDED)

    def test_the_family_opens_with_it_regardless_of_case_or_padding(self):
        # The logged session's rows arrived with leading whitespace and the
        # model does not capitalise consistently; the match is on the opening
        # words, lowercased.
        for variant in (SPAM, "  " + SPAM, SPAM.lower(),
                        SPAM.replace("reproduce", "reproduce.")):
            self.assertEqual(agent.clean_reason(variant), agent.REASON_ELIDED,
                             repr(variant[:60]))

    def test_real_reasoning_passes_through_untouched(self):
        self.assertEqual(agent.clean_reason(REAL), REAL)

    def test_a_mere_quotation_is_not_reasoning_about_the_replay(self):
        # A model that quotes the phrase mid-block ("the previous turn said
        # 'there is no previous thinking to reproduce', which was wrong") is
        # reasoning about the transcript, not spamming the disclaimer. Only a
        # block that OPENS with it is replaced.
        quoted = f"The last block said: {SPAM!r}, that was wrong, because ..."
        self.assertEqual(agent.clean_reason(quoted), quoted)

    def test_an_empty_reason_stays_empty(self):
        # Rows that never reasoned must not grow a key out of the gate.
        for empty in ("", "   ", None):
            self.assertEqual(agent.clean_reason(empty), "")

    def test_a_non_string_cannot_crash_the_transcript(self):
        self.assertEqual(agent.clean_reason(7), "")


class TestTranscriptReplay(unittest.TestCase):
    """What the next turn reads back; the link that made one bad sample an
    attractor."""

    def _rows(self, *reasons):
        return [{"role": "user", "content": "go"},
                *[{"role": "assistant", "content": f"step {i}", "reason": r}
                  for i, r in enumerate(reasons)]]

    def test_spam_never_reaches_the_model_back(self):
        rows = self._rows(SPAM, SPAM, SPAM)
        msgs = agent.transcript_messages(rows, reasoning=True)
        blob = "".join(str(m.get("reasoning_content") or "") for m in msgs)
        self.assertNotIn(agent.REASON_ATTRACTOR, blob.lower())
        self.assertEqual(blob.count(agent.REASON_ELIDED), 3)

    def test_real_reasoning_is_replayed_verbatim(self):
        rows = self._rows(REAL)
        msgs = agent.transcript_messages(rows, reasoning=True)
        self.assertEqual(msgs[1]["reasoning_content"], REAL)

    def test_mixed_rows_keep_their_own_fate(self):
        rows = self._rows(SPAM, REAL)
        msgs = agent.transcript_messages(rows, reasoning=True)
        self.assertEqual(msgs[1]["reasoning_content"], agent.REASON_ELIDED)
        self.assertEqual(msgs[2]["reasoning_content"], REAL)

    def test_reasoning_off_carries_no_key_at_all(self):
        rows = self._rows(SPAM, REAL)
        msgs = agent.transcript_messages(rows, reasoning=False)
        self.assertFalse(any("reasoning_content" in m for m in msgs))


class TestCompactionKeepsTheNewestReasoning(Isolated):
    """The tail a compaction keeps must still show real reasoning.

    `_tail_split` picks the tail by token budget from the newest row backwards,
    and the newest assistant row carrying reasoning can fall on the fold side.
    The model then sees summary + user message and no reasoning anywhere, and a
    thinking model answers that by narrating the absence. Seeding the tail with
    the newest real block keeps the replay loop from ever starting.
    """

    def _rows(self, reason_at=None, key="reason"):
        rows = [{"role": "assistant", "content": f"step {i} " + "w" * 400}
                for i in range(10)]
        if reason_at is not None:
            rows[reason_at] = dict(rows[reason_at], **{key: REAL})
        return rows

    def test_the_tail_is_walked_back_to_the_newest_real_reasoning(self):
        rows = self._rows(reason_at=4)
        old, tail = agent._tail_split(rows, 200, keep_reason=True)
        self.assertEqual(len(old), 4)
        self.assertIs(tail[0], rows[4])
        self.assertEqual(tail[0]["reason"], REAL)

    def test_the_wire_shape_is_recognised_too(self):
        # Live rows carry reasoning under `reasoning_content`, not `reason`.
        rows = self._rows(reason_at=4, key="reasoning_content")
        old, tail = agent._tail_split(rows, 200, keep_reason=True)
        self.assertIs(tail[0], rows[4])

    def test_an_elided_marker_is_not_planted_as_a_seed(self):
        # The marker is what the imitation loop feeds on; seeding it on purpose
        # would hand the loop a fresh sample on every compaction.
        rows = self._rows()
        rows[4] = dict(rows[4], reason=agent.REASON_ELIDED)
        plain_old, plain_tail = agent._tail_split(rows, 200)
        old, tail = agent._tail_split(rows, 200, keep_reason=True)
        self.assertEqual(old, plain_old)
        self.assertEqual(tail, plain_tail)

    def test_the_walk_never_empties_the_fold_side(self):
        # Reasoning only on the very first row: extending to it would leave
        # nothing to summarise, so the guarantee yields and the note carries it.
        rows = self._rows(reason_at=0)
        old, tail = agent._tail_split(rows, 200, keep_reason=True)
        # The walk stops before index 0, so the fold side keeps its six rows and
        # the reasoning row is folded into the summary, where the note covers it.
        self.assertEqual(len(old), 6)
        self.assertEqual(old[0]["reason"], REAL)
        self.assertIs(tail[0], rows[6])

    def test_compact_messages_seeds_the_tail_for_a_thinking_model(self):
        config.save_registry({"default": "p/m", "providers": {
            "p": {"baseUrl": "http://x", "apiKey": "k",
                  "models": [{"id": "m", "contextWindow": 100000,
                              "reasoning": True}]}}})
        msgs = ([{"role": "system", "content": "standing prompt"},
                 {"role": "user", "content": "THE TASK STATEMENT"}]
                + [{"role": "assistant", "content": f"step {i} " + "w" * 2000}
                   for i in range(10)])
        msgs[6]["reason"] = REAL
        orig = agent.engine.chat
        agent.engine.chat = lambda m, **kw: {"content": "SUMMARY OF THE WORK"}
        self.addCleanup(setattr, agent.engine, "chat", orig)
        info = agent.compact_messages(msgs, ref="p/m", budget_chars=8000)
        self.assertIsNotNone(info)
        # The reasoning row moved from the fold side into the kept tail.
        self.assertEqual(msgs[3]["reason"], REAL)
        self.assertEqual(msgs[2]["content"][:len(prompts.SUMMARY_MARK)],
                         prompts.SUMMARY_MARK)
        self.assertEqual(info["kept"], 6)


class TestTheTurnLoop(Isolated):
    """End to end: a step's reasoning reaches the trace, the trace is what the
    epilogue stores, and the store is what the next request is built from."""

    def test_a_spamming_step_cannot_reach_the_next_request(self):
        seen = []

        def fake_stream(msgs, **k):
            seen.append(msgs)
            # Step 1: the model disclaims instead of reasoning, then answers.
            return iter([{"type": "reason", "delta": SPAM},
                         {"type": "text", "delta": "Done."},
                         {"type": "done", "model": {"ref": "p/m"}}])

        orig = agent.engine.stream_chat
        agent.engine.stream_chat = fake_stream
        self.addCleanup(setattr, agent.engine, "stream_chat", orig)
        trace = []
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "do the thing"}]
        list(agent.run_turn(msgs, project=None, max_steps=1, trace=trace))

        # The epilogue stores one assistant row per trace step, `reason` riding
        # along (session_runtime.py:739-747). Build the next turn's request the
        # way the runtime does.
        rows = [{"role": "user", "content": "do the thing"}]
        rows += [{"role": "assistant", "content": s.get("text") or "",
                  "reason": s.get("reason") or ""} for s in trace]
        nxt = agent.transcript_messages(rows, reasoning=True)
        blob = "".join(str(m.get("reasoning_content") or "") for m in nxt)
        self.assertNotIn(agent.REASON_ATTRACTOR, blob.lower(),
                         "the disclaimer came back to the model verbatim")
        self.assertIn(agent.REASON_ELIDED, blob)
        # And the answer itself is still there; the gate must not eat the turn.
        self.assertIn("Done.", "".join(m.get("content") or "" for m in nxt))

    def test_a_real_reasoning_step_is_stored_whole(self):
        def fake_stream(msgs, **k):
            return iter([{"type": "reason", "delta": REAL},
                         {"type": "text", "delta": "Done."},
                         {"type": "done", "model": {"ref": "p/m"}}])

        orig = agent.engine.stream_chat
        agent.engine.stream_chat = fake_stream
        self.addCleanup(setattr, agent.engine, "stream_chat", orig)
        trace = []
        list(agent.run_turn([{"role": "user", "content": "go"}],
                            project=None, max_steps=1, trace=trace))
        self.assertEqual(trace[0]["reason"], REAL)


class TestGuardHasTeeth(unittest.TestCase):
    """Each check fails when its own property is broken, and the untouched
    source still passes it in the same breath."""

    def test_a_gate_that_dropped_instead_of_replaced_is_caught(self):
        # Dropping the key entirely is the regression the note warns about:
        # DeepSeek 400s on a tools request whose assistant turns omit it.
        self.assertTrue(agent.clean_reason(SPAM))
        dropped = lambda t: "" if (t or "").strip().lower().startswith(  # noqa: E731
            agent.REASON_ATTRACTOR) else (t or "").strip()
        self.assertEqual(dropped(SPAM), "")
        rows = [{"role": "assistant", "content": "x", "reason": SPAM}]
        msgs = agent.transcript_messages(rows, reasoning=True)
        self.assertTrue(msgs, "the row itself must survive the gate")
        self.assertIn("reasoning_content", msgs[0],
                      "a replaced note keeps the key; a dropped one does not")

    def test_a_transcript_that_replays_verbatim_is_caught(self):
        rows = [{"role": "assistant", "content": "x", "reason": SPAM}]
        self.assertNotIn(agent.REASON_ATTRACTOR,
                         agent.transcript_messages(rows, reasoning=True)[0]
                         .get("reasoning_content", "").lower())
        # Without the gate the attractor rides back verbatim; the bug itself.
        raw = rows[0]["reason"]
        self.assertIn(agent.REASON_ATTRACTOR, raw.lower())
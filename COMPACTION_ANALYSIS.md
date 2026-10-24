# Why compaction got aggressive between 1.0.3 and 1.0.7

Investigation notes. 1.0.3 = commit `aa2aa7e`, 1.0.7 = `c1a18e7` ("Version 1.0.7"; HEAD
is `3dcdfb6`, a frontend-only site rebuild on top of it (no backend changes)). Scratch
checkouts
of both versions were compared file-by-file (recreate with
`git worktree add .tmp103 aa2aa7e` / `git worktree add .tmp107 c1a18e7`).

**Headline:** one commit, `c75c444` "Persist compaction, fail unknown models", shipped
between 1.0.3 and 1.0.4, inverted the pressure-valve hierarchy. In 1.0.3, elision carried
the context (ephemeral, keeps up to a full wire-budget of real rows) and compaction was a
rare, mostly-dormant turn-start event. In 1.0.7, compaction is the primary valve: it fires
on the stored record, fires mid-turn, and every fold is written into the stored record
permanently, while keeping at most 8,000 tokens verbatim, the same cap 1.0.3 had, but
which 1.0.3 only ever applied ephemerally.

**Also important:** the working tree currently holds *uncommitted* changes to
`backend/agent.py`, `backend/config.py`, `backend/session_runtime.py`, four docs and
`tests/test_claims.py` that implement most of the fixes this analysis recommends (tail
8000 → 200000, share-of-region tail trigger, gated persistence, newest-slice digest, and
(added later in this session) part-summarising of oversized digests).
They are coherent and self-consistent (docs and tests updated to match); they were
committed as the 1.0.8 compaction fix (see "State of the working tree" below, kept as
written). Everything in this file describes
**HEAD** unless marked *working tree*.

## How compaction works in 1.0.7

Two valves, in order of when they act:

1. **Elision** (`agent.windowed`, applied at send time): the wire is trimmed to
   `context_budget(ref)` chars: pin rows + newest rows that fit, older rows replaced by
   an `ELIDED` marker. Ephemeral: the stored record keeps every row. This is 1.0.3's
   primary valve and it still exists, unchanged.
2. **Compaction** (summary + tail): rewrites the transcript into one summary row plus a
   verbatim tail. Two entry points:
   - *Between turns*: `compact_now` at `session_runtime.py:893`, before the user's row is
     appended, gated on `agent.should_compact(rec)` (`agent.py:1101`), announced with a
     notify, record saved.
   - *Mid-turn*: inside `run_turn` (`agent.py:2693`), when the un-elided wire exceeds
     `context_budget(ref)`, at most 3 folds per turn. The fold emits a `compaction` event
     carrying `summary`/`kept_tokens` (`agent.py:2707`); the parent captures it
     (`session_runtime.py:684-703`) and at the epilogue writes it into the stored record
     via `agent.apply_stored_summary` (`agent.py:1386`, called at
     `session_runtime.py:762`): **unconditionally at HEAD**.

The tail is `_tail_budget` (`agent.py:1139`): `max(500, min(COMPACT_TAIL_TOKENS,
trigger × 0.4))`. With `COMPACT_TAIL_TOKENS = 8000`, the 40% share never binds for any
window ≥ 20k tokens: every compaction keeps ~8k tokens verbatim, everything older
becomes one summary row.

## What changed 1.0.3 → 1.0.7

| Aspect | 1.0.3 (`aa2aa7e`) | 1.0.7 HEAD (`c1a18e7`) | Commit |
|---|---|---|---|
| `should_compact` | `billed > fill_target` | `max(billed, stored_tokens(msgs)) > fill_target` | `c75c444` |
| Mid-turn fold persistence | none (wire-only; parent rebuilt the record from `trace` verbatim, `.tmp103/session_runtime.py:568` only bumps metrics) | `apply_stored_summary` at turn end, unconditional | `c75c444` |
| Tail cap (`COMPACT_TAIL_TOKENS`) | 8000 | 8000 (200000 only in the uncommitted working tree) | n/a |
| Tail trigger | 0.4 × `fill_target(window)` | same at HEAD; share-of-region in working tree | n/a |
| Summariser model | `TACIT_COMPACT_MODEL` override, else session model | session model only (override removed) | `c75c444` |
| Digest input cap | oldest 160k chars of the region | oldest 160k chars (newest in working tree) | n/a |
| Mid-turn trigger/budget | `wire_chars > context_budget(ref)`, ≤3/turn | identical | n/a |
| Elision, prune, digest caps | identical | identical | n/a |
| UI | notify only | + `compaction_start` spinner | `91d11c6` |

`398232c`/`efbddf3`/`3f3dac5` are reasoning-replay work (keep newest reasoning through
compaction), not frequency drivers.

## Why it feels like constant mid-turn compaction with heavy loss

1. **Valve inversion.** 1.0.3: when the wire overflowed, elision kept up to a full
   wire-budget (~fill_target tokens) of *newest real rows*, ephemerally; the record
   accumulated everything; permanent compaction only fired at turn start, gated on
   `billed`, which stayed small after folds/elision, so the check mostly slept (the
   commit's own anecdote: 640k tokens of transcript, meter at 11%, asleep for sixteen
   turns). 1.0.7: compaction fires on the *stored* measure and mid-turn folds persist, so
   the record never accumulates real rows past the fold point and elision (which could
   carry ~167k real tokens on a 200k window) almost never gets to work.
2. **Persistence.** Every mid-turn fold (up to 3 per turn) is now written into the record
   at turn end. In 1.0.3 the identical folds died at the process boundary. The same
   trigger, the same 8k tail, but permanent.
3. **Trigger widening.** `max(billed, stored)` was a deliberate fix for the sleeping
   trigger, but it converted silent bloat into aggressive permanent compaction: turn-start
   compaction now fires whenever the record crosses the fill target, including states
   (post-fold small billed) where 1.0.3 showed nothing.
4. **The 8k tail is ~20× smaller than the elision budget it displaces** (167k tokens on a
   200k window). Each event collapses the model's verbatim context from "newest
   167k–500k tokens of real rows" to "one summary + 8k tokens".
5. **Lossy digest loop.** Rows are truncated to 2000 chars, tool results pruned to 400
   before summarising, the digest input capped at 160k chars (oldest slice at HEAD: the
   newest rows of the folded region never reach the summariser). The model re-reads what
   it lost, regenerates transcript, re-hits the budget, folds again.

## The numbers

`CHARS_PER_TOKEN = 4` (`backend/tokens.py:19`). `context_fill_target(window)` =
`max(window × 0.5, window − 33,000)` (`backend/config.py:213`): 1M → 967k, 200k → 167k.
`context_budget` = `clamp(fill_target × 4 chars, 24k, 2M)` (`backend/agent.py:974`,
identical in both versions).

### 1M-window model

| | 1.0.3 | 1.0.7 |
|---|---|---|
| Wire cap | 2M chars ≈ 500k tokens | same |
| Between-turns trigger | billed ≤ ~500k < 967k → **never fires** | stored > 967k → **fires** |
| What the model works with | newest ~500k tokens of real rows (elision) | one summary + ≤8k tokens |
| Mid-turn fold | fires at 2M chars, wire-only, record untouched | fires at 2M chars, **persisted** |

### 200k-window model

| | 1.0.3 | 1.0.7 |
|---|---|---|
| Wire cap | 668k chars ≈ 167k tokens ≈ fill target | same |
| Between-turns trigger | billed crosses 167k only while the wire is full; after a mid-turn fold billed drops, so it mostly stayed asleep | stored crosses 167k → fires |
| What the model works with | newest ~167k tokens of real rows | one summary + ≤8k tokens |
| Mid-turn fold | wire-only, ephemeral | persisted |

That last row is the regression users feel: the model went from "sees the newest
167k–500k tokens of actual rows" to "sees a summary plus 8k tokens", and the rows are
gone from the record too, so it must re-read files it already read.

## Secondary aggravators (all present in 1.0.7, most in 1.0.3 too)

- **Summary input cap is oldest-first at HEAD.** `_summarise` caps the digest at
  `SUMMARY_INPUT_MAX_CHARS = 160,000` chars with `transcript[:160000]`
  (`backend/agent.py:1293`). When the folded region is bigger than that, the *newest*
  rows (the ones just before the kept tail, usually the most valuable) are cut from
  the summariser's view. (Working tree flips this to the newest slice.)
- **Digest truncation.** `_digest_row`/`_digest_wire` cap row content at 2000/1500
  chars, tool args at 200, results at 400. File contents cannot survive into a
  summary at all: only "read X, ran Y".
- **`_prune_old_output` runs before summarising** (`compact_history:1369`,
  `compact_messages:1471`): tool outputs > 1200 chars are pruned to 400 chars in the
  folded region, so the digest sees pruned results and the record loses the rest.
- **Up to 3 mid-turn folds per turn** (`compactions < 3`, `backend/agent.py:2693`),
  each refining the previous summary; loss compounds within one long turn.
- **The summariser is always the session's own model** (`COMPACT_MODEL` was removed
  in this range): a mid-turn pause of seconds, and summary quality tracks whatever
  model the session runs.
- **The local estimator can fire early.** `stored_tokens` estimates at 4 chars/token;
  code-heavy transcripts often run wider, so the stored half of the trigger can cross
  the fill target before the provider's own count would.

## State of the working tree (committed since, as 1.0.8)

`git status` shows modifications to `backend/agent.py`, `backend/config.py`,
`backend/session_runtime.py`, `docs/{architecture,configuration,cost-and-profiles,
data-and-privacy,isolation-and-audit,tools}.md` and `tests/test_claims.py`
(~50 insertions, 24 deletions). They implement, as one coherent change:

1. `COMPACT_TAIL_TOKENS` 8000 → **200000** (`config.py:124`), documented as a backstop.
2. `_tail_budget` becomes "a share of the region being folded, under an absolute
   ceiling": the share is the rule, the ceiling the backstop.
3. `compact_history` triggers the tail on `_chars(rest) // 4` (share of the region being
   folded) instead of `fill_target(window)`: a small record now folds cleanly instead of
   the tail covering the whole region and refusing.
4. `_summarise` keeps the **newest** 160k chars of the region (`transcript[-...]`);
   superseded later in this session by part-summarising (see Recommendations, item 3).
5. `_drive_turn` persists a mid-turn fold **only when `agent.should_compact(rec)`**,
   i.e. only when the stored record itself is over the fill target, the point where
   between-turns compaction would fold it anyway. A fold the wire needed but the record
   did not stays wire-only, as in 1.0.3.
6. Docs and tests updated to match (the tail-budget test now pins the share rule).

**Validated (this session):** every hunk of the diff reviewed against the 1.0.3/1.0.7
comparison; the full suite passes (1371 passed, 1 skipped, 29 subtests). Code-review
findings, all sound:

- `apply_stored_summary` keeps the stored tail at least as wide as the fold's tail
  *plus* every row the turn appended after it (`agent.py:1414-1424`), so the fold can
  never reach rows the summary does not describe.
- The mid-turn path (`compact_messages:1473`) passes `budget_chars // 4` (the wire
  budget, which *is* the mid-turn region's upper bound), so mid-turn folds keep
  `min(200000, 0.4 × fill_target)` ≈ 67k tokens on a 200k window instead of 8k.
- The gate's negative branch was untested. Added
  `test_a_fold_the_record_did_not_need_stays_wire_only` (`tests/test_claims.py`): a
  mid-turn fold over a record under the fill target must leave the record untouched
  (no summary row, both trace steps present) while the ledger still records the wire
  saving. It passes; first run failed only because the test record's `id` did not match
  the sid it was stored under (the anti-resurrection guard refused the save).

This restores 1.0.3's behaviour for the common case (mid-turn folds ephemeral) and fixes
the 8k-tail collapse. It does **not** change the widened `should_compact` trigger, the
digest caps, or the 3-folds-per-turn cap.

## Recommendations

1. **Review and commit the working-tree change**: it is exactly the fix this analysis
   would prescribe, and it is currently one `git checkout .` away from being lost.
   Done: committed as the 1.0.8 compaction fix.
2. Consider making the between-turns trigger elision-aware: compact when the record
   exceeds what elision can actually carry (rows the model loses), not merely when
   `stored > fill_target`; that would also spare sessions whose wire is comfortably
   under budget.
   Left as is, deliberately: the trigger already fires exactly when the record
   outgrows what elision can carry, because elision keeps the same tail budget the
   fold keeps; a record past `fill_target` is a record whose overflow is being
   dropped row by row with no note. An elision-aware trigger would only move the
   same fold earlier, and would add a second measure to keep in step with
   `_tail_budget`.
3. Consider chunk-summarising (or raising `SUMMARY_INPUT_MAX_CHARS`) so the folded
   region's far end is not silently dropped from the digest.
   Done: `_summarise_region` splits an oversized digest at row boundaries
   (`_chunk_digest`), summarises each part, and merges the part notes into the one
   note, bounded at `_SUMMARY_PARTS_MAX = 8` parts with the oldest dropped past the
   bound. Raising the cap alone was rejected: the cap also protects the summariser
   call itself from overflowing the session's window, and a flat raise would break
   small-window models.
4. Keep the tail ceiling tied to the window rather than a flat constant, so the 200k
   backstop cannot itself bind on small windows.
   Left as is, deliberately: `TACIT_COMPACT_TAIL_TOKENS` is an operator escape hatch,
   and a window-derived ceiling would make the knob's effect depend on the model in
   a way the docs cannot state plainly. On a window small enough for the 200k
   ceiling to bind, `context_budget`'s own reserve already caps the wire first.

Stopgaps without code changes (superseded by the working-tree fix, listed for
environments pinned to HEAD):

- `TACIT_COMPACT_MID_TURN=0`: restores wire-only mid-turn behaviour (no fold → no
  `compaction` event → nothing persisted). The between-turns trigger stays
  stored-based.
- `TACIT_COMPACT_TAIL_TOKENS=<much larger>`: widens what every compaction keeps.
- `TACIT_SUMMARY_INPUT_MAX=<larger>`: widens what the summariser sees.
- Per-model `context_budget` profile override: raises the wire cap and, with it,
  every trigger derived from it.

## Verification notes

Checked directly, in both versions: both `should_compact` bodies; both `context_budget`
bodies; all compaction constants in both `config.py` files; the mid-turn trigger and cap
(`agent.py:2693` vs `.tmp103/agent.py:2428`); the persistence path (`agent.py:2690-2718`
→ `session_runtime.py:684-703` → epilogue `748-766` → `apply_stored_summary`
`agent.py:1386`); `_tail_budget`, `_msg_tokens`, `_digest_row`, `_digest_wire`,
`_summarise`, `_stored_pin`; the turn-start call site (`session_runtime.py:893`, with
notify, in both); `CONTEXT_RESERVE_TOKENS = 33000` in both configs; tests
(`tests/test_claims.py` pins the new behaviour symbolically: `COMPACT_TAIL_TOKENS` by
name, `_tail_budget(1_000_000) == config.COMPACT_TAIL_TOKENS`, `<= 4000 × share`, and
`should_compact`'s stored measure at 1173-1205, so the working-tree changes do not break
them); the full uncommitted diff (`git diff`).

Earlier open questions, now resolved: the turn-start compaction is automatic (not
hand-triggered) and was announced in 1.0.3 too, so visibility is not the regression;
persistence is. The summariser-model change (`COMPACT_MODEL` removal) is real but a
quality/cost change, not a loss driver.
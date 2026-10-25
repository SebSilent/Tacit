# Compaction analysis: why sessions compact so often and lose context (1.0.3 → 1.0.8)

Status: concluded. Fixes 1, 2 and 4 under "Suggested fixes" shipped in 1.0.9;
fix 3 (summariser model choice) remains open. Everything below is verified against
the code unless marked **unconfirmed**. Version under review: `1.0.8` (VERSION file) plus two
commits on top (`3b1e648` rebuild site, `3b24560` archive-before-fold). Baseline for
comparison: v1.0.3 = commit `aa2aa7e` (no git tags exist; versions are plain commits
titled "Version 1.0.x").

## The complaint

- Sessions get compacted far more often than before.
- Compaction now fires mid-turn as well as between turns.
- Information/context that used to survive is being lost.
- A compaction appears to freeze the whole chat, not just the compacting session.

## What did NOT change between 1.0.3 and now (ruled out as causes)

Diffed `aa2aa7e..HEAD` for `backend/config.py`, `backend/agent.py`,
`backend/ai/prompts.py`, `backend/session_runtime.py`. Identical in both versions:

- All trigger constants: `AGENT_CONTEXT_BUDGET=120000`, `CONTEXT_BUDGET_MIN=24000`,
  `CONTEXT_BUDGET_MAX=2000000`, `COMPACT_AT=0`, `CONTEXT_RESERVE_TOKENS=33000`,
  `CONTEXT_FILL_FLOOR=0.5`, `CHARS_PER_TOKEN=4` (`backend/tokens.py:19`).
- The fill target itself, `config.context_fill_target(window)` =
  `max(window*0.5, window-33000)` (config.py:238-240).
- The mid-turn trigger condition and its cap — `not chat and COMPACT_MID_TURN and
  compactions < 3 and wire_chars(messages) > budget` — is character-for-character the
  same logic in both versions (v1.0.3 agent.py:2428, HEAD agent.py:2806). Mid-turn
  compaction is not new; it existed in 1.0.3 with the same 3-folds-per-turn cap.
- Tail shape constants: `COMPACT_TAIL_MIN_MESSAGES=4`, `COMPACT_TAIL_MAX_MESSAGES=40`,
  `COMPACT_TAIL_SHARE=0.4`, `COMPACT_PRUNE_KEEP_CHARS=400`,
  `COMPACT_PRUNE_MIN_CHARS=1200`, `SUMMARY_INPUT_MAX_CHARS=160000`,
  `COMPACT_KEEP_TAIL=6`.

So "mid-turn compaction was added recently" is false — it predates 1.0.3. What changed
is *when the between-turns trigger fires* and *what a fold costs*, below.

## What DID change (the candidates for the regression)

### 1. `should_compact` now also reads the stored transcript — the "so often" candidate

- v1.0.3 (agent_103.py:1102-1119): compact when the **billed** prompt tokens of the
  last provider call exceed the fill target; a local estimate only when the window is
  known but the provider reported no usage.
- HEAD (agent.py:1139-1159): compact when `max(billed, stored_tokens(msgs))` exceeds
  the fill target. The comment says this was added because a session carrying 640K
  tokens of transcript sat at 11% on the meter with the check asleep for sixteen turns
  — the billed number alone misses work a mid-turn fold removed from the wire.

Effect: compaction now fires on **store size**, not just wire size. The stored record
holds everything (full tool results, reasoning rows); the wire copy is what elision
actually sends. Any session whose stored transcript is large but whose wire is small
(elision doing its job) now trips the trigger every turn start, where 1.0.3 left it
alone. Each trip is a summariser call that rewrites history — which is exactly
"compacted so often" plus "losing context that was fine before": rows are being folded
that would never have been sent anyway.

Secondary effect interacting with (2): after a mid-turn fold the wire shrinks but the
store does not; the fold becomes real only at the epilogue, and only when the *stored*
record is still over target (session_runtime.py:755-766 — "A fold the wire needed but
the record did not stays wire-only"). So it converges rather than loops: one fold per
session that crosses the store threshold. The extra frequency comes from the stored
measure tripping at all on sessions whose wire was fine, plus a systematic bias:
`estimate_tokens` uses 4 chars/token (tokens.py:19), which over-counts code- and
JSON-heavy transcripts (~3-3.5 chars/token real), so `max(billed, stored)` can trigger
on sessions the provider itself says are well under target. **No live session traced —
frequency claims rest on code reading, not a measured transcript.**

### 2. Tail budget ceiling 8,000 → 200,000 tokens (config.py:124)

`COMPACT_TAIL_TOKENS` went from `8000` (v1.0.3) to `200000` (HEAD). This keeps *more*
verbatim per fold — less loss per compaction, and the commit comment says the 8K
ceiling collapsed a 967K-token transcript to a summary plus 8K rows. Directionally
this is a fix, not a regression. But it makes every fold keep a much larger tail, and
combined with (1) the post-fold store is bigger, which can re-arm the trigger sooner.

### 3. The summariser now always runs on the session's own model (config.py:137-143)

v1.0.3 had `COMPACT_MODEL` (`TACIT_COMPACT_MODEL`) to nominate a cheap summariser.
HEAD removed it ("a summary could be written by a model nobody had chosen"). Now every
compaction is a full-strength model call with up to `SUMMARY_INPUT_MAX_CHARS=160000`
chars (~40K tokens) of transcript as input. On a large/slow model this is a slow,
expensive call — and it runs:

- inside `start_turn` (turn-start compaction, `session_runtime.compact_now`), which
  chat.py:343 runs via `loop.run_in_executor(None, ...)` — the loop's **default**
  executor; and
- inside the turn worker (mid-turn fold, agent.py:2812), announced to the UI as
  `compaction_start` → the composer shuts and the meter pulses.

### 4. Archive-before-fold (commit `3b24560`, newest change)

Every fold now writes the rows it is about to drop verbatim to
`<session>.archive.jsonl` before pruning (agent.py `compact_history`, new
`backend/archive.py`), readable via a `session_archive` tool that is off by default.
Additive, reduces loss; not a cause. But it is the newest behaviour change in the
compaction path and the most likely candidate for *any* new slowdown at fold time
(extra file writes per fold, per row).

### 5. `398232c` "Keep newest reasoning through compaction"

Keeps reasoning rows through folds — again less loss, not more.

## The "entire chat freezes" symptom

Ruled out by reading the code:

- Bus events are sid-scoped: `compacting` carries `sid`
  (session_runtime.py:518), and the frontend gates on `m.sid === activeId`
  (app.js:928) — one session's fold cannot flip another window's composer.
- The engine has no global lock: `ai/engine.py` builds a fresh `httpx.Client` per
  call; concurrent model calls are not serialised.
- `store._write_lock` (store.py:104) is global but held only for one record's
  read-modify-write; a big record makes it longer, not cross-session-stuck.
- `start_turn` returns after spawning the driver thread (session_runtime.py:1055-1060);
  it does not hold its executor thread for the whole turn.

**Confirmed: everything interactive shares one default thread pool.** No
`set_default_executor` / `max_workers` anywhere in `backend/` — the pool is asyncio's
default `min(32, cpu+4)` threads. All of these run on it (`routers/chat.py`, every
call passes `None`):

- :343 `start_turn` — a user prompt. **`start_turn` runs `compact_now` synchronously
  before spawning the worker** (session_runtime.py:893), and `compact_now` →
  `compact_history` → the summariser model call (session's own model, up to 160K chars
  input). A compacting turn start holds a pool thread for the whole summariser call.
- :368 `cancel` — the Stop button queues behind the same pool.
- :454 `start_turn` in assistant mode — autonomous sessions keep starting turns; each
  may compact at turn start the same way.
- :578 `run_terminal` — the code's own comment: "A terminal command can run for
  minutes; it must not hold the event loop, every other session's stream would freeze
  with it." It was moved off the event loop, but onto the *same shared pool*.
- :679 the manual Compact button — `compact_now(force=True)`, summariser call included.

So N sessions compacting at turn start (or compacting + terminals + assistant
continuations) hold N pool threads for tens of seconds each; on a small machine the
pool is 8-12 threads, and every other session's prompt, Stop, and terminal queues
behind them. Turns already streaming keep flowing (driver threads are plain threads) —
which reads exactly as "the whole chat is frozen but the running turn still moves".

Second, environment-dependent freeze path (**unconfirmed**): if the provider is a
local/single-slot server (the engine config mentions ollama_cloud), the summariser
call's 40K-token input occupies the model server itself, and every other session's
next model call queues behind it at the provider. Nothing in Tacit can show that
except slow streams.

## Where the loss actually comes from (mechanism, once triggering is fixed)

A fold keeps: a summary of the head (written by the session's own model, ≤160K chars
input) + a verbatim tail (≤200K tokens, ≥4 messages) + pruned-but-present stubs for
old tool output (400 chars kept per row). Everything else exists only in the archive
(since `3b24560`) — and the archive reader is **off by default**, so by default a fold
really is lossy for anything older than the tail. In 1.0.3 the same fold shape existed
but fired rarely (billed-only trigger) and kept a smaller tail; the difference the user
feels is frequency (1) plus the fact that folds now happen on sessions whose wire never
needed one.

## Suggested fixes (in order of confidence)

1. **Executor pool** — *shipped in 1.0.9* as dedicated turn and terminal pools
   (`TACIT_TURN_POOL`, `TACIT_TERM_POOL`), with the turn start run as a task so
   Stop reaches a run that is still compacting. Original text: install a dedicated executor for turn
   starts/terminals/compaction (`loop.set_default_executor` is not needed — pass an
   explicit executor to `run_in_executor` at chat.py:343/454/578/679), so one session's
   summariser call or terminal cannot starve another session's prompt or Stop.
2. **Re-scope `should_compact`** — *shipped in 1.0.9* as `STORE_COMPACT_FACTOR`
   (default 2.0): the billed measure keeps the plain fill target, the stored
   measure gets the headroom, so folds wait until the record is genuinely being
   stubbed. Original text: re-scope `should_compact` to the wire/billed measure, or give the stored measure
   its own ceiling (e.g. the elision budget) instead of the fill target — restores
   1.0.3 frequency while keeping the 640K-token fix for sessions that genuinely never
   report usage. Also consider a provider-vs-estimate reconciliation: `estimate_tokens`
   at 4 chars/token over-counts code-heavy transcripts, so `max(billed, stored)` can
   trigger on sessions the provider itself says are well under target.
3. **Re-expose a summariser model choice** — *still open* (the `COMPACT_MODEL` removal traded
   cost/latency for governance; a Settings surface would keep the governance and
   restore the cheap path).
4. **Turn the archive reader on by default** — *shipped in 1.0.9*: the default is
   on, `TACIT_ARCHIVE_TOOL` pins it, and the Settings > Tools switch still turns
   it off. Original text: turn the archive reader on by default (or surface it in the transcript UI) so a
   fold is never the last copy from the user's point of view, not just the model's.

## Evidence index

- `git diff aa2aa7e HEAD -- backend/config.py` — tail ceiling 8000→200000,
  COMPACT_MODEL removed, fallback_model added, archive switch added.
- `git show 3b24560` — archive-before-fold commit (agent.py, archive.py, config.py).
- v1.0.3 agent.py extracted to `%TEMP%\agent_103.py` via `git show aa2aa7e:backend/agent.py`;
  should_compact at :1102, mid-turn trigger at :2428.
- HEAD: config.py:100-148 (constants), :238-240 (fill target); agent.py:1012-1040
  (context_budget), :1139-1159 (should_compact), :1177-1188 (_tail_budget),
  :1454+ (compact_history), :2806-2831 (mid-turn fold); session_runtime.py:507-521
  (compacting_notice), :524+ (compact_now), :677-708 (driver fold handling),
  :931-1060 (start_turn); store.py:104-145 (write lock); app.js:924-929, 1041-1055
  (compacting handling); chat.py:335-350 (prompt path), :70 (state compacting flag).
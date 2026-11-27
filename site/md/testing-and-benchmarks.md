# Testing and benchmarks

The suite runs offline: no network, no real model, no container daemon and no benchmark runner, and
Tacit installs neither (`README.md:309-310`).

```sh
python -m unittest discover -s tests -t .
```

The test count is whatever the suite says it is. It is not maintained by hand
(`README.md:310`), so this page does not quote one.

## How the tests stay off your data

`tests/helpers.py` derives the list of state paths instead of writing it out: every uppercase
`Path` attribute of `backend/config.py` that sits under `config.HOME`
(`tests/helpers.py:6-27`). A hand-written list drifts, and it did: the sessions directory was missing
from it, so tests that believed they were isolated wrote fixture sessions into the real
`~/.tacit/sessions` and the interface then showed them as the person's own
(`tests/helpers.py:9-11`). Paths outside the home, the project root and the static directory, are
deliberately excluded, because tests must be able to read those (`:14-15`).

Redirecting a path works because the store re-reads it on every call rather than caching it at import
(`backend/store.py:9-17`, `backend/config.py:873-878`). A test that redirected `WORKSPACES_DIR` had to
actually redirect it.

## What the suite holds down

The README names seven of them (`README.md:310-313`). The list here is this page's own and does not match
the README's row for row: it carries `test_platform.py` where the README carries `test_engine.py`. The
ones that guard the architecture rather than a feature:

| File | What it pins |
|---|---|
| `test_turn_worker.py` | the worker against a real subprocess: spawn, event stream, cooperative cancel, tree kill, the thread escape hatch |
| `test_session_isolation.py` | one running turn per session and sid-scoped abort, over two real sockets |
| `test_isolation.py` | the container backend and the timeout guarantee, with a stubbed runtime |
| `test_platform.py` | tokens, plugins, MCP over a real subprocess, built-in memory, thinking levels |
| `test_claims.py` | each behaviour the README claims, including the agent loop end to end |
| `test_docs.py` | the documentation as a specification |
| `test_frontend_weight.py` | the size of what the browser has to load |

`tests/fake_mcp_server.py` is a real stdio server the MCP tests talk to, because a mock that agrees with
itself proves nothing about a protocol.

## The documentation is a specification

`tests/test_docs.py` checks prose the way it checks code, and it exists because three bugs are
recorded in its opening lines, the third of them found by a rewrite that shrank a file
(`tests/test_docs.py:1-41`).

- **Names that do not exist.** Every `TACIT_*` name in a doc must appear on a line that actually reads
  it out of the environment, in Python, PowerShell or sh, or is passed to a child process through a dict
  (`tests/test_docs.py:159-202`). A name that appears only in a comment is the same bug as a name that
  appears nowhere: it reads like a knob and does nothing.
- **Citations that point nowhere.** Every `path:line` is opened, the file must exist, the line must be
  inside it, and it must not be blank, at both ends of a range (`tests/test_docs.py:205-314`).
  Off-by-a-few is tolerated, because a citation may land one line off the code it describes; a blank
  line means the number was guessed.
- **Links and reachability.** Relative links must resolve to files
  (`tests/test_docs.py:317-330`), every page in `site/md/` must be linked from `site/md/index.md`, and the
  README must point at the hosted site (`tests/test_docs.py:336-365`).

The rule these enforce is the one stated in the module's last line: if you cannot point at the line, you
do not know the answer, and you do not write the sentence (`tests/test_docs.py:39-40`).

## The prose rules are executable too

`scripts/check_prose.py` holds the denylist and the suite runs it: no em dashes, no hollow adjectives, no
throat-clearing openers (`scripts/check_prose.py:1-6`). It checks `README.md`, `CONTRIBUTING.md` and
`site/md/` by default, and `--all` adds the installer text and the UI strings that a person actually reads
(`scripts/check_prose.py:70-74`, `:81-92`).

Patterns that need judgment are reported as warnings and never fail a run, because a rule that cries wolf
gets ignored inside a week (`scripts/check_prose.py:12-14`, `:62-68`). A genuine false positive goes into
`ALLOW` with the file, the line and the reason, so the exception is auditable rather than a widened regex
(`scripts/check_prose.py:16-18`, `:76-78`).

```sh
python scripts/check_prose.py            # the documentation set
python scripts/check_prose.py --all      # plus installer and UI strings
```

## Measuring by hand

Two scripts exist because a page felt slow and nobody could say why. They measure, they do not assert,
and they are run by hand.

`scripts/measure_load.py` answers two separate questions: how long each boot request takes on its own,
and whether a slow request delays an unrelated one. The second is the real one. A subprocess or a sleep
inside an `async def` blocks the event loop, which stops the WebSocket handshake and every other request
at the same time, and that is what "connecting..." for ages actually is
(`scripts/measure_load.py:3-9`). It needs a server already running and only ever issues GETs
(`:13-14`).

`scripts/measure_registry.py` counts bytes rather than messages for `/api/sessions/registry`
(`scripts/measure_registry.py:3`, route at `backend/routers/api.py:695`). Counting messages is not enough
to know whether the payload is small, because one message can carry a 48,000-character `read_file`
result, and bytes are the unit the wire and the parser actually pay
(`scripts/measure_registry.py:5-7`). It is read-only and takes an
optional home directory so a synthetic case can be measured
(`scripts/measure_registry.py:9-12`).

Both are kept in the repo on purpose: the first version of one of them lived in `%TEMP%` and was deleted
with it, which is why the registry's byte cost was argued about from memory afterwards
(`scripts/measure_registry.py:14-15`).

## Checking the documentation itself

Two more scripts run by hand, for the docs rather than the product.

`scripts/dump_citations.py` prints every cited source line next to the doc line that cites it.
`tests/test_docs.py` can prove a citation points at a real file, a line inside it and a non-blank line;
it cannot prove the line says what the sentence claims, and that gap is closed by reading
(`scripts/dump_citations.py:3-8`). It handles the three shapes a citation takes here, including the bare
`:N-M` that repeats the path before it (`:13-15`, `scripts/dump_citations.py:29-35`), and it exits 1 when
a citation names a missing file or a line outside it, so it doubles as a check.

`scripts/check_site_render.js` renders a sample of the docs through `site/assets/markdown.js` in a VM
sandbox, the way a page loads it, and asserts the output contains the structures the source has
(`scripts/check_site_render.js:4-9`). `scripts/build_site.py` already checks that every link resolves;
this checks the renderer. It needs Node, so it is never run by `unittest`, which stays offline and
installs nothing (`scripts/check_site_render.js:13-15`).

## The evidence system

A benchmark row is a claim, and a claim without its evidence is narration. Three pieces make every
evaluated run reproducible and inspectable: a run manifest, a scripted local provider for process-level
conformance, and an evaluation matrix that keeps tuning and held-out tasks apart.

### Run manifests

`backend/manifest.py` writes one plain JSON artifact per evaluated run under `~/.tacit/manifests`:
the build identity (VERSION, a source fingerprint of the loop and control plane, the git commit and
dirty flag where a checkout exists), hashes of the configuration and the standing prompt, the model,
provider, endpoint class and reasoning effort, every model call's normalized usage (nested sub-agent
calls charged to the parent), every tool call with its argument hash, duration, outcome and a bounded
result excerpt, file mutations with before/after content hashes, compactions, delegations, steering,
cancellation, and the terminal status (`backend/manifest.py:1-23`, `:139-275`).

Secrets never land in a manifest: arguments pass the audit ledger's masker, free text is additionally
redacted against the registry's actual key values, and configuration and prompts are recorded as
hashes, never content (`backend/manifest.py:106-131`, `:278-308`). Usage goes through
`metrics.normalize_usage`, the one parser the live meter and the headless runner already share, so a
manifest and a benchmark row cannot disagree about what a token is (`backend/metrics.py:42-69`).

Interactive recording is opt-in with `TACIT_MANIFEST=on` (`backend/config.py:560-568`); the turn
driver feeds the same event stream it already consumes, in both worker modes
(`backend/session_runtime.py:722-731`). A headless benchmark run always records, whatever the
interactive setting says, because a comparison without manifests is exactly the unverifiable claim
the manifest exists to prevent (`backend/headless.py:169-182`).

### The scripted provider

A worker subprocess cannot see a fake engine patched into the test process, so conformance tests that
need process semantics fake the model at the only seam that crosses the boundary:
`tests/fake_provider.py` serves deterministic OpenAI-shaped SSE from a local HTTP server, scripted per
request. The script covers the shapes the suite needs: plain answers, tool calls, delays, hangs,
truncated tool JSON, repeated identical calls, malformed bodies, HTTP 429/500 and connection resets
(`tests/fake_provider.py:1-33`). `tests/test_fake_provider.py` drives each shape through a real worker
and asserts the observable contract: a severed call never executes, identical calls hit the repeat
guard, a malformed body reaches a terminal event without hanging, and provider failures surface as
errors. All traffic is loopback; all state lives under a temporary `TACIT_HOME`.

### The evaluation matrix

`scripts/eval_matrix.py` runs the task catalog in `scripts/eval_tasks.json`: one task is one isolated
run (a fresh `TACIT_HOME`, a prepared workdir, one headless turn, one checker, one manifest), and the
raw artifacts stay beside the report for inspection (`scripts/eval_matrix.py:106-191`).

```sh
python scripts/eval_matrix.py --offline --out eval-artifacts     # pipeline conformance, no keys
python scripts/eval_matrix.py --live --model provider/id --out eval-artifacts
```

Two classification rules are the point of the runner:

- **Infrastructure failures are counted apart from task failures.** A run whose plumbing failed
  (error, timeout, crash) is an infrastructure failure; a clean run the checker rejects is a task
  failure. A harness is not graded on the model's mistakes (`scripts/eval_matrix.py:158-164`).
- **Tuning and held-out splits are never merged.** Tasks used to develop prompts and policies live in
  `tuning`; release claims come from `held_out`, and the report aggregates them separately
  (`scripts/eval_matrix.py:209-224`). The offline run proves the pipeline; capability measurement
  comes from live runs. Its scripted answers show the plumbing carries work to a verified artifact,
  nothing more.

The starter catalog spans feature, bugfix, refactor, documentation and research classes, including a
conformance task that must classify as an infrastructure failure and a documentation-only task whose
manifest must show no execution. Competitor arms (Claude Code, OpenCode) run the same tasks and
checkers through their own harnesses and contribute rows with a different `arm`; the report format
reserves the field, and the adapters plus the first expanded live baseline are the remaining Phase 0B
work.

## Where the published numbers come from

The benchmark section of the README is the evidence for the architecture, not the argument itself: one
model for every row, GLM 5.2 on the fp8 endpoint, run by OpenBench's own runner with its checker files
untouched, three tasks, three trials each, one build, both operating systems, all rows Tacit v1.0.0
(`README.md:191-192`).

How it holds up, which this page owns rather than the README:

- the runner and the checkers are the benchmark's own, byte for byte, and the agent never edits them;
- every row carries the checker's output verbatim, a byte count and a sha256 for every file it graded,
  and a per-call audit ledger of which tools ran and what came back, so a claimed solve is a transcript
  you can re-run;
- the profile rows are two tool bundles over the same cells, which makes the profile comparison an
  experiment rather than an anecdote.

Tokens are uncached input plus output with cache reads excluded, counted the way OpenBench counts them.
Schemelike is graded on Linux only so far, because its checker reads `os.O_NONBLOCK`, which Windows Python
does not provide.

The per-task cells behind those tables, tokens per cell, default profile first:

| Task | Linux, default | Linux, minimal | Windows, default | Windows, minimal |
|---|---|---|---|---|
| feal | 3/3 at 32,673 | 3/3 at 23,437 | 3/3 at 30,407 | 3/3 at 33,228 |
| llm | 3/3 at 235,379 | 3/3 at 226,896 | 3/3 at 182,646 | 2/3 at 157,192 |
| schemelike | 3/3 at 201,348 | 3/3 at 151,312 | not graded | not graded |

Every row carries the checker's output verbatim, a byte count and a sha256 for every file it graded, and a
per-call audit ledger of which tools ran and what came back, so a claimed solve stays a transcript somebody
else can re-run.

## Harness behaviour that changes a score

Three mechanisms exist because of what the cells showed, and they are documented here so a reader knows
which results were helped by them.

**The delivery guarantee** finishes a write the turn started and missed, makes it run, and spends the last
minutes of wall budget landing the artifact, so a failed cell is still a graded artifact
(`README.md:233-237`). It is on by default and `TACIT_DELIVER_GUARANTEE=0` turns it off
(`backend/config.py:546-554`). What opens the phase is evidence rather than the prompt: a `write_file` or
`edit_file` call the turn made and did not land. It used to open on a filename appearing in the task text,
and that guess is not sound, because a name in a prompt can be a session title, an input, a question or an
order, and only the sentence says which; a turn that wrote nothing and attempted nothing now gets the
nudge and the closing summary, and the nudge names no file. The salvage phase gets three rounds rather than
one, because writing the file is step one and the model then needs to run it and fix what the run shows; it
is bounded, because an unbounded "one more chance" is how a turn becomes two turns
(`backend/agent.py:896-2639`).

**The repeat guard** runs an identical call three times and answers from cache afterwards, and four
blocked repeats in a row ends the turn early (`backend/agent.py:865-874`, `:3409-3460`). It exists
because one cell spent 26 of its 29 shell calls on refused repeats and ended with nothing written
(`backend/agent.py:852-883`, `:865-883`).

**Identity, invalidation, and evidence tighten the same guard.** A call's identity is a full
canonical argument hash covering every character, so two long calls that differ only in their tail stay
distinct (`backend/agent.py:1176-1185`). A mutation clears the repeat cache while keeping the
mutating call's own entry, so an identical read after a write executes again instead of quoting the
bytes it replaced (`backend/agent.py:3256-3285`). What counts as verification is structural: a run
verifies only when it followed this turn's most recent successful write,
its exit comes from the structured result rather than the text, and it did not fail
(`backend/agent.py:2561-2584`), and the run and write are ordered by a logical sequence because a
fast turn's write and run can land on the same clock tick. A failing check earns the bounded verify
rounds; a pass ends the phase the moment it lands.

**Recovery is bounded.** The uncapped retry rescues at most four rounds per turn and skips the
deadline's last minute (`backend/agent.py:875-884`, the severed-call check in the main loop), and a
wall of consecutive failures warns once after six and stops the turn past twelve
(`backend/agent.py:868-874`). Salvage extraction refuses to pair fenced blocks with files when the
counts disagree (`backend/agent.py:1175-1179`), and an extracted write is recorded as a synthetic
tool call so the replayed history stays provider-valid (`backend/agent.py:2614-2675`): a bare tool
message after a plain assistant used to be rejected by the provider's own shape check.

**The uncapped retry** re-asks a round that our own output ceiling destroyed, so the model's thinking is
never paid for and then thrown away, and that mechanic is not part of the delivery switch
(`README.md:238-239`, `backend/config.py:525-529`).

Next: [contributing](CONTRIBUTING.md).

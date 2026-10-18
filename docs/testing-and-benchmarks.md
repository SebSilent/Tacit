# Testing and benchmarks

The suite runs offline: no network, no real model, no container daemon and no benchmark runner, and
Tacit installs neither (`README.md:1207-1209`).

```sh
python -m unittest discover -s tests -t .
```

The test count is whatever the suite says it is. It is not maintained by hand
(`README.md:1208-1209`), so this page does not quote one.

## How the tests stay off your data

`tests/helpers.py` derives the list of state paths instead of writing it out: every uppercase
`Path` attribute of `backend/config.py` that sits under `config.HOME`
(`tests/helpers.py:6-27`). A hand-written list drifts, and it did: the sessions directory was missing
from it, so tests that believed they were isolated wrote fixture sessions into the real
`~/.tacit/sessions` and the interface then showed them as the person's own
(`tests/helpers.py:9-11`). Paths outside the home, the project root and the static directory, are
deliberately excluded, because tests must be able to read those (`:14-15`).

Redirecting a path works because the store re-reads it on every call rather than caching it at import
(`backend/store.py:9-17`, `backend/config.py:596-600`). A test that redirected `WORKSPACES_DIR` had to
actually redirect it.

## What the suite holds down

The README names ten of them with what each pins (`README.md:1215-1226`). The ones that guard the
architecture rather than a feature:

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

`tests/test_docs.py` checks prose the way it checks code, and it exists because two bugs actually
happened while the docs were being written (`tests/test_docs.py:1-31`).

- **Names that do not exist.** Every `TACIT_*` name in a doc must appear on a line that actually reads
  it out of the environment, in Python, PowerShell or sh, or is passed to a child process through a dict
  (`tests/test_docs.py:100-145`). A name that appears only in a comment is the same bug as a name that
  appears nowhere: it reads like a knob and does nothing.
- **Citations that point nowhere.** Every `path:line` is opened, the file must exist, the line must be
  inside it, and it must not be blank (`tests/test_docs.py:146-216`). Off-by-a-few is tolerated, because
  a citation may land one line off the code it describes; a blank line means the number was guessed.
- **Links and reachability.** Relative links must resolve to files
  (`tests/test_docs.py:218-235`), every page in `docs/` must be linked from `docs/index.md`, and the
  README must point at `docs/` at all (`tests/test_docs.py:237-266`).

The rule these enforce is the one stated in the module's last line: if you cannot point at the line, you
do not know the answer, and you do not write the sentence (`tests/test_docs.py:29-30`).

## The prose rules are executable too

`scripts/check_prose.py` holds the denylist and the suite runs it: no em dashes, no hollow adjectives, no
throat-clearing openers (`scripts/check_prose.py:1-6`). It checks `README.md`, `CONTRIBUTING.md` and
`docs/` by default, and `--all` adds the installer text and the UI strings that a person actually reads
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
(`scripts/measure_registry.py:3`, route at `backend/routers/api.py:674`). Counting messages is not enough
to know whether the payload is small, because one message can carry a 48,000-character `read_file`
result, and bytes are the unit the wire and the parser actually pay (`:5-7`). It is read-only and takes an
optional home directory so a synthetic case can be measured
(`:9-12`).

Both are kept in the repo on purpose: the first version of one of them lived in `%TEMP%` and was deleted
with it, which is why the registry's byte cost was argued about from memory afterwards
(`scripts/measure_registry.py:14-15`).

## Where the published numbers come from

The benchmark section of the README is the evidence for the architecture, not the argument itself: one
model for every row, GLM 5.2 on the fp8 endpoint, run by OpenBench's own runner with its checker files
untouched, three tasks, three trials each, one build, both operating systems
(`README.md:1091-1094`).

How it holds up (`README.md:1155-1163`):

- the runner and the checkers are the benchmark's own, byte for byte, and the agent never edits them;
- every row carries the checker's output verbatim, a byte count and a sha256 for every file it graded,
  and a per-call audit ledger of which tools ran and what came back, so a claimed solve is a transcript
  you can re-run;
- the profile rows are two tool bundles over the same cells, which makes the profile comparison an
  experiment rather than an anecdote.

Tokens are uncached input plus output with cache reads excluded, counted the way OpenBench counts them
(`README.md:1093-1094`). Schemelike is graded on Linux only so far, because its checker reads
`os.O_NONBLOCK`, which Windows Python does not provide (`README.md:1113-1115`).

## Harness behaviour that changes a score

Three mechanisms exist because of what the cells showed, and they are documented here so a reader knows
which results were helped by them.

**The delivery guarantee** orders a missing file written, makes it run, and spends the last minutes of
wall budget landing the artifact, so a failed cell is still a graded artifact
(`README.md:58-63`). It is on by default and `TACIT_DELIVER_GUARANTEE=0` turns it off
(`backend/config.py:422-426`). The salvage phase gets three rounds rather than one, because writing the
file is step one and the model then needs to run it and fix what the run shows; it is bounded, because an
unbounded "one more chance" is how a turn becomes two turns (`backend/agent.py:772-776`).

**The repeat guard** runs an identical call three times and answers from cache afterwards, and four
blocked repeats in a row ends the turn early (`backend/agent.py:762-771`). It exists because one cell
spent 26 of its 29 shell calls on refused repeats and ended with nothing written
(`backend/agent.py:756-759`, `:767-770`).

**The uncapped retry** re-asks a round that our own output ceiling destroyed, so the model's thinking is
never paid for and then thrown away, and that mechanic is not part of the delivery switch
(`README.md:69-71` and `:44-47`, `backend/config.py:416-420`).

Next: [contributing](../CONTRIBUTING.md).

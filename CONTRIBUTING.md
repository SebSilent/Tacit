# Contributing

Tacit is one Python program serving plain web pages. There is no build step, no frontend toolchain,
and no database to install (`README.md:9-10`). That is the first thing to know before you change
anything: a contribution that adds a compile step, a package.json dependency for the app itself, or a
service to start breaks the property the product is built on.

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # Windows: .venv\Scripts\python.exe
python -m unittest discover -s tests -t .
```

The suite runs offline. It needs no network, no real model, no container daemon and no benchmark
runner, and Tacit installs neither (`README.md:379-380`).

## Layout

| Path | What lives there |
|---|---|
| `backend/` | the server, the agent loop, and every policy decision |
| `backend/routers/` | thin HTTP and WebSocket surfaces, one per domain (`backend/main.py:14`) |
| `backend/ai/` | provider transports and the prompt segments (`backend/ai/prompts.py:9-56`) |
| `backend/plugins/` | bundled plugins, one file each |
| `static/` | the frontend, served as-is from `config.STATIC_DIR` (`backend/config.py:27`, `backend/main.py:175`) |
| `tests/` | the suite, plus `tests/helpers.py` and `tests/fake_mcp_server.py` |
| `docs/` | the documentation set, indexed at `docs/index.md` |
| `scripts/` | `check_prose.py`, `bump_version.py`, `build_site.py`, `demo_shell.py` (the mock's shell, patched out of `static/index.html`), the two measurement scripts, and the two doc checkers |
| `install.sh`, `install.ps1`, `run.sh`, `run.bat`, `restart.ps1` | installers and launchers |
| `VERSION` | the release identity; `package.json` mirrors it (`package.json:4`) |

`package.json` is the Playwright driver's manifest for the `browser` tool and nothing else. It is
optional and only needed for browser automation (`package.json:6-10`). `scripts/bump_version.py` writes
`VERSION` and that mirror together so the two cannot drift (`package.json:4`).

## Two rules that are executable, not editorial

**The documentation is a specification.** `tests/test_docs.py` checks that every `TACIT_*` name in a doc
is actually read from the environment, that every `path:line` citation points at a real file and a real
non-blank line inside it, that relative links resolve, and that every page in `docs/` is reachable from
`docs/index.md` (`tests/test_docs.py:123-300`). If you add a setting, add the line that reads it. If you
cannot point at the line, you do not know the answer, and you do not write the sentence
(`tests/test_docs.py:39-40`).

**The prose has a denylist.** `scripts/check_prose.py` fails on em dashes, hollow adjectives and
throat-clearing openers in `README.md`, `CONTRIBUTING.md` and `docs/`, and `--all` extends it to the
installer text and the UI strings (`scripts/check_prose.py:29-60`, `:70-74`). Patterns that need
judgment are warnings and never fail the run (`:12-14`). If a match is genuinely wrong, add it to `ALLOW`
with the file, the line and the reason, so the exception is auditable instead of a widened regex
(`:16-18`, the tuple is `:76-78` and it is matched at `:102`).

## House rules for code

**Nothing optional is on by default.** The capability defaults live in one dict,
`backend/providers.py:22-38`, and the optional ones start off in it: sandbox `none` (`:24`), memory
`off` (`:26`), learning `learn-off` (`:30`). Two flags in the same dict are on, the analyzer
(`:31`) and the guidance blocks (`:34`), and each carries the reason beside it. The analyzer is
additionally gated on the learning mode, so with learning off its thread never starts
(`backend/analyzer.py:683-694`). A new capability's absence has to be free: no schema, no prompt
block, no worker.
The memory tools are removed from the registry when the mode is off, and `memory_recall` is removed
again when the mode cannot retrieve, because a tool that can only answer "not enabled" is a dead end with
a schema attached (`backend/agent.py:896-910`).

**Never silently downgrade.** A command either runs under the backend that was chosen or it does not run
(`backend/sandbox.py:9-10`). Anything the platform cannot enforce is reported as not enforced
(`backend/sandbox.py:521-536`). Windows has no isolation primitive in the base OS, and the correct fix for
that is the honest note plus the opt-in container backend, not a fake
(`backend/sandbox.py:25-27`, `backend/providers.py:82-90`).

**Blocking code does not run on the event loop.** A handler that shells out, walks a directory or calls a
gateway is a plain `def`, so Starlette runs it on a threadpool; the registry route carries the reason in a
comment (`backend/routers/api.py:677-679`). Slow work a turn start performs goes to the caller's executor
for the same reason (`backend/session_runtime.py:28-31`).

**A turn is a process, and stop is two-stage.** A thread cannot be killed in Python, so a turn runs in a
worker subprocess and cancellation is cooperative before it is forced
(`backend/turn_worker.py:3-21`). If you change that, `test_turn_worker.py` runs it against a real
subprocess and will tell you.

**State paths are re-read, not cached at import.** `backend/store.py:9-17` and
`backend/config.py:615-619` both exist because a cached path made a redirected test write into the real
`~/.tacit` (`tests/helpers.py:9-13`).

**The transcript renderer uses one delegated listener per surface.** Tool cards and message rows must not
attach their own (`tests/test_frontend_weight.py:1-20`). The test pins the structure rather than the
behaviour, and it explains why the rule was invisible for months: the one place that did follow it looked
like the whole file did (`:13-16`).

**Frontend weight is budgeted per file.** The baselines are measured raw and gzipped and the budgets are
that plus about four percent (`tests/test_frontend_weight.py:36`, re-measured at `:39-42`). If your change pushes a file over,
either cut something or update the baseline with the measurement, not with a guess.

## Reporting, not asserting

Two scripts measure instead of asserting, and they are the model for how to argue about performance here:
`scripts/measure_load.py` separates "this request is slow" from "this request blocks the loop"
(`scripts/measure_load.py:3-9`), and `scripts/measure_registry.py` counts bytes rather than messages
because one message can carry a 48,000-character result (`scripts/measure_registry.py:5-7`). Keep the
script in the repo: the first version of one of them lived in `%TEMP%` and was deleted with it, which is
why the number was argued from memory afterwards (`scripts/measure_registry.py:14-16`).

The same rule covers the tooling for these docs, because all of it was in a temp folder once:
`scripts/dump_citations.py` prints every cited source line next to the doc line that cites it, which is
the reading a citation check cannot do for you (`scripts/dump_citations.py:3-8`), and
`scripts/check_site_render.js` renders the docs through the same JavaScript the browser runs to confirm
the vendored renderer still produces tables and code blocks (`scripts/check_site_render.js:4-6`). It needs
Node, so it is run by hand and never from `unittest`, which stays offline and installs nothing
(`scripts/check_site_render.js:13-14`).

## Where to start

The benchmark table wants more arms on the same cells: any first-party or fp8 route for kimi and qwen,
DeepSeek and Qwen as the model, an Apple silicon lane, and a rerun on whatever hardware you have
(`README.md:281-283`). Tuning is welcome and is part of the point, since the profiles and prefs are the
tuning surface (`README.md:284-285`).

One concrete port is named in the README: schemelike is graded on Linux only because its checker reads
`os.O_NONBLOCK`, which Windows Python does not provide (`README.md:247-248`), and a Windows port of that
checker is on the list of cells the table wants next (`README.md:283-284`).

A pull request, an issue with the results file attached, or any row you can reproduce is a contribution
(`README.md:285-286`). Every row must carry the checker's output verbatim, the byte counts and sha256
digests of what it graded, and the per-call audit ledger, so that a claimed solve stays a transcript
somebody else can re-run (`docs/testing-and-benchmarks.md:152-154`).

Templates for both are in `.github/`.

## Reading

- `docs/index.md` for the documentation set and the three rules that hold across it
- `docs/architecture.md` before touching the server, the worker or the loop
- `docs/configuration.md` before adding a setting
- `docs/testing-and-benchmarks.md` for how the published numbers were produced

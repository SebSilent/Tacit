# Architecture

One Python process serves the app, and a second short-lived process runs each turn. There is no build
step and no database to install: the backend is FastAPI over plain modules, and the frontend is files
in `static/` mounted as-is (`backend/config.py:27`, `backend/main.py:175`).

## The layers

| Layer | Files | Responsibility |
|---|---|---|
| App | `backend/main.py` | lifespan, middleware, static mount, one route per page |
| Routers | `backend/routers/` | `api`, `chat`, `mcp`, `memory`, `plugins`, `capabilities`, `profiles`, `hosting`, `vcs` (`backend/main.py:14`) |
| Session runtime | `backend/session_runtime.py` | start, track and finish work, keyed by session id |
| Turn worker | `backend/turn_worker.py` | one subprocess per turn |
| Agent loop | `backend/agent.py` | tool registry, dispatch, compaction, the repeat guard |
| Providers | `backend/providers.py`, `backend/ai/` | what model runs, what capabilities it declares, how it is called |
| Policy | `backend/sandbox.py`, `backend/mcp_registry.py`, `backend/memory_modes.py` | what a command may touch, what may be injected |
| State | `backend/store.py`, `backend/config.py` | sessions on disk, paths and defaults |

The router list is complete and short because a router is a thin HTTP surface. The decision each one
exposes lives in the module it calls.

## A session id is the anchor

Everything a session runs is started, tracked and finished in
`backend/session_runtime.py:3-11`, keyed by sid, never by a socket. A WebSocket is a viewer and a
control channel: it subscribes to the session's stream and forwards a few commands, and it owns
nothing.

That choice is what makes a reload harmless. A browser that closes, reloads or switches sessions
changes nothing about a running turn: the worker keeps running, the bus keeps buffering, the
transcript is saved by the turn's own epilogue, and any window that opens the session subscribes and
is caught up from the buffer (`backend/session_runtime.py:6-11`).

A socket that arrives with an id the store does not know is closed with code 4404 rather than served
an empty session (`backend/routers/chat.py:206-227`). Only the New session action may create one,
and it says so with `?create=1`.

Two workers are normal for one session, and they are independent: the turn worker and the assistant
worker. An assistant reply may stream while the agent's turn runs, which is why the assistant is
tracked in the session runtime rather than in `session_state`, whose single busy row would make the
two mutually exclusive (`backend/session_runtime.py:13-26`). Sub-agents and research runs are extra
workers on top, with no limit, because they are read-only and independent
(`backend/session_runtime.py:20-21`).

## Why a turn is a process

A turn is a unit of work a person must be able to kill, and a thread cannot be killed in Python under
any circumstance (`backend/turn_worker.py:3-7`). A turn blocked in a model read, a stuck shell
command or a runaway regex holds nothing of the server hostage, because it is not running in the
server.

The lifecycle is three steps (`backend/turn_worker.py:9-21`):

1. The session's handler spawns `python -m backend.turn_worker` with the turn's parameters on argv,
   and nothing secret anywhere in the environment it did not already have.
2. The worker runs the same `agent.run_turn` the in-process path ran, and streams events to the
   parent as JSON lines on stdout.
3. Stop is two-stage. The parent first sends `{"cancel": true}` on stdin, so the worker stops between
   events, closes its model stream, kills its own shell trees and exits with the partial transcript
   preserved. If it has not exited within the grace period the parent kills the process tree outright,
   `taskkill /T /F` on Windows, `killpg` on POSIX (`backend/turn_worker.py:299-304`).

`TACIT_TURN_WORKER=thread` restores the in-process path. It exists for the tests that fake the model
engine by patching it into this process, which a worker subprocess cannot see
(`backend/turn_worker.py:23-25`, `backend/config.py:401`).

## What the server does at startup

The lifespan block runs in order (`backend/main.py:17-96`):

1. create the state directory and load `~/.tacit/.env` (`backend/main.py:19-20`)
2. write the pid file (`backend/main.py:22`)
3. sweep orphans before anything else starts, then register this process so `/api/processes` shows the
   server too (`backend/main.py:25-38`)
4. sync the learning worker, which is never on the request path (`backend/main.py:39-46`)
5. start the autoupdater background task (`backend/main.py:47-52`)

On shutdown every worker this server spawned gets the two-stage stop, and each driver's epilogue is
given a moment to land, so a restart saves the partial transcript instead of orphaning the children
(`backend/main.py:54-61`). Then the in-process plan threads are flagged aborted so a snapshot cannot
claim a plan is still exploring (`:62-70`), the autoupdater task is cancelled (`:71-77`), the analyzer
thread and every MCP server are stopped (`:78-85`), the pid file is removed only if it still holds this
pid (`:86-90`), and this process drops its own registry row so the next boot's sweep does not meet a
dead pid wearing this boot's marker (`:91-96`).

The orphan sweep identifies children by a command-line marker and by the `TACIT_ROLE` /
`TACIT_SESSION_ID` pair in their environment, never by the executable name
(`backend/proctools.py:8-12`, `:295-302`). A dead pid is dropped from the registry; a live pid whose
command line still proves it is Tacit's is killed as an orphan from a previous crash.

## Blocking code does not run on the event loop

Many read handlers are plain `def` rather than `async def`, which is how Starlette runs them on a
threadpool. The reason is specific: a handler that shells out to git, walks a directory or calls a
gateway blocks for as long as that takes, and a blocked event loop freezes every other session's
stream.

The same rule is written into the session runtime for the work a start performs, compaction's
summariser call and the spawn, which run in the caller's executor thread and never on the loop
(`backend/session_runtime.py:28-31`).

Responses above 1024 bytes are gzipped (`backend/main.py:147`), and the static mount serves the
frontend with no build step (`backend/main.py:175`).

## The agent loop

`backend/agent.py` holds the registry, the dispatcher and the guards described in
[tools](tools.md). Three things happen around every turn rather than inside the model call:

- **Compaction.** `should_compact` decides whether the transcript is over budget, answering
  to the larger of the last billed prompt and the stored record itself (`backend/agent.py:1101`,
  `stored_tokens` at `backend/agent.py:1092`): a mid-turn compaction shrinks the prompt on
  purpose, so the prompt alone cannot be the measure. `compact_history` writes the summary
  between turns (`backend/agent.py:1322`) and `compact_messages` does it inside a growing turn
  (`backend/agent.py:1421`), which is what `TACIT_COMPACT_MID_TURN` controls
  (`backend/config.py:140-143`). A turn runs in its own process while its parent writes the
  transcript, so a note written mid-turn has to cross that boundary or nothing was reclaimed:
  `apply_stored_summary` folds it into the record when the turn ends (`backend/agent.py:1348`),
  keeping a tail at least as wide as the one the worker kept. `compact_now` owns the
  accounting for both the turn start and the forced command
  (`backend/session_runtime.py:524`), because a hand compaction that moves neither the meter
  nor the ledger leaves the bar disagreeing with the record it describes.
- **The repeat guard and the delivery guarantee**, described in [tools](tools.md) and
  [testing and benchmarks](testing-and-benchmarks.md).
- **Project context.** `AGENTS.md` and friends are indexed by name and fetched on demand, and
  `backend/project_context.py:50` chooses between `off`, `index` and `inline`, with a 1200-token
  inline budget (`backend/project_context.py:52`).

## Reasoning levels

Levels are passed through under the provider's own names where it has them, and the table that maps
them is `REASONING_EFFORT` (`backend/config.py:876-879`). One translation belongs to Tacit and it is
the one that was wrong: `off` must send a value that actually turns thinking off, because sending
nothing leaves thinking on. That was measured on a live endpoint, where every named effort produced
1.5k to 1.8k characters of reasoning while `none` and `off` produced zero
(`backend/config.py:787-792`).

Next: [cost and profiles](cost-and-profiles.md).

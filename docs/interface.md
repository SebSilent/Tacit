# The interface

Three regions, and each one has a different relationship to the agent.

```
┌──────────────┬─────────────────────────────────┬──────────────────┐
│  Sessions    │  Workspace · Mode · Model       │   Assistant      │
│  (search)    ├─────────────────────────────────┤  (goal · run)    │
│              │  transcript                     │  findings /      │
│  · session 1 │  · tool cards                   │  critique        │
│  · session 2 │  · the task strip               │                  │
│  + New       ├─────────────────────────────────┤                  │
│              │  composer          [Send]       │                  │
└──────────────┴─────────────────────────────────┴──────────────────┘
```

**Sessions** (left). One session per project folder, and one running turn per session. The state dot tells you which are working; stopping one leaves the others running. Each session has its own WebSocket (`backend/routers/chat.py:200`), so a turn is never cut off because you opened another session. A socket that arrives with an id the store does not know is closed with code 4404 rather than served an empty session: only the New session action may create one, and it says so with `?create=1`.

The folder you opened a session in is fixed for the life of that session. Switching sessions switches the workspace chip, the mode, the model, the context meter and the composer draft with it.

**Main agent** (middle). The transcript holds tool cards with their real output, a task strip while a plan is running, and the composer at the bottom. Mode is `ask`, `plan`, `task` or `agent`. The task strip is a read-only view of the Task List plugin's board and appears only when that plugin is enabled and a plan is running.

**Assistant** (right). A second agent that reads the session and is invisible to the first one. Whatever it produces is material you act on: a critique, a plan, a summary, a draft. It has its own goal field, and that goal is the Assistant's alone. It never becomes the main agent's task, and it is not part of the agent's context.

## Snapshots

The clock button beside the workspace picker opens the timeline: compare a checkpoint against the working tree, or restore it.

Before the first write, edit or shell command of a turn, the server checkpoints the whole workspace. One capture per turn, not per step, so a turn that edits twelve files is one thing to undo. The agent has no snapshot tools at all: it cannot save its own work or rewind it. Only you can.

Two limits decide what gets captured, and both are reported rather than skipped silently: files over `TACIT_SNAPSHOT_MAX_FILE` (2 MB) and a workspace over `TACIT_SNAPSHOT_MAX_FILES` (3,000 files).

## Restart, and what it costs

**Settings > General > Restart server** stops the process and brings it back on the same port. Any turn that was running is cut off, and the transcript records the reason. The action is audited as `server_restart`.

## Settings

Four tabs are always there: **General**, **Providers**, **Skills & Tools**, **Appearance**. The other five (Capabilities, Learning, MCP, Memory, Plugins) sit behind an `advanced` checkbox so a first run does not have to read about systems it does not use. The choice is yours, not the agent's: the checkbox is remembered in the browser (`static/platform.js:51-72`).

General also holds the profile selector, the permission list, the server section and the updater. [The settings map](settings.md) goes tab by tab.

## Sounds

Four chimes, two switches, both switches server settings rather than per-device ones because they describe the machine: a person who silenced it wants every device pointed at this install to stay silent. `turnSound` is **on** by default and covers the two that announce finished work, `done` and `error`. `alertSound` is **off** by default and covers the two that fire *during* work: `warn`, the budget warning, and `tick`, one per cycle of a loop that can run for hours unattended. Sound that repeats by itself is the kind a person reaches for the mute on, so it starts silent and is opted into. The **General** tab's Interface section auditions all four in order, past both switches, on purpose: you clicked to hear them. The switches are `turnSound` and `alertSound` in `prefs.json`, read at `backend/config.py:413` and `:426`, and there is no environment variable for either.

## The dashboard

Cost and context live in Settings, not in a bar above the composer. The meter is a context meter, and it says so: the figure it shows is the window the model reports, not a tokeniser's guess. The dashboard shows what the turn cost, what the standing prompt costs, memory's budget and what was avoided, and the cache hit rate.

Compaction is visible while it happens. The meter reads `compacting…` and the composer is disabled for
the session doing it, so a prompt typed during a fold cannot land in the transcript the fold is
rewriting (`static/app.js:1224-1231`, refused at `:1128`). The state arrives on the same socket as
everything else, from `compacting_notice` in `backend/session_runtime.py:507`, and it is cleared when
the fold is written, not when the model answers: a fold that reclaimed nothing clears too. The figure
the meter shows afterwards is the surviving record, floored at what is actually stored, because the
number that matters is what the next request will pay.

Next: [what every switch does](settings.md).

# Tools

The registry is one list: `backend/agent.py:757-842`. It holds 23 tools, and each entry is a name, a
description the model reads, and a JSON schema. The handler that runs is looked up by name in `EXECS`
(`backend/agent.py:669-692`).

Nothing else in the product adds a tool to that list silently. Plugin tools and activated MCP schemas
are appended at the end of the same function (`backend/agent.py:1016-1032`), and both are off until
you turn them on.

## The 23

| Tool | What it does | Defined at |
|---|---|---|
| `list_files` | list files and folders, relative to the working project | `backend/agent.py:771` |
| `read_file` | read a file as numbered lines, with a range footer | `backend/agent.py:773` |
| `write_file` | create or overwrite a file | `backend/agent.py:776` |
| `edit_file` | replace a snippet that appears exactly once | `backend/agent.py:777` |
| `glob_files` | find files by name pattern | `backend/agent.py:779` |
| `grep_files` | search file contents with a regular expression | `backend/agent.py:780` |
| `run_shell` | run a command in the project directory | `backend/agent.py:783` |
| `skill` | read one skill's full instructions | `backend/agent.py:786` |
| `bg_start` | start a long-running command in the background | `backend/agent.py:788` |
| `bg_output` | read a background job's output | `backend/agent.py:790` |
| `bg_stop` | stop a background job | `backend/agent.py:792` |
| `fetch` | fetch a URL as readable text | `backend/agent.py:793` |
| `browser` | drive a real browser | `backend/agent.py:795` |
| `research` | a bounded multi-source check | `backend/agent.py:805` |
| `evidence` | record or read a citable fact | `backend/agent.py:811` |
| `task` | delegate to a sub-agent with its own context | `backend/agent.py:814` |
| `mcp_list_servers` | list MCP servers and what their tools would cost | `backend/agent.py:818` |
| `mcp_search_tools` | search the MCP catalogue by intent | `backend/agent.py:820` |
| `mcp_activate_tools` | inject MCP schemas for a few turns | `backend/agent.py:823` |
| `mcp_call` | call one MCP tool without paying its schema | `backend/agent.py:826` |
| `memory_recall` | search durable memory | `backend/agent.py:836` |
| `memory_add` | propose a durable memory, pending review | `backend/agent.py:839` |
| `session_archive` | read the frozen archive of rows a compaction folded away | `backend/agent.py:850` |

Two different numbers describe how many tools you are paying for, and the difference is the gating
above. The profile table publishes 20 for a default install, which is the registry minus the two
memory schemas and the archive reader, each priced in its own bucket instead
(`backend/profiles.py:124-141`, `:201-205`). A fresh install with no MCP server configured actually
sees 16, because the four helpers are also removed at request time (`backend/agent.py:994-1032`). The
published figure counts what the profile owns; the runtime figure counts what the model is offered.

## Five rules decide whether a tool is offered

**1. A disabled tool is gone.** `disabledTools` in `~/.tacit/prefs.json` removes entries by name
(`backend/agent.py:973-977`). The same list is what **Skills & Tools** edits.

**2. A read-only caller loses the tools that can change things.** `READONLY_BLOCKED` holds
`write_file`, `edit_file`, `run_shell`, `bg_start` and `bg_stop`, and adds `task`, `mcp_call`,
`mcp_activate_tools` and `memory_add` (`backend/agent.py:694-702`). `evidence` keeps its read actions
but refuses `add`; `browser` keeps `open`, `text` and `links`, but refuses the mutating `click`,
`type`, `screenshot` and `close` actions (`backend/agent.py:707-733`). Hiding a schema does not stop a
call, so `readonly_guard` runs again at dispatch (`backend/agent.py:810-2026`). Every caller also
gets an `allowed_tools` set built from the exact schemas offered for that turn, and dispatch refuses
any other name, including every tool in chat (`backend/agent.py:2502-2521`, `:2022-2050`).

**3. The MCP helpers appear only when a server is configured.** With nothing in `mcp.json` the four
helpers are four dead ends that cost tokens and invite calls that can only fail
(`backend/agent.py:994-1032`). Presence is the gate, not connectivity: a configured but stopped server
still gets them, because listing and searching are how the model learns what it could start.

**4. Memory tools follow the memory mode.** Both are removed when the mode is off
(`backend/agent.py:999-1003`). `memory_recall` is removed again in `explicit` mode, because that mode
injects what you approved and retrieves nothing, so the tool could only ever answer "not enabled"
(`backend/agent.py:994-1032-1001`, `backend/memory_modes.py:26-29`).

**5. The archive reader follows its own switch.** `session_archive` is removed when the archive
switch is off (`backend/agent.py:1000-1004`), and the same gate is enforced again at dispatch, because
a model that remembers the name from an earlier turn can still emit it and a refusal it can read is
the honest answer (`backend/agent.py:2019-2023`). The switch lives in Settings > Tools and its
default is on: a fold the model cannot read back is the loss the person feels, and the reader is
what makes a fold a reorganisation instead of a deletion (`backend/config.py:560-577`). It shipped
off and paid nothing for it, which was the problem: the fold still happened. `TACIT_ARCHIVE_TOOL=0`
pins it off for an install that wants neither.

## What the tools are told about themselves

The description is the only documentation the model gets, so three of them carry facts that used to
live only in the code.

`run_shell` states the shell and the operating system it is running on, read from config at the
moment the registry is built (`backend/agent.py:787-796`). `research` quotes
its own budget, `RESEARCH_ASPECTS`, `RESEARCH_STEPS` and `RESEARCH_TIMEOUT`, taken from
`backend/config.py:266`, `:276` and `:280` rather than written into the sentence, so the description
cannot drift away from the caps (`backend/agent.py:786-791`). `memory_add` says plainly that a
proposal lands pending review and is injected nowhere until you approve it
(`backend/agent.py:820-826`).

## Results have to say where they stopped

Two reporting rules exist because a session showed what happens without them
(`backend/agent.py:172-177`, `:245-248`).

`read_file` ends with the range it actually delivered and the offset to continue from, so a partial
read never reads as a whole file. `grep_files` searches exactly what it is pointed at: a file path
searches that file, a directory path searches that tree, and a path that does not exist is an error
rather than a fallback to somewhere else.

## Background jobs outlive their turn

`bg_start` deliberately releases the command from the per-turn worker's process containment
(`backend/extras.py:237-278`). Its stdin is disconnected and its combined stdout/stderr goes to
`~/.tacit/jobs/<id>.log`; the adjacent JSON row records the owning session, root pid, process-group
id on POSIX, the process's birth identity, command, working directory and start time
(`:264-269`). An ordinary turn or worker exit therefore leaves the job running. `bg_output` reads
that log from the end rather than loading it all, so a later worker using the same `TACIT_HOME` can
read a job without owning its original process handle (`backend/extras.py:108-126`, `:321-340`).

`bg_stop` kills the whole tree (`taskkill /T /F` on Windows, the process group on POSIX), waits for it
to die, and returns an error rather than claiming success if it is still running
(`backend/extras.py:129-234`, `:349-354`). A stop from a process that does not own the original
handle first proves the persisted pid is still the process the record registered, by its birth
identity; a reused pid or a record with no identity is refused, not killed
(`backend/extras.py:168-196`). The Stop control stops every persisted job belonging to
that session, without touching another session's jobs (`backend/session_runtime.py:1248-1292`). A
normal server exit stops all remaining jobs (`backend/extras.py:351-375`). Stopped and exited rows
and logs remain available for later `bg_output` calls; stopping a job which has already exited does
not claim to have killed it. Finished records and logs are kept for a week and capped at 200, pruned
on the next start; running jobs are never pruned (`backend/extras.py:281-318`).

## Which tools can change your files

`MUTATING` is `write_file`, `edit_file` and `run_shell` (`backend/agent.py:840-844`). The first use
of any of them in a turn takes a snapshot of the workspace before it runs, and the result says so
(`backend/agent.py:937-963`, `:3107-3112`). `run_shell` is in that set because a command can rewrite
the tree, and the sandbox layer reports every file a command changed regardless of which tool ran it.

Snapshots are not agent tools. The agent cannot save its own work or rewind it; the undo belongs to
the person using Tacit (`backend/agent.py:669-692`).

## The repeat guard

The same call with the same arguments runs three times in a turn, and after that it is answered from
cache instead (`backend/agent.py:859-888`). Four blocked repeats in a row ends the turn early and
spends the last round on the deliverable rather than counting down to the step cap
(`backend/agent.py:3362-3372`).

`write_file` and `edit_file` are exempt from caching, because a repeated write may be a recovery and
silently skipping a mutation the model asked for would make the harness lie about what it did to the
project (`backend/agent.py:857-883`). `run_shell` is deliberately not exempt: the guard exists because
one benchmark cell spent 26 of its 29 shell calls on refused repeats.

## Tools that exist but are not in the registry

The learning analyzer and the migration importer have no tools at all. They are driven from the
interface, so they cost nothing in the schema budget and cannot be invoked by a model
(`backend/analyzer.py:14-16`). Snapshot and restore are the same story: they are server actions, not
agent actions.

Next: [architecture](architecture.md).

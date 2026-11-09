# Tools

The registry is one list: `backend/agent.py:714`. It holds 23 tools, and each entry is a name, a
description the model reads, and a JSON schema. The handler that runs is looked up by name in `EXECS`
(`backend/agent.py:635`-652`).

Nothing else in the product adds a tool to that list silently. Plugin tools and activated MCP schemas
are appended at the end of the same function (`backend/agent.py:973`, `:964`), and both are off until
you turn them on.

## The 23

| Tool | What it does | Defined at |
|---|---|---|
| `list_files` | list files and folders, relative to the working project | `backend/agent.py:715` |
| `read_file` | read a file as numbered lines, with a range footer | `backend/agent.py:717`-713-700` |
| `write_file` | create or overwrite a file | `backend/agent.py:720` |
| `edit_file` | replace a snippet that appears exactly once | `backend/agent.py:721`-716-703` |
| `glob_files` | find files by name pattern | `backend/agent.py:723` |
| `grep_files` | search file contents with a regular expression | `backend/agent.py:724`-720-707` |
| `run_shell` | run a command in the project directory | `backend/agent.py:727`-723-710` |
| `skill` | read one skill's full instructions | `backend/agent.py:730`-725-712` |
| `bg_start` | start a long-running command in the background | `backend/agent.py:732`-727-714` |
| `bg_output` | read a background job's output | `backend/agent.py:734`-729-716` |
| `bg_stop` | stop a background job | `backend/agent.py:736` |
| `fetch` | fetch a URL as readable text | `backend/agent.py:737`-732-719` |
| `browser` | drive a real browser | `backend/agent.py:739`-737-724` |
| `research` | a bounded multi-source check | `backend/agent.py:749`-748-735` |
| `evidence` | record or read a citable fact | `backend/agent.py:755`-751-738` |
| `task` | delegate to a sub-agent with its own context | `backend/agent.py:758`-755-742` |
| `mcp_list_servers` | list MCP servers and what their tools would cost | `backend/agent.py:762`-757-744` |
| `mcp_search_tools` | search the MCP catalogue by intent | `backend/agent.py:764`-760-747` |
| `mcp_activate_tools` | inject MCP schemas for a few turns | `backend/agent.py:767`-763-750` |
| `mcp_call` | call one MCP tool without paying its schema | `backend/agent.py:770`-768-755` |
| `memory_recall` | search durable memory | `backend/agent.py:780`-776-763` |
| `memory_add` | propose a durable memory, pending review | `backend/agent.py:783`-782-769` |
| `session_archive` | read the frozen archive of rows a compaction folded away | `backend/agent.py:794`-792-779` |

Two different numbers describe how many tools you are paying for, and the difference is the gating
above. The profile table publishes 20 for a default install, which is the registry minus the two
memory schemas and the archive reader, each priced in its own bucket instead
(`backend/profiles.py:124-141`, `:201-205`). A fresh install with no MCP server configured actually
sees 16, because the four helpers are also removed at request time (`backend/agent.py:929`-933`). The
published figure counts what the profile owns; the runtime figure counts what the model is offered.

## Five rules decide whether a tool is offered

**1. A disabled tool is gone.** `disabledTools` in `~/.tacit/prefs.json` removes entries by name
(`backend/agent.py:921`-917`). The same list is what **Skills & Tools** edits.

**2. A read-only caller loses the tools that can change things.** `READONLY_BLOCKED` holds
`write_file`, `edit_file`, `run_shell`, `bg_start` and `bg_stop` (`backend/agent.py:660`), and adds
`task`, `mcp_call`, `mcp_activate_tools` and `memory_add` for a reason worth keeping: filtering the
schema by name is not the whole job, because `mcp_call` can be handed `confirm=true` by the model
itself with nobody to ask (`backend/agent.py:662`-662`). `evidence` is not dropped, only its `add`
action is refused, so a read-only panel can still read what was recorded
(`backend/agent.py:670`-667`). Hiding a schema does not stop a call, so the same rule is enforced
again at dispatch time by `readonly_guard` (`backend/agent.py:676`-691`).

**3. The MCP helpers appear only when a server is configured.** With nothing in `mcp.json` the four
helpers are four dead ends that cost tokens and invite calls that can only fail
(`backend/agent.py:929`-933`). Presence is the gate, not connectivity: a configured but stopped server
still gets them, because listing and searching are how the model learns what it could start.

**4. Memory tools follow the memory mode.** Both are removed when the mode is off
(`backend/agent.py:951`-951`). `memory_recall` is removed again in `explicit` mode, because that mode
injects what you approved and retrieves nothing, so the tool could only ever answer "not enabled"
(`backend/agent.py:946`-951`, `backend/memory_modes.py:26-29`).

**5. The archive reader follows its own switch.** `session_archive` is removed when the archive
switch is off (`backend/agent.py:959`-960`), and the same gate is enforced again at dispatch, because
a model that remembers the name from an earlier turn can still emit it and a refusal it can read is
the honest answer (`backend/agent.py:1828`-1828`). The switch lives in Settings > Tools and its
default is on — a fold the model cannot read back is the loss the person feels, and the reader is
what makes a fold a reorganisation instead of a deletion (`backend/config.py:511-528`). It shipped
off and paid nothing for it, which was the problem: the fold still happened. `TACIT_ARCHIVE_TOOL=0`
pins it off for an install that wants neither.

## What the tools are told about themselves

The description is the only documentation the model gets, so three of them carry facts that used to
live only in the code.

`run_shell` states the shell and the operating system it is running on, read from config at the
moment the registry is built (`backend/agent.py:726`-722`). `research` quotes
its own budget, `RESEARCH_ASPECTS`, `RESEARCH_STEPS` and `RESEARCH_TIMEOUT`, taken from
`backend/config.py:199`, `:209` and `:213` rather than written into the sentence, so the description
cannot drift away from the caps (`backend/agent.py:748`-747`). `memory_add` says plainly that a
proposal lands pending review and is injected nowhere until you approve it
(`backend/agent.py:782`-777`).

## Results have to say where they stopped

Two reporting rules exist because a session showed what happens without them
(`backend/agent.py:171-176`, `:244-247`).

`read_file` ends with the range it actually delivered and the offset to continue from, so a partial
read never reads as a whole file. `grep_files` searches exactly what it is pointed at: a file path
searches that file, a directory path searches that tree, and a path that does not exist is an error
rather than a fallback to somewhere else.

## Which tools can change your files

`MUTATING` is `write_file`, `edit_file` and `run_shell` (`backend/agent.py:803`-801`). The first use
of any of them in a turn takes a snapshot of the workspace before it runs, and the result says so
(`backend/agent.py:889`-910`). `run_shell` is in that set because a command can rewrite the tree, and
the sandbox layer reports every file a command changed regardless of which tool ran it.

Snapshots are not agent tools. The agent cannot save its own work or rewind it; the undo belongs to
the person using Tacit (`backend/agent.py:662`-662`).

## The repeat guard

The same call with the same arguments runs three times in a turn, and after that it is answered from
cache instead (`backend/agent.py:822`-820`). Four blocked repeats in a row ends the turn early and
spends the last round on the deliverable rather than counting down to the step cap
(`backend/agent.py:2877`-2879`).

`write_file` and `edit_file` are exempt from caching, because a repeated write may be a recovery and
silently skipping a mutation the model asked for would make the harness lie about what it did to the
project (`backend/agent.py:814`-814`). `run_shell` is deliberately not exempt: the guard exists because
one benchmark cell spent 26 of its 29 shell calls on refused repeats.

## Tools that exist but are not in the registry

The learning analyzer and the migration importer have no tools at all. They are driven from the
interface, so they cost nothing in the schema budget and cannot be invoked by a model
(`backend/analyzer.py:14-16`). Snapshot and restore are the same story: they are server actions, not
agent actions.

Next: [architecture](architecture.md).

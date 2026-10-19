# Cost and profiles

Tacit's cost per turn is made of parts you choose, and every one of them is measured before you
choose it. A profile is those choices captured under a name, so moving between "as small as possible"
and "everything on" is one action with a known price (`backend/profiles.py:3-9`).

## What the fixed cost is made of

The standing prompt is assembled in one function (`backend/ai/prompts.py:148-176`):

| Part | Source | When it appears |
|---|---|---|
| Base instructions | `backend/ai/prompts.py:9-25` | always, or `SUBAGENT` (`:49-56`) for a delegated run |
| The version-control rule | `backend/ai/prompts.py:3-7`, chosen at `:153` | always, and it tracks the setting so the prompt never claims something the shell guard would refuse |
| Project scope | `backend/ai/prompts.py:27-31`, added at `:155-156` | when the session has a usable project (`backend/config.py:680`) |
| Scratch-workspace notice | `backend/ai/prompts.py:33-38`, added at `:157-162` | when there is no usable project and a session id, so the model is told where it is working |
| Read-only or chat notice | `backend/ai/prompts.py:40-44`, added at `:167-170` | per mode |
| Skill index | added at `backend/ai/prompts.py:171-175` | only when skills exist; bodies are fetched on demand by the `skill` tool |

On top of that sits the tool schemas, then plugin tools, then memory, then any eagerly injected MCP
schemas. `cost_of` returns those five numbers separately rather than one total, so a reader can see
which part of the bill moved (`backend/profiles.py:199-203`).

The published figures are in the README: 974 characters of standing instructions, about 768 tokens on
Minimal and 1,914 on Default (`README.md:19-21`). A test reads the table back out of the README and
re-measures it against `cost_of`, because 1,889 stayed published for a while after the schemas grew
past it and nothing noticed.

## How a token figure is produced

There is no assumed tokenizer. `tiktoken` is used when it happens to be installed and a character
heuristic is used otherwise, at 4 characters per token rounded up, because real BPE tokenizers average
about 4 on prose and fewer on code and JSON (`backend/tokens.py:7-19`).

Two rules follow from that:

- **An estimate is never presented as exact.** `exact()` reports which mode the process is in
  (`backend/tokens.py:38-40`), `label()` prefixes an estimate with `~` (`:110-112`), and `estimate_of()`
  appends the word `estimate` for logs (`:115-119`).
- **A schema is priced as it is sent.** `estimate_tool_schema_tokens` serialises the whole tool
  definition, which is what the request `tools` array actually carries (`backend/tokens.py:62-67`), and
  `estimate_tools_tokens` sums it (`:70-71`). A message is content plus tool-call name and arguments
  plus a 4-token envelope (`:74-89`).

A plugin's cost is measured from its real schemas rather than from the number it declares
(`backend/profiles.py:119-121`).

## The six built-in profiles

`BUILTIN` is the whole list (`backend/profiles.py:21-81`), and `default` is the active one until you
change it (`:83`).

| Profile | Tools | Sandbox | Memory | Learning |
|---|---|---|---|---|
| `minimal` | the seven core tools only | none | off | off |
| `default` | all, nothing extra | none | off | off |
| `safe` | all | tacit-micro | explicit | off |
| `power-isolation` | all | tacit-micro | explicit | off |
| `power-memory` | all | none | full | off |
| `full` | all | tacit-micro | full | off |

`CORE_TOOLS` is `read_file`, `write_file`, `edit_file`, `run_shell`, `grep_files`, `list_files` and
`glob_files` (`backend/profiles.py:18-19`). `tools: None` means the whole registry
(`:37`, `:129-130`). No profile turns learning on, because a profile sets cost and containment and does
not decide what Tacit is allowed to remember on its own (`README.md:458-460`).

Two numbers describe how many tools you pay for and they differ on purpose. `cost_of` publishes 20 for
a default install, which is the registry minus the two memory schemas
(`backend/profiles.py:128-136`, `:196-200`). At request time a fresh install with no MCP server is
offered 16, because the four MCP helpers are also removed (`backend/agent.py:889-892`). The first
figure is what the profile owns; the second is what the model sees.

Memory is priced from the mode, not from a plugin, because one switch decides both what is injected and
what the agent may call (`backend/profiles.py:164-166`). `memory_recall` is counted only for a mode that
would actually offer it (`:173-183`), and the memory schemas are excluded from the base tool set so a
memory-off profile is not billed for tools it does not have (`:131-136`).

## What applying a profile does

`apply` switches and reports honestly what it could not turn on: a profile is a bundle of choices, and
if one cannot be honoured the rest still apply and the failed one is named with its reason, because a
profile must never silently give you less than it claims (`backend/profiles.py:243-249`).

The order matters. Capabilities are applied first so the memory mode and budget are in place before the
plugins and the toolset are decided (`backend/profiles.py:257-282`). A capability that has no adapter or
is unavailable is skipped with the reason from the registry (`:265-270`). Then every known plugin is
enabled or disabled to match the bundle (`:287-293`), the memory budget is written (`:294`), MCP direct
mode is set (`:296`), the tool set becomes `disabledTools` in prefs (`:300-302`), the active name is
saved (`:304-306`), and the whole thing is recorded as `profile_applied` with status `partial` when
anything was skipped (`:307-308`).

`capture` saves the live configuration under a name you choose, refusing a built-in name and resolving
legacy aliases first so a capture cannot shadow a real profile (`backend/profiles.py:313-332`). `delete`
refuses built-ins and falls back to `default` if the deleted profile was active
(`:335-348`). Custom names may not shadow built-ins even when listing (`:109-116`).

`current()` expresses the live state in the same shape as a profile, which is what makes "save current
as" and "what would this cost" the same computation (`backend/profiles.py:229-240`).

The routes are five: list, current, apply, capture, delete (`backend/routers/profiles.py:19-48`). The
list carries the active name, the current state, the memory budget and whether the figures are exact
(`:19-23`). Profiles are stored in `~/.tacit/profiles.json` (`backend/config.py:55`).

## The budget that memory gets

The startup memory block is capped at 120 tokens by default with a hard ceiling of 500
(`backend/memory_store.py:30-31`). The configured figure comes from `memoryBudget` in prefs, and a
value above the hard ceiling needs an explicit override flag, which lifts the ceiling to 100,000
(`backend/memory_store.py:140-149`, written by `backend/memory_store.py:157`). In `jit` and `full` modes half the budget is reserved for mid-turn
retrieval rather than spent at startup (`backend/memory_modes.py:25`).

## What a turn costs as it runs

`backend/metrics.py` keeps the counters that make the cost of context discipline legible: what was
injected, and what was avoided by lazy loading, budgeting, compaction and delegation
(`backend/metrics.py:3-6`). The fields are instructions, task list, tool schemas, memory, injected MCP
schemas, then four savings counters, one per avoidance mechanism (`backend/metrics.py:22-25`).

Two ceilings stop a turn from running away. `TACIT_TURN_TOKEN_BUDGET` is a hard token ceiling that
defaults to no ceiling at all (`backend/config.py:152`), and `TACIT_COST_WARN` is the set of window
fractions at which a turn tells you what it is costing (`:149`). Compaction is decided against a
reserve model rather than a flat fraction by default: `TACIT_CONTEXT_RESERVE` leaves 33,000 tokens free
for the next turn's work (`:108`) and `TACIT_CONTEXT_FILL_FLOOR` refuses to compact below half the
window, so a small model is not starved (`:114`). The machinery itself is in
[architecture](architecture.md).

Next: [memory and learning](memory-and-learning.md).

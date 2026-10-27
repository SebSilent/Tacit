# Cost and profiles

Tacit's cost per turn is made of parts you choose, and every one of them is measured before you
choose it. A profile is those choices captured under a name, so moving between "as small as possible"
and "everything on" is one action with a known price (`backend/profiles.py:3-9`).

## What the fixed cost is made of

The standing prompt is assembled in one function (`backend/ai/prompts.py:154-182`):

| Part | Source | When it appears |
|---|---|---|
| Base instructions | `backend/ai/prompts.py:9-25` | always, or `SUBAGENT` (`:49-56`) for a delegated run |
| The version-control rule | `backend/ai/prompts.py:3-7`, chosen at `:159` | always, and it tracks the setting so the prompt never claims something the shell guard would refuse |
| Project scope | `backend/ai/prompts.py:27-31`, added at `:161-162` | when the session has a usable project (`backend/config.py:680`) |
| Scratch-workspace notice | `backend/ai/prompts.py:33-38`, added at `:163-168` | when there is no usable project and a session id, so the model is told where it is working |
| Read-only or chat notice | `backend/ai/prompts.py:40-44`, added at `:173-176` | per mode |
| Skill index | added at `backend/ai/prompts.py:177-181` | only when skills exist; bodies are fetched on demand by the `skill` tool |

On top of that sits the tool schemas, then plugin tools, then memory, then any eagerly injected MCP
schemas. `cost_of` returns those five numbers separately rather than one total, so a reader can see
which part of the bill moved (`backend/profiles.py:204-208`).

The published figures are in the README: 974 characters of standing instructions, about 768 tokens on
Minimal and 2,039 on Default (`README.md:13-15`). A test reads the table back out of the README and
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
not decide what Tacit is allowed to remember on its own (`README.md:199-200`).

Two numbers describe how many tools you pay for and they differ on purpose. `cost_of` publishes 20 for
a default install, which is the registry minus the two memory schemas
(`backend/profiles.py:128-136`, `:194-199`). At request time a fresh install with no MCP server is
offered 16, because the four MCP helpers are also removed (`backend/agent.py:849-852`). The first
figure is what the profile owns; the second is what the model sees.

Memory is priced from the mode, not from a plugin, because one switch decides both what is injected and
what the agent may call (`backend/profiles.py:164-166`). `memory_recall` is counted only for a mode that
would actually offer it (`:173-183`), and the memory schemas are excluded from the base tool set so a
memory-off profile is not billed for tools it does not have (`:131-136`).

## What applying a profile does

`apply` switches and reports honestly what it could not turn on: a profile is a bundle of choices, and
if one cannot be honoured the rest still apply and the failed one is named with its reason, because a
profile must never silently give you less than it claims (`backend/profiles.py:249-254`).

The order matters. Capabilities are applied first so the memory mode and budget are in place before the
plugins and the toolset are decided (`backend/profiles.py:262-287`). A capability that has no adapter or
is unavailable is skipped with the reason from the registry (`:269-275`). Then every known plugin is
enabled or disabled to match the bundle (`:292-298`), the memory budget is written (`:299`), MCP direct
mode is set (`:300-303`), the tool set becomes `disabledTools` in prefs (`:305-307`), the active name is
saved (`:309-311`), and the whole thing is recorded as `profile_applied` with status `partial` when
anything was skipped (`:312`).

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

## Context management, against the alternatives

Prompt size is half of it. The other half is what happens when a session gets long, and that is where
these designs differ most. Everything in this table was read out of the programs themselves on one
machine, not from their documentation.

| | Tacit | Claude Code | Hermes | little-coder | DSH |
|---|---|---|---|---|---|
| Standing prompt | **974 chars** | large + CLAUDE.md | 23,367 chars | 7,747 chars | 6,195 chars |
| Tool schemas | **5.2 KB / 16** (6.6 KB / 20 once an MCP server is configured) | all sent every turn | 52.5 KB / 32 | 10.4 KB / ~29 | 26.7 KB / 25 |
| External tools | **lazy, 4 helpers** | direct | direct | direct | direct |
| Compaction trigger | **window − 33K reserve** | window − ~33K reserve | 50% (75% under 512K) | delegated | delegated |
| Trigger at a 1M window | **967,000** | ~967,000 | 500,000 | none | none |
| Tail kept | **token budget, 4-40 msgs** | none | token budget, floor 8 | none | none |
| Cheap pre-pass before summarising | **yes** | none | yes | none | none |
| Summariser model | **the session's own**, or the fallback a person chose | none | cheap auxiliary | none | none |
| Fallback if summarising fails | **deterministic digest** | none | digest + cooldown | none | none |
| Compaction mid-turn | **yes** | none | yes | none | none |
| Task pinned against elision | **yes** | none | head protected | none | none |
| Per-turn guidance, appended | **yes** | none | none | yes (originated it) | none |
| Project instructions | **indexed, not inlined** | inlined always | a prompt section | guidance hint | a prompt section |
| Task state outside the window | **plugin, off by default** | todo list | state store | none | none |
| Concurrent sub-agents | **yes** | yes | yes | up to 4 | none |
| Prompt-cache breakpoints | **sent on Anthropic** | yes | none | none | none |
| Cost reported while running | **yes** | percentage only | none | none | none |

### Where each piece came from

None of this was invented here.

- **The reserve model is Claude Code's.** Its trigger is not a percentage: it is the window minus a
  roughly fixed ~33K-token reserve, which works out at about 83% of a 200K window and 97% of a 1M one.
  Tacit used a flat 0.65 and was wrong at both ends: it compacted a 1M-token model at 650,000, throwing
  away 350,000 tokens of usable context, and an 8K model at 5,200, leaving too little to answer in.
  Adopting the reserve model is worth 317,000 extra usable tokens on a 1M window, and the triggers now
  agree with Claude Code's (`backend/config.py:104-114`).
- **The compaction mechanics are Hermes'.** Its `context_compressor.py` was ahead of what was here: a
  token-budget tail rather than a fixed message count, a cheap pre-pass that clears old tool output
  before paying a model to summarise it, iterative summaries that refine one handover note instead of
  stacking, headings marked *reference only* so the summary is not read as new instructions, and a
  deterministic fallback when the summariser fails. Tacit previously kept a fixed six messages, had no
  pre-pass, and returned nothing at all on a failed summary, which meant the transcript stayed full and
  the window cap silently elided it.
- **The per-turn guidance pattern is little-coder's**, with credit: the cleanest version of the idea,
  fitted here to this architecture.

### What the mechanics do, and why each is there

- **The trigger counts the whole transcript**, tool results and reasoning included, which is where the
  size actually is (`backend/agent.py:1140-1173`). It reads two measures and answers to the larger: the
  *billed* prompt of the last provider call, at the plain fill target, and the *stored* record, at twice
  the target (`TACIT_STORE_COMPACT_FACTOR`). The store is what elision serves from — a store over the
  target means stubs in the wire, not an over-budget request — and folding it at the plain target
  summarised records whose wire was fine, which is how 1.0.8 came to compact sessions that needed no
  fold: every fold is a model call that replaces verbatim rows with a note. The billed measure keeps no
  headroom because it describes the request the provider will actually make.
- **A cheap pre-pass clears bulky tool output** from the rows about to be folded away, keeping the head
  of each result plus the fact that there was more (`backend/agent.py:1260-1288`). It shrinks what the
  summariser is sent; it does not replace it, and the summariser is still called.
- **The tail is kept by token budget**, between 4 and 40 messages (`backend/config.py:124-126`). Six
  messages is six huge tool results on one turn and six one-line answers on the next: the first
  overflows the summariser's input and the second discards recent work that was cheap to keep.
- **Repeated compaction refines one handover note** instead of stacking summaries, because an earlier
  note falls into the region being summarised rather than being pinned
  (`backend/agent.py:1476-1480`).
- **A region too wide for one summariser call is summarised in parts**: each part is summarised on its
  own and the part notes are merged into the one note, so the far end of a very long region is not
  silently absent from it (`backend/agent.py:1360-1409`).
- **A failed summary still leaves a note**: a deterministic digest of the paths touched, the calls that
  failed and the most recent activity (`backend/agent.py:1294-1318`).
- **The note is framed as a record, not an instruction.** A handover that reads as a task list gets
  acted on, which is how a compacted session restarts work it already finished.
- **Compaction runs during a turn, not only between them** (`backend/agent.py:1565-1592`). Checking the
  size once before a turn begins is what let one investigation reach 1.9M prompt tokens on a
  108,000-token transcript with nothing reclaimed.
- **The standing prompt and the task are pinned against elision** (`backend/agent.py:965-985`). Pinning
  only the first message once lost a sub-agent its instructions: it kept the persona and dropped the
  question.

### Where Tacit is still behind

- **Prompt caching is directed where it can be, and shallowly.** Anthropic does not cache
  automatically: a request has to mark where the cacheable prefix ends, so Tacit speaks the native
  Messages API for Anthropic endpoints and places those breakpoints itself. Everywhere else it adds
  nothing, because OpenAI-compatible servers cache the prefix on their own and an unrecognised field can
  get a request rejected. What it does not do is place breakpoints along the growing transcript for
  Anthropic: two marks cover the stable prefix and the last message, which is the documented pattern,
  but a rolling strategy is possible.
- **The task strip is read-only.** The Task List plugin keeps the remaining work outside the window as
  data and survives compaction because it was never in the transcript, and the strip above the composer
  draws it, open over total, ticking items off. The model owns the list; a person cannot reorder or add
  to it from the strip the way Claude Code's panel allows.
- **Retrieval is keyword-only.** SQLite full-text search where the build has it, plain matching where it
  does not. No embeddings, deliberately, but that is a ceiling as well as a choice.

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

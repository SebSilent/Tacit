# Tacit

**The coding agent where nothing is hidden and nothing moves without you.**

Tacit is a coding agent you run yourself and use in your browser. It reads your files, runs
commands, writes code, searches the web, and connects to outside tools. Everything stays on your
machine. There are no accounts, no telemetry, and no tracking of any kind.

The whole thing is one Python program serving plain web pages. There is no build step, no frontend
toolchain, and no database to install.

---

## Key properties

Three properties decide what an agent costs you and what it can do to you. Tacit is built around all
three.

1. **The fixed cost per turn is the smallest in its class.** 975 characters of standing
   instructions, about 768 tokens on Minimal and 1,889 on Default; DSH pays 8,390 and Hermes
   18,970 every turn. The full accounting is below.
2. **Nothing optional is on, and nothing learns without you.** Memory, plugins, external tools,
   sandboxing and learning are all off until you turn them on. Even then, learning only proposes:
   an approved rule goes back through the same budgets as everything else, never around them.
3. **It reports what it actually enforces.** On Windows, Tacit says `mechanism: none` and enforces a
   timeout, because the base operating system offers no stronger primitive. It does not describe
   limits it cannot apply.

---

## Why the architecture wins

An agent earns the right to run on your machine two ways: by showing you what it is doing, and by
refusing to act where you have not let it. Tacit is built around both, and the numbers are what
they are because of it.

**Paperwork per turn: the smallest in its class.** 975 characters of standing instructions plus
your profile's tool descriptions: roughly 768 tokens on Minimal and 1,889 on Default, against
8,390 for DSH and 18,970 for Hermes, tool descriptions included. At twenty-five turns a task that
gap is tens of thousands of tokens paid before the model reasons about a single line of your
code. Everything else is lazy: skills, knowledge, MCP tool descriptions and project instructions
are indexed by name and fetched only when used.

**Thinking is never billed twice.** A round cut off by an output ceiling is re-asked with the
ceiling removed, so the model's reasoning is never paid for and then thrown away. A cut shell
command hands back the output it printed before the kill, so 90 percent of a computation never
reads as a hung no-op.

**And it does not leave a task half-done.** The delivery guarantee, below, is the one property no
other harness in this comparison has: when the turn would end without the file the task names,
the file gets written, then run, and the turn only finishes when the task's own check passes or
the rounds are spent. A cell that fails still leaves a graded artifact instead of nothing.

---

## Delivery guarantee

On by default. When a turn is about to end without the file the task names, Tacit orders it
written, makes it run, and spends the last minutes of wall budget landing the artifact instead of
dying with one in hand. Where the model answers in prose instead of a tool call and the answer
contains the file, that content is written for it, unedited. This switch is the difference between
"the harness works for the result" and "the harness waits for the model to feel finished": on, a
failed cell is a graded artifact; off, it may be nothing at all.

Off is the raw mode, for steering by hand: no forcing, no verification rounds, no clock-driven
delivery, and the turn ends when the agent ends it. One run: `TACIT_DELIVER_GUARANTEE=0`. For
keeps: `"deliverGuarantee": false` in `~/.tacit/prefs.json`.

One mechanic is not part of the switch: a round destroyed by our own output ceiling is re-asked
with the ceiling removed, either way, because billing that model thinking as waste is not a
setting.

---

## How it compares

Measured on one machine, with one rule: characters divided by four. That is the fallback heuristic
Tacit uses when no exact tokenizer is installed, and it is the heuristic DSH documents for its own
token meter, so nothing is measured by a standard it does not already apply to itself.

| System | System prompt | Tools | Tool schemas | Fixed cost per turn |
|---|---|---|---|---|
| **Tacit, Minimal** | **975 chars** | **7** | **2.0 KB** | **~768 tokens** |
| Pi | 1,352 chars | 7 | 4.5 KB | ~1,600 |
| **Tacit, default profile** | **975 chars** | **20** | **6.4 KB** | **~1,889** |
| little-coder | 7,747 chars | ~29 | 10.4 KB | ~4,600 |
| DSH | 6,195 chars | 25 | 26.7 KB | ~8,390 |
| Hermes Agent | 23,370 chars | 32 | 51.3 KB | ~18,970 |

Ordered by cost. Tacit appears twice because its profile system is the variable, and both rows are
the same program.

### The like-for-like row

The first row is the one worth arguing about, because it compares the same seven capabilities on
both sides. Tacit's Minimal profile ships exactly the tools Pi ships, read, write, edit, shell, grep,
list and glob, and nothing else.

| Same seven capabilities | Tacit | Pi |
|---|---|---|
| Tool schemas | **2,001 chars** | 4,626 chars |
| System prompt | **975 chars** | 1,352 chars |
| **Fixed cost per turn** | **~768 tokens** | **~1,600** |

Same tools, less than half the cost. Two things account for it. Pi's descriptions are longer: its
`read` tool takes the same three parameters as Tacit's and still costs 182 tokens against 67. And
Tacit's standing prompt is smaller to start with. Pi's `grep` does expose more options than Tacit's,
so that one is not a fair fight, but the rest are.

### What each difference actually buys

| Property | Tacit | DSH | Hermes Agent |
|---|---|---|---|
| Runtime dependency on another harness | **none** | plugin runtime | gateway ecosystem |
| Fixed cost per turn | **~768 to ~2,308** | ~8,390 | ~18,970 |
| Memory in the prompt | **off by default, hard token budget** | not applicable | accumulated in the system prompt |
| Learning that changes behaviour | **approval first, off by default** | not applicable | automatic |
| Isolation on Windows | **not available: says so** | not verified here | not applicable |
| Where state lives | **one folder you can delete** | its own | its own |

DSH's own documentation devotes a section to explaining why tool schemas are re-paid on every step.
That is the cost Tacit exists to make visible, and the profiles exist to let you move.

### Reproducing it

Tacit's row is read from its own profile selector, in **Settings > Profile**: the Minimal profile
reports the same figure with seven tools. The other rows were measured from the installed programs
on this machine, and every one of them is reproducible from their own tooling.

---

## How context is managed, against the alternatives

Prompt size is half of it. The other half is what happens when a session gets long, and that is
where the designs differ most. Everything below was read out of the programs themselves, on this
machine, not from their marketing.

| | Tacit | Claude Code | Hermes | little-coder | DSH |
|---|---|---|---|---|---|
| Standing prompt | **975 chars** | large + CLAUDE.md | 23,367 chars | 7,747 chars | 6,195 chars |
| Tool schemas | **5.2 KB / 16** (6.6 KB / 20 once an MCP server is configured) | all sent every turn | 52.5 KB / 32 | 10.4 KB / ~29 | 26.7 KB / 25 |
| External tools | **lazy, 4 helpers** | direct | direct | direct | direct |
| Compaction trigger | **window − 33K reserve** | window − ~33K reserve | 50% (75% under 512K) | delegated | delegated |
| Trigger at a 1M window | **967,000** | ~967,000 | 500,000 | none | none |
| Tail kept | **token budget, 4–40 msgs** | none | token budget, floor 8 | none | none |
| Cheap pre-pass before summarising | **yes** | none | yes | none | none |
| Summariser model | **nominatable** | none | cheap auxiliary | none | none |
| Fallback if summarising fails | **deterministic digest** | none | digest + cooldown | none | none |
| Compaction mid-turn | **yes** | none | yes | none | none |
| Task pinned against elision | **yes** | none | head protected | none | none |
| Per-turn guidance, appended | **yes** | none | none | yes (originated it) | none |
| Project instructions | **indexed, 48 tokens** | inlined always | a prompt section | guidance hint | a prompt section |
| Task state outside the window | **plugin, off by default** | todo list | state store | none | none |
| Concurrent sub-agents | **yes** | yes | yes | up to 4 | none |
| Prompt-cache breakpoints | **sent on Anthropic** | yes | none | none | none |
| Cost reported while running | **yes** | percentage only | none | none | none |

### What Tacit took from each

None of this was invented here, and it is worth saying where it came from.

- **The reserve model is Claude Code's.** Its trigger is not a percentage: it is the window minus a
  roughly fixed ~33K-token reserve, which works out to about 83% of a 200K window and 97% of a 1M
  one. Tacit used a flat 0.65 and was wrong at both ends: it compacted a 1M-token model at 650,000,
  throwing away 350,000 tokens of usable context, and an 8K model at 5,200, leaving too little to
  answer in. Adopting the reserve model is worth **317,000 extra usable tokens** on a 1M window, and
  the numbers now agree with Claude Code's exactly.
- **The compaction mechanics are Hermes'.** Its `context_compressor.py` was well ahead of what was
  here: a token-budget tail rather than a fixed message count, a cheap pre-pass that clears old tool
  output before paying a model to summarise it, iterative summaries that refine one handover note
  instead of stacking, headings marked *reference only* so the summary is not read as new
  instructions, and a deterministic fallback when the summariser fails. Tacit previously kept a fixed
  six messages, had no pre-pass, and returned nothing at all on a failed summary: which meant the
  transcript stayed full and the window cap silently elided it instead.
- **The per-turn guidance pattern is taken from little-coder**, with credit: it is the cleanest
  version of the idea, and Tacit's take on it is the same principle fitted to this architecture.

### Where Tacit is still behind

- **Prompt caching is now directed where it can be.** Anthropic does not cache automatically: a
  request has to mark where the cacheable prefix ends: so Tacit speaks the native Messages API for
  Anthropic endpoints and places those breakpoints itself. Everywhere else it adds nothing, because
  OpenAI-compatible servers cache the prefix on their own and an unrecognised field can get a request
  rejected outright. Which applies is detected, overridable, and a rejection is retried once without
  the markers so caching can never be the reason a turn fails.
  What Tacit still does not do is place breakpoints on the growing transcript for Anthropic: two marks
  cover the stable prefix and the last message, which is the documented pattern, but a longer rolling
  strategy is possible.
- **The task strip is read-only.** The Task List plugin keeps the remaining work outside the window
  as data, and it survives compaction because it was never in the transcript. The strip above the
  composer now draws that list — open over total, ticking items off as the agent finishes them —
  but the model owns the list: the interface watches and does not edit. Claude Code's panel lets
  the person reorder and add; that is the part still missing here.
- **Retrieval is keyword-only.** SQLite full-text search where the build has it, plain matching where
  it does not. No embeddings, deliberately, but that is a ceiling as well as a choice.

---

## Why the cost stays low

Every message carries a fixed overhead: the instructions the assistant always follows, plus the
descriptions of every tool it has been given. That overhead is present before your question is read,
and it is paid again on every turn.

Most tools do not show you this number. Tacit does, and then gives you the switches to move it.

- **A standing prompt of about 980 characters**, roughly 245 tokens. That is the complete set of
  always-on instructions, and it is the same for every profile.
- **Capabilities load on demand.** Skills, knowledge and external tool descriptions are indexed by
  name. An entry costs about 84 characters up front instead of its full text, and the full text is
  fetched only when it is used.
- **Heavy exploration goes to a sub-agent.** The `task` tool runs in its own fresh context with a
  separate step budget, and returns only its report. Reading a large codebase costs the main
  conversation the summary, not the reading. The report is trimmed from both ends inward, so its
  conclusion survives, and the tokens the sub-agent spent out of your window are counted in the
  session's token accounting rather than vanishing.
- **Memory is capped and retrieved, not carried.** A fixed token budget, default 120, enforced. The
  rest is searched and injected only when relevant, and what the budget held back is reported.
- **Older turns compact in place.** The trigger counts the whole transcript: tool results and
  reasoning included, which is where the size actually is: and the summary is written from a digest
  of what ran, not just what was said.
- **The trigger is a reserve, not a percentage.** What has to stay free is room for the next turn's
  work, and that is roughly constant rather than proportional, so compaction fires at the window minus
  about 33,000 tokens: near 83% of a 200K window and 97% of a 1M one. A flat fraction got both ends
  wrong: it cost a 1M-token model 350,000 tokens of usable context and left an 8K model too little to
  answer in. Below a window the reserve exceeds, it floors at half.
- **Old tool output is cleared before anything is summarised.** A cheap pre-pass, and often the whole
  job: if dropping the bodies of results already being folded away brings the transcript back under
  budget, no model is paid to write a summary at all.
- **A failed summary still leaves a note.** If the summariser errors or answers with nothing, a
  deterministic digest of the paths touched, the calls that failed and the most recent activity takes
  its place. It used to return nothing, which left the transcript full for the window cap to elide.
- **The summary is framed as a record, not an instruction.** A handover note that reads as a task list
  gets acted on, which is how a compacted session restarts work it already finished.
- **The tail is kept by token budget, not message count.** Six messages is six huge tool results on
  one turn and six one-line answers on the next; the first overflows the summariser and the second
  discards recent work that was cheap to keep. The task and any earlier summary are pinned through
  every compaction, and repeated compaction refines one handover note instead of stacking them.
- **Compaction happens during a turn, not only between them.** Checking the size once, before a turn
  begins, is what let one investigation reach 1.9M prompt tokens with nothing ever reclaimed. A turn
  that outgrows its budget is summarised as it goes, which keeps information the window cap would
  otherwise throw away.
- **The window cap follows the model.** What is sent is bounded by the model's own context window
  where it is known, not by one flat number that suits neither a 1M-token model nor an 8k one. When
  something must still be elided, the standing prompt, the task and any compaction summary are
  pinned: the agent is never left holding instructions it can no longer see the question for.
- **Cost is reported while the turn is running.** Crossing 25%, 50% or 75% of the model's window
  raises a warning in the interface, so a runaway turn is visible before it finishes rather than in
  a dashboard afterwards. `TACIT_TURN_TOKEN_BUDGET` sets a hard ceiling that stops the turn and asks
  for a report instead; it is off by default, like every other constraint here.
- **Off by default.** Memory, plugins, external tool servers and sandboxing are disabled until you
  enable them, and can be disabled again at any time.

Nothing above is a claim about the model. It is about what Tacit chooses to put in front of it.

---

## Your project's own instructions

Most projects state their rules somewhere, and an agent that does not read them works against the
grain of the codebase. Tacit looks for `AGENTS.md`, `CLAUDE.md`, `TACIT.md`, `.cursorrules`,
`.tacit/instructions.md` and `.github/copilot-instructions.md` in the project root, plus `README.md`,
`SPEC.md` and `CONTRIBUTING.md` as documentation.

Root only, and deliberately so: walking the tree for instruction files in every subdirectory is how a
harness ends up reading a vendored copy of someone else's rules, and it costs a scan on every turn.

What it does with them is the interesting part, because inlining them is exactly the thing this whole
design is trying not to do. Three modes:

| Mode | What is injected | Measured on one project |
|---|---|---|
| `off` | nothing | 0 tokens |
| `index` (default) | the names found and their sizes | **48 tokens** |
| `inline` | the full text, up to a budget | 3,340 tokens |

The index is the default because it is nearly free and it removes the guesswork: the model is told
that a 3,340-token `README.md` exists and can decide to read it, instead of being handed a vague
instruction to "read the project's own instructions if it has them" and spending a tool call finding
out whether any do. Inline mode is there when you would rather pay every turn than spend the call,
is budgeted, and reports what the budget held back.

The block goes into the standing prefix before the transcript, because it depends only on the folder
: putting it there keeps a provider's prefix cache intact instead of invalidating it. Its cost is a
line of its own in the token accounting, never folded into the base prompt figure.

---

## Prompt caching, per provider

An agent loop re-sends its whole transcript on every step. On a long session that is the entire bill:
one measured run showed a 108,000-token transcript costing 1.9M prompt tokens, because it was sent
about eighteen times. Caching is what makes that cheap, and providers do not agree on how it works.

So Tacit picks the best strategy the endpoint actually supports, rather than applying one everywhere:

| Endpoint | Strategy | What is sent |
|---|---|---|
| Anthropic (`api.anthropic.com`) | native Messages API | breakpoints on tools, system and the last message |
| OpenRouter | OpenAI shape, passthrough | one breakpoint on the system block |
| Any other OpenAI-compatible server | provider-side automatic | **nothing added** |

The last row is the important one. Automatic prefix caching needs no markers, and a server that has
never seen `cache_control` may reject the body outright: so adding markers there would risk a working
setup to gain nothing. Tacit adds them only where they are understood.

Anthropic needs a native transport for this, and it is a real translation rather than a rename:
`system` is a top-level parameter instead of a message, tools carry `input_schema` instead of
`parameters`, a tool result is a `tool_result` block inside a *user* message so a run of consecutive
results has to be merged into one turn, `max_tokens` is required, temperature must be omitted while
extended thinking is on, and the stream is a sequence of typed events rather than OpenAI's deltas.

Three guarantees around it, because an optimisation must never be a new way to fail:

- **A rejection is retried once without the markers.** It is detected from the HTTP status line,
  before any event has streamed, so nothing is ever half-sent and then repeated.
- **`TACIT_PROMPT_CACHE=off` disables it** for a provider that misbehaves.
- **What is in force is reported** as `breakpoints sent` or `provider-side`: along with the hit rate
  actually observed. A measured run on an OpenAI-compatible
  endpoint reported **71.1%** of prompt tokens served from cache; that figure used to be computed and
  thrown away, so the field had always been blank.

---

## Isolation

Three backends behind one interface, and the report always names the one that actually ran. Same
tools, same change report, same ledger either way; you choose how hard the walls are.

| Backend | Where | What it enforces |
|---|---|---|
| `none` (the default) | everywhere | fully transparent local execution: timeout, full change report, nothing hidden |
| `tacit-micro`, the built-in layer | everywhere | the strongest primitive the OS offers, plus a timeout, limits where the platform allows, and a report of every file that changed |
| `container` (opt-in) | anywhere a Docker or Podman runtime exists, Windows included over WSL2 | network namespace, read-only or read-write project bind, private `/tmp`, memory and CPU ceilings, a PID limit, privilege escalation off; a timeout kills the container itself |

Inside `tacit-micro`, the OS provides the walls: Linux gets bubblewrap (read-only or read-write
project bind, private `/tmp`, an optional network namespace, the sandbox dies with the parent) and
macOS gets `sandbox-exec` with a generated profile (no network, writes confined to the workspace).
On Windows the base OS has no equivalent primitive, and the report says exactly that instead of
drawing a wall that is not there, which is what the container backend is for.

Everything above was checked against a **real daemon**, by hand, on Docker Engine 29.8.1 with a
WSL2 backend: `--network none` really refuses a connection to `1.1.1.1:53`, a read-only bind is
rejected by the kernel (`cannot create /workspace/...: Read-only file system`), a write from inside
lands on the host and shows up in the change report, `/tmp` is a `tmpfs`, `NoNewPrivs:
1` is present, `pids.max` reads back `64` when 64 was asked for, `memory.max` reads back
`268435456` for `memory_mb=256`, nothing of the host filesystem is reachable, and after a timeout
the container is gone from `docker ps -a`. A 4-second timeout returned in 5.8 seconds with exit 124
and no surviving container. `test_isolation.py` covers the same ground against a stubbed runtime so
the logic is tested on every commit.

The project is bind-mounted, not copied, so snapshot, restore and the change report describe the
files you actually have. Nothing is downloaded for you: a missing image is reported with the exact
`docker pull` command that would fetch it, and installing it is your call. The container backend is
never required, and nothing is installed for you, ever.

**The timeout is enforced, not merely reported.** `TACIT_SHELL_TIMEOUT` is documented as the seconds a
command may run, and on Windows it was not: killing a `cmd /c` or `.bat` wrapper left its grandchildren
alive holding the captured pipe, so the call blocked long after the timeout fired. Measured at the
time: two orphaned processes still running minutes later, and a shell tool that took 180 seconds to
return from a command that should have died in three. The whole tree is now killed and the drain is
bounded, so a timeout returns in about the time it names.

**The audit ledger.** Every tool call, sandbox decision, memory injection, learning proposal and
approval is appended to `~/.tacit/audit.jsonl`. It is a plain JSONL file you can read, search or
delete. Credentials are masked on the way in, at any depth: including a token carried in a URL's
query string, which is the shape that usually reaches a log. Nothing in it is ever edited or removed:
the interface's own *clear* action retires the file under a timestamped name and records the rotation
as the first entry of the new one, so the history stays complete.

---

## Learning, with approval first

Tacit reads its own finished conversations and proposes what it noticed. It will not act on any of
it by itself.

1. **A background analyzer reads completed sessions**, on a timer, off the request path. It uses
   pattern matching over your own turns: explicit recall ("remember that"), standing orders
   ("always", "never"), corrections ("no, don't", "instead"), and stated preferences. It makes no
   model calls.
2. **Each finding becomes a proposal artifact** with its provenance attached: which session, which
   turn, and the exact sentence that triggered it. You see the sentence, not a summary of it.
3. **Nothing is used until you approve it.** Proposals sit in `proposed` state. Approving one writes
   it to memory under the memory budget, or to a skill file. Rejecting it marks it and it is never
   injected.
4. **A repeated rule raises confidence instead of duplicating.** Seeing the same correction five
   times produces one proposal that is more certain, not five proposals.

The default learning mode is `learn-off`: nothing is read, nothing is proposed, and no background
worker runs. `propose` is the explicit opt-in, and it still cannot change anything; `auto` and
`auto-low-risk` exist if you want them, and they are your decision to make, in **Settings >
Learning**, which also holds the proposals and their approve, reject, edit and delete actions. An
install from before this default keeps whatever mode it had stored — only the default moved.

The analyzer adds no tool to the agent and changes no prompt text. The background worker exists
only while learning is on: it is not started when learning is off, a mode change starts or stops it,
and switching learning off stops a running worker rather than leaving it waking up to do nothing.

---

## Memory, with a budget and provenance

Persistent memory records things such as a preferred convention, a decision already made, or how a
project is structured. It can also consume a large part of a context window if it is not limited.

The Memory Vault limits it:

- **Disabled by default.** Nothing is stored until you enable it.
- **A fixed budget.** Memory may use at most 120 tokens of your prompt by default, with a ceiling of
  500 unless you change it. The limit is enforced, and setting it to zero injects nothing.
- **Nothing is stored without approval.** Tacit can read a conversation and propose memories, but
  each proposal is shown with its type, its confidence and its token cost before anything is saved.
- **Every entry carries provenance.** You can see where a memory came from: your own note, something
  you approved, or a session summary, and which session.
- **Every entry is editable.** Edit, disable, unpin or delete any memory at any time, and the prompt
  cost updates immediately.
- **Memories are retrieved, not carried.** The store searches on demand rather than injecting the
  whole set. Retrieval is keyword based, using SQLite full-text search when the build provides it
  and plain matching when it does not. There is no vector database and no embedding model.
- **Sessions can be summarised.** A finished session can be distilled into one durable note, with
  its source session recorded, so the work is recallable without the transcript.

The dashboard shows the current cost of memory and how much the budget avoided.

---

## Profiles

A profile is a named bundle of the choices that decide the fixed cost, so you do not have to toggle
several things each time you change how you are working. Switching applies immediately.

| Profile | Tools | Sandbox | Memory | Learning | Fixed cost |
|---|---|---|---|---|---|
| **minimal** | 7 | none | off | off | ~768 tokens |
| **default** | 20 | none | off | off | ~1,889 tokens |
| **safe** | 20 | tacit-micro | explicit | off | ~2,308 tokens |
| **power-isolation** | 20 | tacit-micro | explicit | off | ~2,308 tokens |
| **power-memory** | 20 | none | full, budgeted | off | ~2,308 tokens + budget |
| **full** | 20 | tacit-micro | full, budgeted | off | ~2,308 tokens + budget |

The four profiles that turn the Memory Vault on cost about 419 tokens more than `default`, because
the vault contributes five tool schemas of its own. That is the whole difference between the rows:
they are identical in prompt cost and differ only in what is switched on. Every figure here is
recomputed from the live configuration by **Settings > Profile**, so the table cannot drift without
the interface disagreeing with it.

No profile turns learning on — not even proposing. That is a deliberate choice: a profile sets cost
and containment, and does not decide what Tacit is allowed to remember on its own. Proposing is a
selection in **Settings > Learning**, made explicitly or not at all.

**minimal** is the one to reach for on a small model or a tight context window. It hands the agent
read, write, edit, shell, grep, list and glob, and switches off the other seventeen built-in tools
as well as every optional capability. You give up browsing, sub-agents, plan mode, research and
snapshots, and in exchange the standing prompt drops to about a third of its normal size.

You can also save the current configuration under your own name and delete it later. The presets
matter less than the habit they encourage: the cost of a configuration is knowable before you adopt
it, rather than discovered later in a bill or a truncated conversation.

---

## Install

**macOS / Linux / WSL**

```sh
curl -fsSL https://raw.githubusercontent.com/SebSilent/Tacit/HEAD/install.sh | bash
```

**Windows** (PowerShell)

```powershell
irm https://raw.githubusercontent.com/SebSilent/Tacit/HEAD/install.ps1 | iex
```

**Windows** (cmd)

```bat
powershell -NoProfile -c "irm https://raw.githubusercontent.com/SebSilent/Tacit/HEAD/install.ps1 | iex"
```

The installer downloads the source, creates a private virtual environment inside the checkout, and
writes a `tacit` launcher at `~/.local/bin/tacit` or `%LOCALAPPDATA%\Tacit\bin\tacit.cmd`.
Re-running it updates the checkout. Flags: `--no-browser` (or `TACIT_NO_BROWSER=1`) skips the
optional browser-automation step of about 130 MB, `--dir` chooses the checkout location, and
`--help` lists the rest.

### Requirements

- **Python 3.10 or newer.** This is the only real prerequisite. Everything else is optional.
- **`curl`** on macOS and Linux, to fetch the installer. Windows uses PowerShell, which is already
  present.
- **`git`** if you have it, which lets the installer update an existing checkout. Without it the
  installer downloads a source archive instead.
- **Node 22 or newer** is optional, and only used for browser automation. Skip it with
  `--no-browser`.

There is no database to install, no build pipeline, no frontend toolchain, and nothing to configure
before the first run. Neither Hermes nor DSH is required, referenced, or contacted at any stage.

### Run from source

Tacit runs on its own virtual environment, never on whatever `python` happens to be first on your
PATH. That is deliberate: an ambient interpreter may belong to another tool, and may not have the
packages Tacit needs.

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

On Windows that is `.venv\Scripts\python.exe` in both lines. Then double-click **`run.bat`**, or run
**`./run.sh`**. Both start the server on `.venv` and refuse to start without it, telling you the two
commands above rather than quietly using a different Python.

Open **http://localhost:8550**. On first start Tacit creates `~/.tacit`, which is empty. Add a
provider in **Settings > Providers** and your keys are stored in `~/.tacit/.env`. Nothing is
imported from elsewhere on the machine.

### Browser automation (optional)

The `browser` tool drives a real Chromium browser and is the only part that needs Node. Install
either of the following:

```sh
npm install
```
```sh
pip install playwright && playwright install chromium
```

Then restart. If neither is present, the tool reports this instead of failing silently. To reuse an
existing installation, set `TACIT_PLAYWRIGHT_PATH` to point at it.

---

## What you can do

- **Work in a project.** Pick a folder and Tacit reads, edits and runs commands there.
- **Get a second opinion.** The Assistant reads the session and helps you direct it, without the
  agent ever seeing that conversation.
- **Use any model.** OpenAI-compatible endpoints, local servers such as Ollama, llama.cpp or
  LM Studio, or hosted providers. Configure several at once, and give the Assistant a different one
  from the agent.
- **Undo changes.** Snapshots record your project before changes, with a timeline, a file-by-file
  comparison, and one-click restore.
- **Connect outside tools.** Tacit speaks MCP, the open standard for connecting assistants to
  tools. Descriptions stay out of your prompt until a tool is actually used.
- **Remember what matters.** A memory vault stores preferences, decisions and project facts within a
  token budget you set. It is off until you turn it on.
- **Learn from your own corrections.** A background analyzer proposes rules, and you decide.
- **Plan first.** Plan mode researches your project, asks clarifying questions, and drafts an
  approach for you to approve before any files are changed.
- **Delegate.** A sub-agent reads a large amount of material in its own separate context and returns
  only a summary, so the main conversation stays small. Several asked for in one step run
  concurrently, since they are independent and read-only.
- **Work with the project's own rules.** Instruction files are found rather than guessed at, and
  indexed rather than inlined by default: see below.
- **Keep the remaining work in view.** An optional task list holds the steps of a multi-part job
  outside the transcript, where a compaction cannot summarise them away.

---

## Meet the Assistant

Tacit gives you two conversations instead of one.

```
+-----------+------------------------------+-------------------+
|  sessions |         the main agent       |     Assistant     |
|           |   writes code, runs commands |  helps you think  |
+-----------+------------------------------+-------------------+
```

The main agent does the work, and it stays in the middle where you expect it.

The **Assistant** sits on the right and helps with the work around the work: drafting a prompt to
give the agent, thinking an approach through, noticing what got missed, explaining what the agent
just did. It is the conversation you would otherwise be having in a second tab, with all the
copy-pasting that involves.

Three things make it useful rather than annoying:

- **It can read the session.** It sees what you asked and what the agent answered, so you never have
  to paste context into it. A further switch adds a one-line digest of the tools behind each answer,
  which is what makes "explain what the agent just did" answerable; it is off by default because tool
  results are the expensive part of a transcript.
- **The agent cannot see it.** The Assistant is invisible to the main agent. Nothing it says reaches
  the agent unless you copy it across yourself, so your notes, hesitations and half-formed ideas
  stay between you and it.
- **It is confined to the session.** Every conversation has its own Assistant. Switch sessions and
  you switch assistants; delete a session and its assistant goes with it.
- **It answers in Markdown.** The same renderer the main chat uses, so a drafted prompt arrives as
  a fenced code block with a one-click copy button, and headings, lists and tables render as
  themselves. Its persona asks for exactly that shape, so a prompt to paste is a block, not prose.

A second conversation is a second cost, so you decide exactly what it may read, and the price is
shown before anything is sent:

```
   ~182 tokens of session context
   you 20   persona 162   history 0   tools 0

   [x] Your prompts     [x] Agent replies     [x] Session info
   [ ] Tool calls       [ ] Read-only tools
   Last [20] turns
```

Read-only tools add roughly **1,130 tokens** on their own, which is why they are off by default, and
they are read-only in fact and not only by label: the switches that write — delegation, `evidence
add`, and any external tool call — are refused at the point of the call, not merely hidden from the
schema. Hiding a tool does not stop a model that remembers its name. The
Assistant has its own provider, model and thinking level as well, so pointing a reasoning-heavy
model at the thinking and a cheap one at the code is a couple of clicks.

Its counter sits above its conversation, so a second conversation is never a hidden second bill.

---

## Connecting external tools (MCP)

Tacit supports **MCP (Model Context Protocol)**, the open standard for connecting assistants to
tools. If a tool provides an MCP server, Tacit can use it without any changes to the tool.

Add one in **Settings > MCP** by giving it a name and a command such as `npx`, `uvx`, `python`, or
any executable path, or by providing an HTTP endpoint.

When you connect a server, Tacit discovers its tools and lists them, but does not insert their
descriptions into your prompt. The agent receives four small helper tools instead, and looks up what
it needs — and with **no server configured, those four schemas are not offered at all**: they cost
nothing and appear the moment a server is added, with no restart and no profile switch. The helpers
are:

- search the tool catalogue by intent
- activate a tool for a limited number of turns
- call a tool directly
- list connected servers and the cost of their tools

A server that offers fifty tools therefore costs nothing until one of them is used. The dashboard
shows the difference.

Controls available for each server:

- New servers are added disabled. They are never installed or started automatically.
- Per-server allow and deny lists decide which tools may run.
- Tools that look destructive, such as delete, deploy, push or execute, ask for confirmation first.
- An audit log records every connection, discovery and call. Environment values and credentials are
  masked in both the interface and the log.

A **Direct MCP mode** toggle is available if you prefer to load every tool up front. It is off by
default.

### Bringing notes in from another tool

Tacit does not connect to other harnesses. It does not call them, read their directories, or need
them installed. If you have notes exported from somewhere else, there is a one-time importer:

- **Point it at a file or folder you exported.** Nothing is detected or scanned.
- **You see a preview first.** What it found, what it would cost, and anything it refused because it
  looked like a credential.
- **Then it imports, if you say so.** Additively, without overwriting, and without duplicating if
  you run it twice.
- **After that the relationship is over.** Nothing stays connected, and nothing reads the source
  again.

Two text shapes are understood, because they are what exports tend to look like: prose separated by
a section sign, and markdown headings and bullets. Tacit can also export its memory and skills as
plain text for you to take anywhere.

This is a migration, not an integration. Import your bookmarks once, then close the tab.

---

## Optional dependencies

Tacit installs nothing on its own. Where a feature needs something that is not in the standard
library, the interface says which package, why it is needed, roughly how large it is, and the exact
command that would install it. Nothing runs until you run it. Until then the feature reports itself
as unavailable rather than half working.

The rows are checked on the machine, now, not asserted: browser automation looks for a playwright
install (node or pip) rather than stopping at "node exists"; container isolation asks the daemon for
its version and distinguishes a missing binary from a daemon that is not running; rich memory
actually creates an FTS5 table in memory to see whether this Python build has it. A group that fails
its check still names the command that would fix it.

| Group | Needed for | Ships with |
|---|---|---|
| Advanced isolation | bubblewrap on Linux | your distribution |
| Container isolation | Docker or Podman, any platform | a separate install |
| Browser automation | the `browser` tool | about 130 MB |
| Rich memory | the memory vault | built in, SQLite |
| Autonomous learning | the session analyzer | built in |
| Migration importers | reading your own export | built in |

---

## Plugins

Plugins add tools, connections or panels. They are disabled by default, and each one reports the
tokens it would add to your prompt before you enable it. A plugin's code is loaded only after you
enable it.

Manage them in **Settings > Plugins**. Tacit ships with two:

| Plugin | Purpose | Cost when enabled | Default |
|---|---|---|---|
| **Memory Vault** | Persistent memory with a token budget | 419 tokens | Off |
| **Task List** | The turn's remaining work, kept outside the context window | 132 tokens | Off |

Both figures are measured from the schemas the plugin actually contributes, not from a budget it
declares about itself: a plugin that declared zero used to show zero in the panel while really
adding its tools to every request. Disabled plugins are priced too, since that is the number you need
in order to decide.

Anything else is a plugin you write or install yourself. There is no bundled plugin that reaches
outside your machine.

---

## Profile selector

**Settings > Profile** is the profile selector: the named bundles, their measured cost, apply and
delete, and a box to save the current setup under your own name. Switching applies immediately. The
token tables that used to sit under it were removed at the operator's request — the accounting still
happens and is still local, but the tab's job is to choose a bundle, not to report on it.

---

## Snapshots and undo

Snapshots belong to the session that took them. The clock button in the top bar, next to the
workspace picker, opens this session's timeline. For each snapshot you can:

- see when it was taken, its label, and the files it contains
- **compare** it with the project as it is now, listing changed, added and removed files
- **restore** it, after a confirmation prompt

The agent takes a snapshot automatically before the first edit of a turn — the first `write_file`,
`edit_file` or `run_shell` of a turn, once, whether the edit goes through a tool or a shell command.
It used to be offered the choice and left to take one; across two real sessions it never did, so the
undo timeline was empty at exactly the moment it would have been needed. It is also not offered the
tools any more: `snapshot`, `list_snapshots` and `restore` were removed from the agent's tool list,
because a model that can restore its own work can hide what it just did, and the undo timeline is a
person's instrument. One snapshot per turn keeps it cheap, and a snapshot that fails is reported
without blocking the edit. Snapshots taken before per-session tracking carry no session marker; they
are counted in the panel's note line rather than silently vanishing.

---

## Sessions are isolated from each other

Sessions are standalone. Work done in one never appears in another, in the
backend or in the interface, and the guarantees are enforced rather than
promised:

- **One session, one running turn.** Live turn state — busy, starting, what kind
  of work, since when, how many turns the session has started — is held in a
  process-local registry keyed by session id (`backend/session_state.py`), not on
  a socket and not in the stored record. A second window attaching to a busy
  session sees the busy flag, and a second prompt for that session is refused
  from any window. The interface reads the registry at `GET /api/sessions/state`
  — it is also what draws the left bar's activity indicators.
- **A turn's worker is bound to its session at spawn.** Its events, its metrics
  and its transcript writes go to the session it was started in. Nothing can
  redirect them to another session afterwards — the worker carries its own
  session id, and the registry is cleared by the worker's own exit, so a browser
  that disconnects mid-turn cannot mark a running session idle.
- **Stop is scoped, and it really stops.** Abort on one session cannot touch a
  turn in another; each connection owns its own stop event, and the registry
  carries no stop state at all. The left bar carries a stop control on every
  working session's row, and it stops that session's turn through that
  session's own socket — including a session you are not currently looking at.
- **A turn runs in its own process, so it can be killed.** A thread cannot be
  killed in Python under any circumstance, and a turn is a unit of work a
  person must be able to kill: blocked in a model read, a stuck shell command,
  a runaway regex. So the turn runs in a worker process owned by its session
  (`backend/turn_worker.py`), streaming its events back as JSON lines. Stop is
  two-stage: a cooperative cancel first — the worker stops between events,
  closes its model stream, kills its own shell trees and exits cleanly, so the
  partial transcript is preserved — and if it has not exited within five
  seconds, the process tree is killed outright (`taskkill /T /F` on Windows,
  `killpg` on POSIX). The interface is told which one ran. The turn's
  background jobs die with it. `TACIT_TURN_WORKER=thread` restores the old
  in-process path.
- **A socket is born attached to one session.** The interface holds one
  WebSocket per session, not one per tab: switching sessions opens a different
  socket, and a session with a running turn keeps streaming into its own
  socket — and its own transcript — while you sit in another one. The in-band
  `switch` command is deleted from the protocol: a socket is never re-pointed
  at another session, and the server answers the old command with the reason.
- **The server never rebinds a connection.** Creating a session, changing mode,
  or implementing an approved plan creates the session and returns its id; the
  client opens a fresh socket for it. A running turn's worker keeps writing
  into the session it was started in regardless — the capture is defence in
  depth, not the mechanism.
- **The left bar shows what is working.** Every session row carries a state
  dot — idle, or pulsing while a turn runs — fed by `GET /api/sessions/state`
  (polled lightly) and refined live by the session's own socket events. A
  working row carries its own stop control.
- **Delegation is per-session.** The sub-agent panel shows the active
  session's delegations, keyed by session id and call id, so two sessions
  delegating at once never cross-report; switching sessions swaps the card
  set.

---

## Where your data lives

Everything Tacit reads or writes is under **`~/.tacit`**. Nothing is written inside this repository,
and nothing is written into another tool's directory.

```
~/.tacit/
├─ models.json        providers + models      (Settings > Providers)
├─ .env               API keys
├─ prefs.json         preferences, defaults
├─ skills/            your skills      ─┐ listed by name, loaded on demand
├─ knowledge/         reference cards  ─┘
├─ memory.db          memory vault          (Settings > Memory, off by default)
├─ learning.json      learning proposals    (Settings > Learning)
├─ capabilities.json  capability selections
├─ audit.jsonl        append-only ledger of what happened
├─ audit.jsonl.<ts>   retired ledgers, kept rather than deleted
├─ mcp.json           external tool servers (Settings > MCP)
├─ plugins.json       plugin state          (Settings > Plugins)
├─ mcp_audit.jsonl    external tool activity log
├─ benchmarks.json    per-model temperature / max_tokens (model_settings.py)
├─ evidence.jsonl     recorded citable facts
├─ checkpoints/       project snapshots for undo
├─ plans/             approved plans
├─ tasks/             per-session task lists  (Task List plugin, off by default)
├─ sessions/          transcripts + metadata
└─ github.json        hosting panel state
```

Deleting `~/.tacit` removes all stored data.

---

## How it works

```
backend/
├─ main.py            FastAPI app, serves static/, /health, starts the analyzer when learning is on
├─ config.py          every path + setting  (the single source of truth)
├─ agent.py           the agent loop, the tools, the sandbox
├─ tokens.py          token accounting (exact when possible, estimate otherwise)
├─ metrics.py         per-session usage and savings
├─ providers.py       the capability registry: what exists, what it may do
├─ sandbox.py         command isolation using the platform's own primitives
├─ memory_store.py    budgeted persistent memory (SQLite with optional full-text search)
├─ memory_modes.py    how memory is chosen and injected, with a reserved budget
├─ learning.py        proposals as artifacts, approval, undo
├─ analyzer.py        the background session analyzer
├─ migrate.py         one-time import from a file you choose
├─ deps.py            optional dependency detection, installation never automatic
├─ audit.py           the append-only ledger
├─ plugin_manager.py  plugins: discovery, enable/disable, token impact
├─ mcp_client.py      MCP transports (stdio and streamable HTTP)
├─ mcp_registry.py    external servers: lifecycle, policy, audit, lazy activation
├─ anthropic.py       the native Messages API, and where cache breakpoints go
├─ profiles.py        named capability bundles
├─ project_context.py your project's own instruction files: found, indexed, budgeted
├─ skills.py          skills + knowledge, progressive disclosure
├─ plan.py            plan mode: decompose, explore, ask, draft
├─ research.py        multi-source research with citations
├─ browser.py         optional Playwright driver
├─ evidence.py        citable fact store
├─ model_settings.py  per-model temperature / max_tokens / context budget
├─ proctools.py       process identity, job containment, the orphan sweep
├─ extras.py          background processes, fetch, snapshots
├─ store.py           sessions, transcripts, cross-device registry
├─ session_state.py   live per-session turn state, keyed by sid
├─ turn_bus.py        one running turn's event buffer + fan-out; a mid-turn viewer is replayed
├─ turn_worker.py     one process per running turn, two-stage stop
├─ vcs.py             the version-control panel
├─ hosting.py         the repository hosting panel
└─ routers/           api, chat (WebSocket), mcp, memory, plugins, capabilities, profiles, hosting, vcs
```

The design rule is that a feature which enlarges the starting prompt must be optional and must show
its cost. It is applied throughout, and it is the reason the numbers above are what they are.

The agent works in a project you choose. Relative paths resolve there. There is no bundled sandbox
directory, and no assumption about where your projects are stored.

---

## Built-in tools

| Tools | Purpose |
|---|---|
| `list_files` `read_file` `write_file` `edit_file` `glob_files` `grep_files` | files |
| `run_shell`, `bg_start` `bg_output` `bg_stop` | commands, foreground and background |
| `task` | delegate to a sub-agent with a separate context |
| `skill`, `research`, `fetch`, `browser` | knowledge on demand |
| `mcp_search_tools` `mcp_activate_tools` `mcp_call` `mcp_list_servers` | external tools without the prompt cost; offered only when an MCP server is configured |
| `evidence` | citable facts |
| `memory_recall` `memory_add` `memory_update` `memory_delete` `memory_list_summary` | memory, only when the vault is enabled |

Version control is off for the agent by default. A model left to itself commits and pushes far more
than anyone asked for, and cleaning that up by hand is nobody's idea of a good afternoon. The panel
is the intended route for the person using Tacit. If you would rather the agent handled it,
**Settings > Tools** has a switch for exactly that, and it is yours to flip.

The learning analyzer and the migration importer deliberately have no tools at all. They are driven
from the interface, so they cost nothing in the schema budget and cannot be invoked by a model.

### How the file tools report themselves

A tool result is the only feedback the model gets, so it has to say where it stopped. Two rules
follow from that, and both exist because a session showed what happens without them:

- **`read_file` ends with the range it actually delivered**: `lines 211-334 of 658 shown; pass
  offset=334 for the next 324`: and that trailer survives the output limit. An `offset` past the end
  of the file is an error rather than an empty page. Reading a long file in chunks used to leave the
  model guessing where the cut fell, and it answered by re-reading the same span five times.
- **`grep_files` searches what it is pointed at.** A file path searches that file; a directory path
  searches that tree; a path that does not exist is an error. It used to fall back to the parent
  directory for a file path, so grepping one module returned matches from its neighbours: and a
  mistyped path silently searched somewhere else. Both read as real answers.

Shell and search results are clipped to `TACIT_TOOL_OUTPUT_LIMIT`. `read_file` has its own,
much larger `TACIT_READ_OUTPUT_LIMIT`, because a file is the one result the agent may have
to reproduce whole: at 6,000 characters a 17,578-character source file was two thirds
invisible, and the agent stopped reading it and re-derived the same facts through shell
probes instead: which is how a task needing one large write ended as thirty thin rounds.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TACIT_PORT` | `8550` | web host port |
| `TACIT_HOME` | `~/.tacit` | all state |
| `TACIT_HOST` | `0.0.0.0` | bind address |
| `TACIT_PROJECTS_ROOTS` | none | folders offered by the workspace picker; Tacit does not guess |
| `TACIT_PLAYWRIGHT_PATH` | auto-detected | an existing playwright installation |
| `TACIT_MAX_STEPS` | `24` | tool steps per turn |
| `TACIT_SUBAGENT_STEPS` | `12` | steps per sub-agent |
| `TACIT_COMPACT_AT` | `0` (reserve model) | set a flat fraction of the window instead |
| `TACIT_CONTEXT_RESERVE` | `33000` | tokens left free for the next turn's work |
| `TACIT_CONTEXT_FILL_FLOOR` | `0.5` | never compact below this fraction, for small windows |
| `TACIT_COMPACT_TAIL_TOKENS` | `8000` | recent transcript kept verbatim through a compaction |
| `TACIT_COMPACT_TAIL_SHARE` | `0.4` | cap on that tail as a share of the fill target |
| `TACIT_COMPACT_TAIL_MIN` / `_MAX` | `4` / `40` | message bounds on the tail |
| `TACIT_COMPACT_PRUNE_KEEP` | `400` | head of an old tool result kept by the pre-pass |
| `TACIT_COMPACT_MODEL` | none | a cheap model to write summaries with |
| `TACIT_SUMMARY_INPUT_MAX` | `160000` | cap on what the summariser is sent |
| `TACIT_CONTEXT_BUDGET` | `120000` | characters of transcript sent when the model's window is unknown |
| `TACIT_CONTEXT_BUDGET_MIN` | `24000` | floor for the derived cap, so a small window is still usable |
| `TACIT_CONTEXT_BUDGET_MAX` | `2000000` | ceiling for the derived cap, so a huge window is not filled |
| `TACIT_COMPACT_MID_TURN` | `1` | compact a growing turn in place rather than only between turns |
| `TACIT_COST_WARN` | `0.25,0.5,0.75` | fractions of the window at which a turn warns you what it is costing |
| `TACIT_PROMPT_CACHE` | `auto` | `auto` marks only endpoints known to understand it; `on` forces; `off` never |
| `TACIT_ANTHROPIC_MAX_TOKENS` | `8192` | default output ceiling; the Messages API requires one |
| `TACIT_ANTHROPIC_THINKING` | `4096` | base extended-thinking budget, scaled by the level picked |
| `TACIT_TURN_TOKEN_BUDGET` | `0` | hard token ceiling per turn; `0` means no ceiling |
| `TACIT_ANTHROPIC_MAX_TOKENS` | `8192` | default output ceiling for the Messages API, which requires one |

Container isolation is configured in **Settings > Capabilities** rather than by environment, since it
is a capability choice: the image (default `python:3.12-slim`), whether a missing image may be pulled
(default no), and the PID ceiling (default 256).

**Settings > General** also has **Restart server**: it stops this process and starts a fresh one on
the same port. A detached watcher waits for the port to free, then starts the server once and blocks
on it; the exit itself goes through uvicorn's own shutdown, so the analyzer, the MCP servers and the
pid file are cleaned up. The browser reconnects on its own. A turn that is running is cut off, and
the button says so before you press it. The restart is recorded in the audit ledger as
`server_restart`. Every process Tacit spawns — turn workers, the restart watcher, the server itself —
carries a `tacit-*` marker in its command line, a `TACIT_ROLE`/`TACIT_SESSION_ID` pair in its
environment, and a row in the live process registry (`GET /api/processes`, shown in **Settings >
General**); on Windows each child is also bound to a kill-on-close job object, so a crashed parent
takes its children with it. At startup the server sweeps the registry: a dead pid is dropped, and a
live pid whose command line still proves it is Tacit's is killed as an orphan from a previous crash.
Nothing is ever matched on the executable name.
| `TACIT_TOOL_OUTPUT_LIMIT` | `6000` | characters kept from a shell or search result |
| `TACIT_READ_OUTPUT_LIMIT` | `48000` | characters kept from `read_file` |
| `TACIT_SUBAGENT_RESULT_LIMIT` | `4000` | characters of a sub-agent report kept in the parent's window |
| `TACIT_SHELL_TIMEOUT` | `180` | seconds a shell command may run; enforced by killing the process tree |
| `TACIT_TEMPERATURE` | `0.2` | sampling temperature |

---

## Staying current

**Settings > General** holds the auto-updater. Tacit is an open-source project with no releases:
every push to the default branch is an update, and the updater's job is to make applying one a
single decision you can see the shape of first.

- **The check is one small GET.** The updater reads the `VERSION` file at the repository root and
  compares it with the same file upstream. Same version, nothing else is fetched — which is what
  keeps the hourly auto-check cheap.
- **The update is an archive copy, not a git operation.** When the versions differ, the branch
  tarball is downloaded and unpacked over the checkout: changed files are overwritten, new files are
  created, and the result names what it did. No git, no tokens, no API. An install that was made
  from an archive updates the same way it was installed, and a checkout that happens to be a git
  repository is left entirely alone — no fetch, no pull, no dirty-tree complaints.
- **User state is never touched.** The protected list — `models.json`, `prefs.json`, `mcp.json`,
  `sessions/`, `skills/`, `knowledge/` and the rest of what `~/.tacit` owns — is skipped on every
  path, and only tracked source extensions are considered.
- **A file upstream no longer has is reported, never deleted.** The archive cannot tell "upstream
  removed this" from "you added this", so the updater leaves it on disk and says so in the result.
- **Applying takes a restart.** The running server keeps its loaded code; the result says what was
  updated and the **Restart server** action above applies it.

The check runs on a timer you set — an hour by default — and can be switched off. Nothing is ever
downloaded or applied without the button.

---

## Security and privacy

- There is no telemetry, no analytics and no remote tracking anywhere in the codebase.
- **Binding is yours to choose.** `TACIT_HOST=127.0.0.1` keeps it on your machine; `0.0.0.0` puts it
  on the network for anyone you share it with, like any other local tool.
- **API keys** are stored in `~/.tacit/.env`, are masked in the interface, and are never written
  into a project or into a log.
- The agent's file tools refuse the key store.
- **The audit ledger masks secrets** before writing, at any depth, so a pasted token does not end up
  in a readable file.
- **Version control** is off for the agent unless you enable it in **Settings > Tools**.
- **External tools and plugins** run only after you enable them, and their activity is recorded in a
  readable audit log.
- **No feature calls out to another harness.** Not on start, not on a timer, not on a tool call.

---

## Benchmarked on OpenBench

This section is the evidence for the architecture above, not the argument itself. One model for
every row: GLM 5.2 on the fp8 endpoint, run by OpenBench's own runner with its `checker.sh` files
untouched. Three tasks, three trials each, one build, both operating systems. Tokens are uncached
input plus output, cache reads excluded, counted the way OpenBench counts them.

### Linux

| Profile | Solved | Tokens per solve | Wall seconds per cell |
|---|---|---|---|
| Default | 9/9 | 156,466 | 705 |
| Minimal | 9/9 | 133,881 | 648 |

Per task on Linux: feal 3/3 default and 3/3 minimal at 32,673 and 23,437 tokens per cell; llm 3/3 and 3/3 at 235,379 and 226,896; schemelike 3/3 and 3/3 at 201,348 and 151,312.

### Windows

| Profile | Solved | Tokens per solve | Wall seconds per cell |
|---|---|---|---|
| Default | 6/6 | 106,526 | 530 |
| Minimal | 5/6 | 82,814 | 418 |

Per task on Windows: feal 3/3 default and 3/3 minimal at 30,407 and 33,228 tokens per cell; llm
3/3 default and 2/3 minimal at 182,646 and 157,192. Schemelike is graded on Linux only so far:
its checker reads `os.O_NONBLOCK`, which Windows Python does not provide. Porting it is a two-hour
job and exactly the kind of contribution the list below asks for: it would come in with your
name on it.

### Against the published arms, same model, same three tasks

| Arm | Solved | Cells that errored |
|---|---|---|
| Tacit, Linux, default | 9/9 | 0 |
| Tacit, Linux, minimal | 9/9 | 0 |
| Pi | 8/9 | 3 |
| OpenCode | 7/9 | 3 |
| Claude | 5/9 | 5 |
| Grok | 5/9 | 6 |
| Codex | 4/9 | 5 |

The fifth column is the one the token table cannot show you. Every errored cell is a run where the
arm's own plumbing decided the outcome, and its tokens still got spent. Every published arm that
looks cheap in its row is partly charging you for failures it then discards.

### Run it yourself, that is the point of the numbers

These rows come from one model, one endpoint, one machine, twenty cells of it. That is a floor, not
a ceiling, and it is exactly why this section exists: the invitation is the remedy for the sample
size. The benchmark is meant to be run, not read: your model, your endpoint, your hardware, your
numbers.

- Everything needed ships with the runner: the tasks, the untouched checkers, and a results file
  format that takes any harness row.
- A three-trial run of one task lands before your coffee is cold. The full hard set is an
  afternoon.
- Send your rows back and your arm gets a line in the table with your name on it: a pull request,
  an issue with the jsonl attached, or the file in the discord show-off channel, whatever is
  easiest for you.

The cells this table wants next: any first-party or fp8 route for kimi and qwen, DeepSeek and
Qwen as the model, an Apple silicon lane, and a rerun of ours on whatever you run. Tuning is fair
game and wanted here too: the other arms ship tuned for the models they run, and Tacit's profiles
and prefs are the tuning surface, so tune for yours and send that row as well. More arms on the
same cells is what turns this from one box's word into a field.

### How the measurement holds up

- The runner and the checkers are the benchmark's own, byte for byte, and the agent never edits
  them.
- Every row carries the checker's output verbatim, a byte count and a sha256 for every file it
  graded, and a per-call audit ledger: which tools ran, on what, and what came back. A claimed
  solve is a transcript you can re-run.
- One build, one fingerprint, both operating system lanes. The profile rows are two tool bundles
  over the same cells, so the profile comparison is an experiment, not an anecdote.

---

## Status

Working and covered by tests: chat and agent turns against any OpenAI-compatible endpoint,
sub-agents, plan mode, skills, compaction, background processes, snapshots with comparison and
restore, the terminal, providers, sessions, token accounting and the dashboard, capability profiles,
per-session turn state with session isolation (one running turn per session, sid-scoped abort,
`GET /api/sessions/state`), the killable turn worker with its two-stage stop, the turn's event bus
with mid-turn replay (a viewer that arrives while a turn runs is caught up from the buffer, and a
socket that dies mid-turn takes nothing with it), MCP servers over stdio and HTTP with lazy tool
activation, the plugin system, the capability registry, standalone isolation, the memory vault with
enforced budgeting, the background session analyzer with approval-first proposals, one-time
migration from your own export, the self-update transport (a `VERSION` check and an archive copy,
no git required), and the per-session Assistant with its own model, thinking level and budgeted
read access to the session.

The repository includes an automated test suite: no network and no real model. The suite needs no
container daemon and no benchmark runner, and Tacit installs neither. Run it and see for yourself
how many there are — the count is whatever the suite says it is, and it is not maintained by hand.

```sh
python -m unittest discover -s tests -t .
```

| File | What it holds down |
|---|---|
| `test_platform.py` | tokens, plugins, MCP over a real subprocess, the memory vault, thinking levels |
| `test_capabilities.py` | the capability registry, isolation, profiles, learning, migration |
| `test_contracts.py` | the wire shapes the browser reads, and the shell guardrails |
| `test_claims.py` | each behaviour this README claims, including the agent loop end to end |
| `test_engine.py` | the provider stream protocol, the Anthropic transport, and a full turn over the WebSocket |
| `test_isolation.py` | the container backend and the timeout guarantee, with a stubbed runtime |
| `test_session_isolation.py` | sessions are standalone: one running turn per session, sid-scoped abort, over two real sockets |
| `test_turn_worker.py` | the turn worker against a real subprocess: spawn, event stream, cooperative cancel, tree kill, the thread escape hatch |
| `test_steer.py` | steering a running turn across the process boundary, and the worker registry's sid keying |
| `test_autoupdater.py` | the update transport against a stubbed HTTP seam: version check, archive apply, protected paths, dry run, a failed download changes nothing |

`test_claims.py` exists because a documented behaviour and the running program disagreed, and the
disagreement was only visible in a transcript: an agent re-reading the same file five times, a
sub-agent that had forgotten what it was asked, two dashboard rows that could not leave zero. Each
test there is named for the claim it enforces, so the next refactor cannot quietly drop one.

### Known limits

- **Binding.** `TACIT_HOST` is yours: `127.0.0.1` for your machine only, `0.0.0.0` to reach it
  from another device. There is no login system; bring your own if you put it on a network.
- **Windows has no isolation primitive.** `mechanism: none` is the answer there, and the
  timeout plus the change report are all that is enforced. A container is the way to get more.
- **Token counts are estimates without `tiktoken`.** Every figure is marked `~` when it is, and no
  number for another product is estimated at all.
- **A hung shell command used to cost far more than its timeout.** Fixed: the process tree is killed
  and the drain bounded. What remains is that the default is 180s, so a genuinely slow command still
  owns those three minutes; lower `TACIT_SHELL_TIMEOUT` if you would rather fail faster.
- **Container isolation needs a runtime you install yourself.** It is implemented and was checked by
  hand against a real daemon, but Tacit will not fetch Docker or an image for you, and on a machine
  without one the backend reports itself unavailable rather than quietly falling back to `none`.
  No live-daemon test is shipped, so those checks are not re-run on every commit.

## License

MIT. See [LICENSE](LICENSE).

# Tacit

**A standalone coding agent that shows you what it costs, and changes nothing without your approval.**

Tacit is a coding agent you run yourself and use in your browser. It reads your files, runs
commands, writes code, searches the web, and connects to outside tools. Everything stays on your
machine. There are no accounts, no telemetry, and no tracking of any kind.

The whole thing is one Python program serving plain web pages. There is no build step, no frontend
toolchain, and no database to install.

It is standalone in the strict sense: it never calls another harness, never reads another tool's
directories, and never requires one to be installed.

---

## The short version

Three properties decide what an agent costs you and what it can do to you. Tacit is built around all
three, and every number below is measured rather than asserted.

1. **The fixed cost per turn is the smallest here.** About 980 characters of standing instructions
   plus the tool descriptions for your profile. Roughly 710 tokens on the Minimal profile, about
   2,100 on the default. Compare with roughly 8,400 for DSH and 19,000 for Hermes.
2. **Nothing optional is on, and nothing learns without you.** Memory, plugins, external tools,
   sandboxing and autonomous learning are all off or proposal-only until you turn them on. An
   approved rule goes back through the same budgets as everything else, never around them.
3. **It reports what it actually enforces.** On Windows, Tacit says `mechanism: none` and enforces a
   timeout, because the base operating system offers no stronger primitive. It does not describe
   limits it cannot apply.

---

## How it compares

Measured on one machine, with one rule: characters divided by four. That is the fallback heuristic
Tacit uses when no exact tokenizer is installed, and it is the heuristic DSH documents for its own
token meter, so nothing is measured by a standard it does not already apply to itself.

| System | System prompt | Tools | Tool schemas | Fixed cost per turn |
|---|---|---|---|---|
| **Tacit, Minimal** | **979 chars** | **7** | **1.9 KB** | **~710 tokens** |
| Pi | 1,352 chars | 7 | 4.5 KB | ~1,600 |
| **Tacit, default profile** | **979 chars** | **24** | **7.2 KB** | **~2,100** |
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
| Tool schemas | **1,852 chars** | 4,626 chars |
| System prompt | **979 chars** | 1,352 chars |
| **Fixed cost per turn** | **~710 tokens** | **~1,600** |

Same tools, less than half the cost. Two things account for it. Pi's descriptions are longer: its
`read` tool takes the same three parameters as Tacit's and still costs 182 tokens against 67. And
Tacit's standing prompt is smaller to start with. Pi's `grep` does expose more options than Tacit's,
so that one is not a fair fight, but the rest are.

### What each difference actually buys

| Property | Tacit | DSH | Hermes Agent |
|---|---|---|---|
| Runtime dependency on another harness | **none** | plugin runtime | gateway ecosystem |
| Fixed cost per turn | **~710 to ~2,100** | ~8,390 | ~18,970 |
| Memory in the prompt | **off by default, hard token budget** | not applicable | accumulated in the system prompt |
| Learning that changes behaviour | **approval first, proposal state by default** | not applicable | automatic |
| Isolation on Windows | **reported honestly as unavailable** | not verified here | not applicable |
| Where state lives | **one folder you can delete** | its own | its own |

DSH's own documentation devotes a section to explaining why tool schemas are re-paid on every step.
That is the cost Tacit exists to make visible, and the profiles exist to let you move.

### Caveats, so the table stands up to a second look

These are fixed startup costs only. Nothing in this table speaks to output quality, speed, or
capability, and it is not a ranking of how good any of these tools are. Nothing here is badly built.

- **The Hermes figure includes accumulated state.** Its system prompt carries 9,994 characters of
  skills and 3,853 of memory. A fresh installation would be nearer 14,500 characters, about 12,900
  tokens. Still the heaviest here by a wide margin, but the difference is partly history rather than
  design.
- **The Pi figure is the softest.** Its prompt embeds per-tool snippet lines generated at runtime,
  which are estimated, and its parameter schemas are approximated. If anything, Pi is understated.
- **little-coder spends its tokens deliberately**, on write guards, output repair and per-turn skill
  cards, and it is tuned for models far smaller than the ones Tacit targets. Its Terminal-Bench
  result on a 35B model running on an 8 GB laptop is a good capability claim. It is not a token
  claim, and this table is not a capability comparison.
- **DSH and Hermes are measured, not judged.** I have not tested their isolation, their learning, or
  their reliability, and I make no claim about them beyond the prompt sizes above.

### Reproducing it

- **Tacit** is read from its own dashboard, in **Settings > Tokens**, and from the profile selector.
- **Pi** and **little-coder** are measured from the bundled JavaScript of the installed packages.
  `AGENTS.md` is little-coder's system prompt, replacing Pi's built-in one through
  `--system-prompt`, which is why the two rows are not additive.
- **DSH** is measured from its own persisted session logs under `~/.dsh/sessions`, which record the
  assembled system prompt and tool schemas for every request. Across 16 main-agent sessions the
  numbers were identical every time: 6,195 characters and 25 tools.
- **Hermes** is measured by its own `prompt-size` diagnostic:

```sh
python -c "from hermes_cli.prompt_size import compute_prompt_breakdown as f; print(f('cli'))"
```

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
  conversation the summary, not the reading.
- **Memory is capped and retrieved, not carried.** A fixed token budget, default 120, enforced. The
  rest is searched and injected only when relevant.
- **Older turns compact in place**, and a window cap decides what is sent.
- **Off by default.** Memory, plugins, external tool servers and sandboxing are disabled until you
  enable them, and can be disabled again at any time.

Nothing above is a claim about the model. It is about what Tacit chooses to put in front of it.

---

## Isolation, reported honestly

Tacit uses the strongest primitive the operating system actually offers, and says which one it is.
It does not describe a boundary it cannot enforce.

| Platform | Mechanism | What is enforced |
|---|---|---|
| Linux | `bubblewrap` | read-only or read-write project bind, private temp, network namespace, process isolation |
| macOS | `sandbox-exec` | no network, writes confined to the workspace and temp |
| Windows | none available | timeout and change reporting only |
| Any | container, opt-in | Docker or Podman, if you install one |

On Windows the answer is `mechanism: none`, and the interface says so in as many words. That is the
honest result: the base system offers no equivalent primitive, and a container runtime is the way to
get one. A container is never required and is never installed for you.

**The audit ledger.** Every tool call, sandbox decision, memory injection, learning proposal and
approval is appended to `~/.tacit/audit.jsonl`. It is a plain JSONL file you can read, search or
delete. Credentials are masked on the way in, at any depth. Nothing in it is ever edited or removed.

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

The default learning mode is `propose`, and it cannot change anything. `auto` and `auto-low-risk`
exist if you want them, and they are your decision to make, in **Settings > Capabilities**.

The analyzer adds no tool to the agent, changes no prompt text, and is switched off entirely when
learning is set to `learn-off`.

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
| **minimal** | 7 | none | off | propose | ~710 tokens |
| **silent** (default) | 24 | none | off | propose | ~2,100 tokens |
| **safe** | 24 | tacit-micro | explicit | propose | ~2,100 tokens |
| **power-isolation** | 24 | tacit-micro | explicit | propose | ~2,100 tokens |
| **power-memory** | 24 | none | full, budgeted | propose | ~2,100 tokens + budget |
| **full** | 24 | tacit-micro | full, budgeted | propose | ~2,100 tokens + budget |

No profile enables automatic learning. That is a deliberate choice: a profile sets cost and
containment, and does not decide what Tacit is allowed to remember on its own.

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
  only a summary, so the main conversation stays small.

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
  to paste context into it.
- **The agent cannot see it.** The Assistant is invisible to the main agent. Nothing it says reaches
  the agent unless you copy it across yourself, so your notes, hesitations and half-formed ideas
  stay between you and it.
- **It is confined to the session.** Every conversation has its own Assistant. Switch sessions and
  you switch assistants; delete a session and its assistant goes with it.

A second conversation is a second cost, so you decide exactly what it may read, and the price is
shown before anything is sent:

```
   ~182 tokens of session context
   you 20   persona 162   history 0   tools 0

   [x] Your prompts     [x] Agent replies     [x] Session info
   [ ] Read-only tools
   Last [20] turns
```

Read-only tools add roughly **1,450 tokens** on their own, which is why they are off by default. The
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
it needs:

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

Manage them in **Settings > Plugins**. Tacit ships with one:

| Plugin | Purpose | Default |
|---|---|---|
| **Memory Vault** | Persistent memory with a token budget | Off |

Anything else is a plugin you write or install yourself. There is no bundled plugin that reaches
outside your machine.

---

## Token dashboard

**Settings > Tokens** shows a local breakdown for the current session. It opens with the profile
selector described above.

- tokens sent and received
- the composition of the starting prompt: base instructions, built-in tools, external tool schemas,
  and memory
- the estimated full-context baseline, which is what the prompt would cost with everything loaded up
  front
- what Tacit actually sends
- the difference, attributed to on-demand loading, memory budgeting, and compaction

All figures are measured locally. Numbers are exact when a tokenizer is available and are marked as
estimates otherwise. No figure for another product is estimated or invented: the numbers in the
comparison table above were read from those products' own diagnostics and logs.

---

## Snapshots and undo

**Settings > Snapshots** shows a timeline of saved states. For each one you can:

- see when it was taken, its label, and the files it contains
- **compare** it with the project as it is now, listing changed, added and removed files
- **restore** it, after a confirmation prompt

The agent takes a snapshot before risky edits, and you can request one at any time.

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
├─ learning.json      learning proposals    (Settings > Capabilities)
├─ capabilities.json  capability selections
├─ audit.jsonl        append-only ledger of what happened
├─ mcp.json           external tool servers (Settings > MCP)
├─ plugins.json       plugin state          (Settings > Plugins)
├─ mcp_audit.jsonl    external tool activity log
├─ benchmarks.json    per-model temperature / max_tokens
├─ evidence.jsonl     recorded citable facts
├─ checkpoints/       project snapshots for undo
├─ plans/             approved plans
├─ sessions/          transcripts + metadata
└─ github.json        hosting panel state
```

Deleting `~/.tacit` removes all stored data.

---

## How it works

```
backend/
├─ main.py            FastAPI app, serves static/, /health, starts the analyzer
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
├─ profiles.py        named capability bundles
├─ skills.py          skills + knowledge, progressive disclosure
├─ plan.py            plan mode: decompose, explore, ask, draft
├─ research.py        multi-source research with citations
├─ browser.py         optional Playwright driver
├─ evidence.py        citable fact store
├─ benchmarks.py      per-model profiles
├─ extras.py          background processes, fetch, snapshots
├─ store.py           sessions, transcripts, cross-device registry
├─ vcs.py             the version-control panel
├─ hosting.py         the repository hosting panel
└─ routers/           api, chat (WebSocket), mcp, memory, plugins, capabilities, vcs
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
| `snapshot` `list_snapshots` `restore` | undo for the agent's own edits |
| `task` | delegate to a sub-agent with a separate context |
| `skill`, `research`, `fetch`, `browser` | knowledge on demand |
| `mcp_search_tools` `mcp_activate_tools` `mcp_call` `mcp_list_servers` | external tools without the prompt cost |
| `evidence`, `benchmark` | citable facts, per-model settings |
| `memory_recall` `memory_add` `memory_update` `memory_delete` `memory_list_summary` | memory, only when the vault is enabled |

Version control is off for the agent by default. A model left to itself commits and pushes far more
than anyone asked for, and cleaning that up by hand is nobody's idea of a good afternoon. The panel
is the intended route for the person using Tacit. If you would rather the agent handled it,
**Settings > Tools** has a switch for exactly that, and it is yours to flip.

The learning analyzer and the migration importer deliberately have no tools at all. They are driven
from the interface, so they cost nothing in the schema budget and cannot be invoked by a model.

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
| `TACIT_COMPACT_AT` | `0.65` | compact at this fraction of the window |
| `TACIT_TOOL_OUTPUT_LIMIT` | `6000` | characters kept from a tool result |
| `TACIT_SHELL_TIMEOUT` | `180` | seconds a shell command may run |
| `TACIT_TEMPERATURE` | `0.2` | sampling temperature |

---

## Security and privacy

- There is no telemetry, no analytics and no remote tracking anywhere in the codebase.
- **There is no authentication.** Tacit binds `0.0.0.0`, so anyone who can reach the port can drive
  the agent. Run it on a trusted network, or set `TACIT_HOST=127.0.0.1` to keep it local.
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

## Status

Working and covered by tests: chat and agent turns against any OpenAI-compatible endpoint,
sub-agents, plan mode, skills, compaction, background processes, snapshots with comparison and
restore, the terminal, providers, sessions, token accounting and the dashboard, capability profiles,
MCP servers over stdio and HTTP with lazy tool activation, the plugin system, the capability
registry, standalone isolation, the memory vault with enforced budgeting, the background session
analyzer with approval-first proposals, one-time migration from your own export, and the per-session
Assistant with its own model, thinking level and budgeted read access to the session.

The repository includes an automated test suite:

```sh
python -m unittest discover -s tests -t .
```

## License

MIT. See [LICENSE](LICENSE).

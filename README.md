# Tacit

**The Silent Harness.** A local-first coding agent that runs in your browser.

Tacit runs on your computer or on your local network. It reads files, runs commands, writes code,
searches the web, and connects to external tools. Your data stays on your machine, and no
information is sent to us.

The whole application is one Python program. The interface is plain web pages and needs no build
step.

---

## Why Tacit is different

AI models have a limited working memory: the amount of text they can consider at once. Many agent
tools fill that memory at the start with tool descriptions, plugin listings, and remembered facts,
whether or not any of it is relevant to the task. You pay for that on every message.

Tacit loads most capabilities only when they are needed, and shows what each one costs.

That matters because attention is the scarce resource. A model does not read your message in
isolation. It attends to everything in the prompt at once, and the fixed part is re-sent on every
turn. Instructions it never needs still compete with the question it is answering, and you are
billed for them each time. The usual way to make an agent more capable is to add more to that fixed
part. Tacit takes the position that the fixed part should stay small, that anything extra should be
opt-in, and that the number should be visible before you send it.

- **Small standing prompt.** About 980 characters, roughly 245 tokens. That is the complete set of
always-on instructions.
- **Capabilities load on demand.** Skills, tools and integrations are listed by name. Full details
are fetched only when they are used.
- **Visible cost.** A local dashboard shows what was sent, what it cost, and what was avoided.
Estimated figures are labelled as estimates.
- **Off by default.** Memory, plugins and external tool servers are disabled until you enable them,
and can be disabled again at any time.
- **Local data.** All state lives in one folder on your computer. There are no accounts, no
telemetry, no analytics, and no remote tracking.

---

## What a turn costs before you type

Every message carries a fixed overhead: the instructions the assistant always follows, plus the
descriptions of every tool it has been given. That overhead is present before your question is read,
and it is paid again on every turn.

Most tools do not show you this number. Tacit does, in **Settings > Tokens**.

| System | System prompt | Tools | Tool schemas | Estimated tokens |
|---|---|---|---|---|
| **Tacit, Minimal profile** | **979 chars** | **7** | **1.9 KB** | **~710** |
| Pi | 1,352 chars | 7 | 4.5 KB | ~1,600 |
| **Tacit, Default profile** | 979 chars | 24 | 7.2 KB | ~2,100 |
| little-coder | 7,747 chars | ~29 | 10.4 KB | ~4,600 |
| DSH | 6,195 chars | 25 | 26.7 KB | ~8,390 |
| Hermes Agent | 23,370 chars | 32 | 51.3 KB | ~18,970 |

The rows are ordered by cost, so the three systems that fit inside 2,200 tokens sit together at the
top. The interesting one is the first, because it is a like for like. Tacit's **Minimal** profile
leaves exactly the seven tools Pi ships with, read, write, edit, shell, grep, list and glob, and
nothing else:

| Same seven capabilities | Tacit | Pi |
|---|---|---|
| Tool schemas | **1,852 chars** | 4,626 chars |
| System prompt | **979 chars** | 1,352 chars |
| **Estimated tokens** | **~710** | **~1,600** |

Same tools, less than half the cost. Two things account for it. Pi's descriptions are longer: its
`read` tool has the same three parameters as Tacit's and still costs 182 tokens against 67. And
Tacit's standing prompt is smaller to begin with. Pi's `grep` does expose more options than Tacit's,
so that one is not a fair fight, but the rest are.

Every figure here is a measurement of a real installation, not an estimate of someone else's product,
and all of them use the same rule: characters divided by four. That is the heuristic Tacit falls back
to when no exact tokenizer is installed, and it is also the heuristic DSH documents for its own token
meter, so nothing is being measured by a standard it does not already apply to itself.

- **Tacit** is read from its own dashboard, and the same numbers appear in the profile selector in
Settings > Tokens.
- **Pi** and **little-coder** are measured from the bundled JavaScript of the installed packages.
`AGENTS.md` is little-coder's system prompt, replacing Pi's built-in one through `--system-prompt`,
which is why the two rows are not additive.
- **DSH** is measured from DSH's own persisted session logs under `~/.dsh/sessions`, which record the
assembled system prompt and tool schemas for every request. Across 16 main-agent sessions the numbers
were identical every time: 6,195 characters and 25 tools.
- **Hermes** is measured by its own `prompt-size` diagnostic. You can reproduce it:

```sh
python -c "from hermes_cli.prompt_size import compute_prompt_breakdown as f; print(f('cli'))"
```

Caveats, because a comparison is only worth anything if it is fair:

- The Hermes figure includes the skills and memory that installation has accumulated, 9,994 and 3,853
  characters, because Hermes keeps both inside the cached system prompt. Fresh, it would be nearer
  14,500 characters and about 12,900 tokens. Still the heaviest here by a wide margin.
- Pi's figure is the softest. Its prompt embeds per-tool snippet lines generated at runtime, so those
  are estimated, and its parameter schemas are approximated. If anything Pi is understated.
- little-coder spends its tokens deliberately, on write guards, output repair and per-turn skill
  cards, and it is tuned for models far smaller than the ones Tacit targets. Its Terminal-Bench
  result on a 35B model running on an 8 GB laptop is a capability claim, and a good one. It is not a
  token claim, and this table is not a capability comparison.
- These are fixed startup costs only. Nothing here speaks to speed, output quality or features, and it
  is not offered as a ranking of how good any of these tools are.

The point is not that anything here is badly built. It is that this cost is real, it is usually
hidden, and it is large enough to be worth showing. DSH's own documentation devotes a section to
explaining why tool schemas are "re-paid per step", which is exactly the kind of number a user should
be able to see before sending. Tacit exists to make that number visible and to give you the switches
to move it, which is what the profiles below are for.

---

## Profiles

A profile is a named bundle of the choices that decide the fixed cost, so you do not have to toggle
several things each time you change how you are working. Every profile shows its total prompt cost
before you apply it.

| Profile | What is on | Fixed prompt |
|---|---|---|
| **Minimal** | Seven core tools. No plugins, no memory, nothing else. | ~710 tokens |
| **Default** | All 24 built-in tools. No plugins, no memory. | ~2,100 tokens |
| **Assisted** | Default, plus memory at the default budget | ~2,500 tokens |
| **Full** | Assisted, plus every bundled plugin | ~2,700 tokens |
| **Everything** | Full, with MCP tools injected eagerly instead of on demand | ~2,700 tokens plus your MCP schemas |

Minimal is the one to reach for on a small model or a tight context window. It hands the agent
read, write, edit, shell, grep, list and glob, and switches off the other seventeen built-in tools
as well as every optional capability. You give up browsing, sub-agents, plan mode, research and
snapshots, and in exchange the standing prompt drops to about a third of its normal size.

Switching profiles applies immediately, and the numbers in the dashboard below the selector update
to match. You can also save the current configuration under your own name and delete it later.

The point is not the presets. It is that the cost of a configuration is knowable before you adopt
it, rather than discovered later in a bill or a truncated conversation.

---

## What you can do

- **Use any model.** OpenAI-compatible endpoints, local servers such as Ollama, llama.cpp or
LM Studio, or hosted providers. You can configure several at once.
- **Work on real projects.** Choose a folder, and Tacit reads, edits and runs commands there.
- **Undo changes.** Snapshots record your project before changes, with a timeline, a file-by-file
comparison, and one-click restore.
- **Connect external tools.** Tacit supports MCP, the open standard for connecting assistants to
tools. Tool descriptions stay out of your prompt until a tool is actually used.
- **Optional memory.** A memory vault stores preferences, decisions and project facts within a
token budget you set. It is disabled until you enable it.
- **Plan first.** Plan mode researches your project, asks clarifying questions, and drafts an
approach for you to approve before any files are changed.
- **Delegate.** A sub-agent can read a large amount of material in its own separate context and
return only a summary, so the main conversation stays small.

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

The installer downloads the source, creates a private virtual environment inside the checkout, and
writes a `tacit` launcher at `~/.local/bin/tacit` or `%LOCALAPPDATA%\Tacit\bin\tacit.cmd`.
Re-running it updates the checkout. Flags: `--no-browser` (or `TACIT_NO_BROWSER=1`) skips the
optional browser-automation step of about 130 MB, `--dir` chooses the checkout location, and
`--help` lists the rest.

## Requirements

- **Python 3.10 or newer** runs the entire backend.
- **Node 22 or newer** is optional and is only used for browser automation and the optional bridge
helper.

There is no database to install, no build pipeline, and no frontend toolchain.

## Run from source

```sh
python -m pip install -r requirements.txt
python -m backend.main
```

On Windows, double-click **`run.bat`**. On macOS or Linux, run `./run.sh`.

Open **http://localhost:8550**. On first start Tacit creates `~/.tacit`, which is empty. Add a
provider in **Settings > Providers** and your keys are stored in `~/.tacit/.env`. Nothing is
imported from elsewhere on the machine.

### Browser automation (optional)

The `browser` tool drives a real Chromium browser and is the only part that needs Node. Install
either of the following:

```sh
npm install                                   # uses the playwright declared in package.json
```
```sh
pip install playwright && playwright install chromium
```

Then restart. If neither is present, the tool reports this instead of failing silently. To reuse an
existing installation, set `TACIT_PLAYWRIGHT_PATH` to point at it.

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

### Using DSH plugins

DSH plugins are Cordis modules. They are not standalone programs: a plugin runs inside the DSH
process and calls into its services directly (`ctx.tools`, `ctx.fs`, `ctx.systemPrompt`,
`ctx.approval`, `ctx.sandboxPolicy`, `ctx.inject`). That shapes how they can be reused, and Tacit
covers both routes that actually work:

- **Anything that speaks MCP works directly.** MCP is the boundary tools agree on, which is why DSH
  ships an MCP client of its own. Import the server in Settings > MCP and its tools appear, with no
  rewriting.
- **Bundles that expose a tool table work through the optional Node bridge.** It loads the bundle,
  reads the tools it exports and serves them over MCP. Node is needed for this one step, and for
  nothing else in Tacit.

For a plugin that offers neither, Tacit writes an adapter: a small working MCP server plus a README
listing exactly what remains to be filled in, which is usually a short and obvious job.

The rule of thumb is **use MCP where a tool offers it, bridge what you can, adapt the rest**. A
Cordis plugin whose whole purpose is to call into a running DSH session needs DSH to run; that is a
consequence of how those plugins are built, and it is the reason MCP, not the plugin format, is the
thing worth supporting well.

Tacit reports which of your plugins work natively, which work through the bridge, and which need an
adapter, so nothing is claimed on your behalf that turns out not to be true.

---

## Memory

Persistent memory records things such as a preferred convention, a decision that was already made,
or how a project is structured. It can also consume a large part of a context window if it is not
limited.

The Memory Vault limits it:

- **Disabled by default.** Nothing is stored until you enable it.
- **A fixed budget.** Memory may use at most 120 tokens of your prompt by default, with a ceiling of
500 unless you change it. The limit is enforced.
- **Nothing is stored without approval.** Tacit can read a conversation and propose memories, but
each proposal is shown with its type, its confidence and its token cost before anything is saved.
You can approve, edit or reject it.
- **Every entry is editable.** You can edit, disable, unpin or delete any memory at any time, and
the prompt cost updates immediately.
- **Memories are searchable.** The agent retrieves a memory when it is relevant instead of carrying
all of them permanently.

The dashboard shows the current cost of memory and how much the budget avoided.

---

## Plugins

Plugins add tools, connections or panels. They are disabled by default, and each one reports the
tokens it would add to your prompt before you enable it. A plugin's code is loaded only after you
enable it.

Manage them in **Settings > Plugins**. Tacit ships with two:

| Plugin | Purpose | Default |
|---|---|---|
| **Memory Vault** | Persistent memory with a token budget | Off |
| **DSH Bridge** | Import and adapt DSH-style plugins | Off |

---

## Token dashboard

**Settings > Tokens** shows a local breakdown for the current session. It opens with the profile
selector described above.

- tokens sent and received
- the composition of the starting prompt: base instructions, built-in tools, external tool schemas,
and memory
- the estimated full-context baseline, which is what the prompt would cost with everything loaded
up front
- what Tacit actually sends
- the difference, attributed to on-demand loading, memory budgeting, and compaction

All figures are measured locally. Numbers are exact when a tokenizer is available and are marked as
estimates otherwise. No figures for other products are estimated or invented.

---

## Snapshots and undo

**Settings > Snapshots** shows a timeline of saved states. For each one you can:

- see when it was taken, its label, and the files it contains
- **compare** it with the project as it is now, listing changed, added and removed files
- **restore** it, after a confirmation prompt

The agent takes a snapshot before risky edits, and you can request one at any time.

---

## Where your data lives

Everything Tacit reads or writes is under **`~/.tacit`**. Nothing is written inside this repository
or into another tool's directory.

```
~/.tacit/
├─ models.json        providers + models      (Settings > Providers)
├─ .env               API keys
├─ prefs.json         preferences, defaults
├─ skills/            your skills      ─┐ listed by name, loaded on demand
├─ knowledge/         reference cards  ─┘
├─ memory.db          memory vault          (Settings > Memory, off by default)
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
├─ main.py            FastAPI app, serves static/, /health
├─ config.py          every path + setting  (the single source of truth)
├─ agent.py           the agent loop, the tools, the sandbox
├─ tokens.py          token accounting (exact when possible, estimate otherwise)
├─ metrics.py         per-session usage and savings
├─ plugin_manager.py  plugins: discovery, enable/disable, token impact
├─ mcp_client.py      MCP transports (stdio and streamable HTTP)
├─ mcp_registry.py    external servers: lifecycle, policy, audit, lazy activation
├─ memory_store.py    budgeted persistent memory (SQLite with optional full-text search)
├─ plugins/           bundled plugins (memory_vault, dsh_bridge)
├─ skills.py          skills + knowledge, progressive disclosure
├─ plan.py            plan mode: decompose, explore, ask, draft
├─ research.py        multi-source research with citations
├─ browser.py         optional Playwright driver
├─ evidence.py        citable fact store
├─ benchmarks.py      per-model profiles
├─ extras.py          background processes, fetch, snapshots
├─ store.py           sessions, transcripts, cross-device registry
├─ vcs.py             version-control bridge (the human's panel)
├─ hosting.py         repository hosting bridge
└─ routers/           api, chat (WebSocket), mcp, memory, plugins, dsh, vcs, hosting
```

The design rule is that a feature which enlarges the starting prompt must be optional and must
show its cost. It is applied throughout:

- The always-on prompt is about 980 characters.
- Skills and knowledge are indexed by name. Each entry costs roughly 84 characters up front instead
of its full text.
- External tool descriptions are discovered but not injected until activated.
- Memory is capped by a budget you set.
- The `task` tool runs a sub-agent in a separate context and returns only its report.
- Compaction summarises older turns in place, and `windowed()` caps what is sent.

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

Version control is not available to the model. The Git panel is the interface for the person using
Tacit, and `run_shell` refuses version-control binaries.

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
- The agent's file tools refuse the key store, and the shell tool refuses version control.
- **External tools and plugins** run only after you enable them, and their activity is recorded in
a readable audit log.

---

## Status

Working and covered by tests: chat and agent turns against any OpenAI-compatible endpoint,
sub-agents, plan mode, skills, compaction, background processes, snapshots with comparison and
restore, the terminal, providers, sessions, token accounting and the dashboard, capability profiles,
MCP servers over stdio and HTTP with lazy tool activation, the plugin system, the DSH bridge and
adapter scaffolding, and the memory vault with enforced budgeting.

The repository includes an automated test suite:

```sh
python -m unittest discover -s tests -t .
```

## License

MIT. See [LICENSE](LICENSE).

# Documentation

The README states what Tacit is and what it costs. These pages state how it works, where each
setting lives, and which line of code decides it. Every citation is a file and a line you can open.

| Page | What it answers |
|---|---|
| [Install](install.md) | getting it running, and what the installers do |
| [Interface](interface.md) | the three regions, snapshots, restart, sounds |
| [Settings](settings.md) | which tab owns which switch and which file it writes |
| [Configuration](configuration.md) | every environment variable, every preference key, and which one wins |
| [Tools](tools.md) | the 22 tools the agent is offered, and the four rules that decide whether a tool appears |
| [Architecture](architecture.md) | server, session runtime, turn worker, routers, the agent loop |
| [Cost and profiles](cost-and-profiles.md) | how a token figure is produced, and what each profile changes |
| [Isolation and audit](isolation-and-audit.md) | what a command was allowed to touch, and what it changed |
| [Memory and learning](memory-and-learning.md) | the four memory modes, the budget, and how a proposal becomes a rule |
| [MCP and plugins](mcp-and-plugins.md) | external tools without the prompt cost |
| [Data and privacy](data-and-privacy.md) | every file Tacit writes and what is in it |
| [Testing and benchmarks](testing-and-benchmarks.md) | how to run the suite and what the published figures came from |

Three rules hold across all of them.

**Nothing optional is on by default.** Memory, sandboxing and learning start off, and their defaults
are declared in one place, `backend/providers.py:22-30`. Plugins start disabled until you enable one
(`backend/plugin_manager.py:55`), and an external tool server neither auto-starts nor injects every
tool it offers (`backend/mcp_registry.py:39-45`). Two flags are on: the per-turn guidance blocks, with
the reason written next to them (`backend/providers.py:32-34`), and the analyzer's `enabled`
(`:31`), which `sync_worker` still gates on the learning mode, so with learning off the thread never
starts (`backend/analyzer.py:683-694`).

**Nothing is enforced that is not reported.** Where the platform cannot isolate a command, the
sandbox says so in the result rather than claiming a guarantee it does not have
(`backend/sandbox.py:21-27`).

**Nothing is learned without you.** A proposal is a stored artifact you read and decide on
(`backend/learning.py:3-19`), and the analyzer that finds candidates registers no tool and adds no
prompt text (`backend/analyzer.py:14-16`).

Start with [configuration](configuration.md) if you want to change something, and with
[architecture](architecture.md) if you want to change the code.

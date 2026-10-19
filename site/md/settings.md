# The settings map

Nine tabs, four of them visible until you ask for the rest. Every one of them writes a different file, which matters the moment you want to move your setup to another machine or work out why a change did not take.

| Tab | What it holds | What it writes |
|---|---|---|
| **General** | Profile selector, project roots, permissions, the fallback model, the Interface section with both sound switches, their audition button and transcript density, server, updater, Restart server | `prefs.json`, `.env`, and this browser's `localStorage` for density (`static/app.js:115-122`) |
| **Providers** | Endpoints, keys, per-model capabilities | `models.json`, `.env` |
| **Skills & Tools** | Skills, knowledge, the tool list with a cost figure per tool | `~/.tacit/skills/`, `~/.tacit/knowledge/`, `prefs.json` |
| **Appearance** | Theme, grouped by tone so a light theme can be picked without reading the dark ones | this browser's `localStorage` (`tacit.theme`), not the server (`static/harness.js:104-111`) |
| **Capabilities** | Sandbox backend, container image, PID ceiling, turn timeout | `capabilities.json` |
| **Learning** | The mode selector, the proposal queue, the analyzer | `learning.json` (`backend/config.py:59`) |
| **MCP** | Servers, allow and deny lists, Direct MCP, the audit log | `~/.tacit/mcp.json`, `mcp_audit.jsonl` |
| **Memory** | The mode switch, the token budget, every entry with its provenance | `memory.db` (`backend/config.py:60`), budget in `prefs.json` |
| **Plugins** | Each plugin, what it contributes, what it costs, enabled or not | `plugins.json` |

The last five are hidden behind the `advanced` checkbox (`static/platform.js:51-72`). Nothing else about them changes: a hidden tab is still the only place to edit the thing it owns.

## Where each default lives

Five of the optional systems default to off, and the code is the source of truth:

| System | Default off | In |
|---|---|---|
| Version control tools | `allowVersionControl` absent from `prefs.json` | `backend/config.py:496` |
| Plugins | empty `enabled` list | `backend/plugin_manager.py:55` |
| Learning | `mode = "learn-off"` | `backend/providers.py:30` |
| Memory | `mode = "off"`, budget 120 | `backend/providers.py:26` |
| MCP | `enabled` false on every added server | `backend/mcp_registry.py:93` |

Cost and containment are separate switches. Turning plugins on does not turn learning on, and a profile that moves the sandbox does not touch either.

## General

The profile selector is here, not on a tab of its own. It shows each profile's measured starting cost against your live configuration, and applying one writes the tools, sandbox, memory and learning choices it carries. "Save current as a profile" takes what you have configured and names it.

Project roots decide which folders the workspace picker offers. Tacit does not guess where your projects live, so the list starts empty: `TACIT_PROJECTS_ROOTS`, or `projectRoots` in `prefs.json`.

Turn budget sets how long one reply runs: tool steps for the main loop, for a sub-agent, and for a
research aspect, plus a token ceiling for the whole turn. The ceilings exist to keep a runaway turn
from filling the context with tool results nobody asked for, so they are limits, not targets. Each
field shows the default under the box; leaving a field empty puts the default back. A number the
environment already set (`TACIT_MAX_STEPS`, `TACIT_SUBAGENT_STEPS`, `TACIT_RESEARCH_STEPS`,
`TACIT_TURN_TOKEN_BUDGET`) is greyed out and carries a note saying so, because a control that looks
editable and then ignores you is worse than one that is missing.

**Fallback model** is a provider and a model, or nothing at all, and nothing is the default
(`static/platform.js:241-275`). It exists because `resolve_model` used to answer a ref this machine
no longer has with the first model in the registry: a session pointed at a removed provider ran on
something else, at that model's cost, quality and context window, and the transcript said nothing
about it. That substitution is gone. Now a session whose model is missing runs on the model you
chose here, and with no choice it stops and names what it could not find
(`backend/ai/engine.py:42-57`). The value is saved as `fallbackModel` in `prefs.json`
(`backend/routers/api.py:852-866`) and read by `backend/config.py:371-384`, where
`TACIT_FALLBACK_MODEL` outranks it. A ref that is stored but no longer in the registry is still shown,
labelled `not in the registry`, because a picker that reports only what would resolve keeps the broken
value invisible (`static/platform.js:247-253`). The compaction summariser is not nominatable and has
no hidden backup either: it writes the summary on the session's own model.

Permissions lists what the agent may do without asking, including the version control switch. Server shows the host and port and holds the restart. The updater checks `VERSION` against the repository and, when you press it, replaces the checkout and runs `pip install -r requirements.txt` again. It never runs `npm install` and never pulls a browser build: those stay yours to decide.

## Providers

Endpoints and keys. A key goes to `~/.tacit/.env`, is masked in the interface, and the agent's own file tools refuse that path. `models.json` holds the base URL, the model list and the per-model numbers: `temperature`, `maxTokens`, `contextWindow`. Those three are what the compactor and the cost meter read; Tacit does not go looking them up over the network, and `backend/model_settings.py` says so out loud.

The Assistant can be pointed at a different provider from the main agent, including a cheap local one, because it is a separate call with its own cost.

## Capabilities

The sandbox backend: `none`, `tacit-micro`, or `container`. The container fields are the image (default `python:3.12-slim`), whether to pull (default: no, so a run never depends on a registry being reachable), and the PID ceiling (default 256). See [Isolation and audit](isolation-and-audit.md) for what each backend actually enforces.

## Learning

Four modes: `learn-off` (default), `propose`, `auto`, `auto-low-risk`. The queue holds proposals with the exact repeated sentence, the session and turn it came from, and an edit field. Approving is what makes a rule a rule.

The analyzer runs on a daemon thread named `tacit-analyzer` (`backend/analyzer.py:723`), off the request path, and `sync_worker` starts it only while the learning mode needs it (`backend/analyzer.py:683-694`). It is pattern-matching, not a model call: no API spend, nothing sent anywhere. The model pass is a separate module, `backend/proposal_pass.py`, off until a person turns it on (`backend/analyzer.py:8-13`).

## Memory

One switch, four modes: `off` (default), `explicit`, `jit`, `full` (`backend/memory_modes.py:7-13`). The budget is a hard cap on the rendered block, default 120 tokens, and an entry that does not fit is deferred rather than silently dropped. Every entry carries its provenance and is editable, and a pinned entry survives a prune.

`memory_recall` goes through `memory_modes.recall_for` rather than around it, which is what makes a hit count as used and push its expiry out (`backend/agent.py:426-436`). Under `explicit` and `off` it answers with the rule that applied instead of an empty result the model would read as "no memory" (`backend/agent.py:438-444`), and each retrieval is audited as `memory_retrieval` (`backend/agent.py:445-447`).

## MCP

Add a server by name and command (`npx`, `uvx`, `python -m`, or any path) or by HTTP URL. New servers are added **disabled**, and a disabled server costs nothing because its tools are not in the prompt.

Two lists per server, allow and deny, and they are checked when a tool is *called*, not only when it is offered. A tool with a destructive-looking name asks for confirmation the first time. The audit log records every call with the arguments masked.

**Direct MCP mode**, off by default, puts a server's tools in the prompt as ordinary tool schemas instead of leaving them behind the four discovery helpers.

## Plugins

Each plugin shows what it contributes and what that costs, enabled or not. A plugin you have switched off is still priced, because the cost of a plugin you might switch on is the number you need before you decide.

## Appearance

Theme and transcript density. Density changes how much of a tool result sits in the transcript before you expand it; it does not change what the agent sees.

Next: [every knob, with its default](configuration.md).

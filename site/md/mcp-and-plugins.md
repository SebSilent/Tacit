# MCP and plugins

Both are extension points, and both follow the same rule: nothing a plugin or a server contributes
reaches the model until you turn it on, and the cost of turning it on is shown before you do.

## MCP is policy, and the client is not

`backend/mcp_client.py` only speaks protocol, written on the standard library rather than pulling in
the official SDK (`backend/mcp_client.py:3`). Policy lives in `backend/mcp_registry.py`: which servers
may run, which tools may be called, what gets injected into the prompt, and what gets written to the
audit log (`backend/mcp_registry.py:3-5`).

The context rule is the whole design. A connected server's tools are discovered and stored, never
auto-injected: the agent sees only the four compact helper tools until it searches for something and
explicitly activates it for a bounded number of turns (`backend/mcp_registry.py:7-11`).

| File | What it holds |
|---|---|
| `~/.tacit/mcp.json` | servers, allow and deny lists, settings (`backend/config.py:54`, read at `backend/mcp_registry.py:50`, written at `:61`) |
| `~/.tacit/mcp_audit.jsonl` | every external call, arguments redacted, capped at 500 rows (`backend/mcp_registry.py:14`, `:33`) |

## The four helpers

`mcp_list_servers`, `mcp_search_tools`, `mcp_activate_tools` and `mcp_call`
(`backend/agent.py:717-729`, handlers at `:542`, `:558`, `:571`, `:585`). They appear only when a server
is configured, because with nothing in `mcp.json` they are four dead ends that cost tokens and invite
calls that can only fail (`backend/agent.py:884-892`). Presence is the gate rather than connectivity: a
configured but stopped server still gets them, because listing and searching are how the model learns
what it could start.

`mcp_call` is the cheap path. It costs no schema tokens, and a destructive tool requires `confirm=true`
(`backend/agent.py:725-728`). Whether a tool looks destructive is a word list, not a guess: `delete`,
`remove`, `drop`, `destroy`, `kill`, `exec`, `shell`, `write`, `create`, `push`, `deploy`, `publish`,
`rm `, `unlink`, `truncate`, `reset`, `overwrite`, `send`, `post`, `install`
(`backend/mcp_registry.py:35-37`).

## Activation and its clock

Activated tool keys live in memory with an expiry measured in turns
(`backend/mcp_registry.py:31`), and the default lifetime is three turns
(`backend/mcp_registry.py:41`). When a key expires it is dropped and the event is audited as
`tool_deactivated` with the reason `ttl expired` (`backend/mcp_registry.py:419-423`).

What is injected at any moment is the activated set plus the pinned set
(`backend/mcp_registry.py:426-433`). Pinning is a per-tool decision made in **Settings > MCP** and it is
audited as `tool_pinned` or `tool_unpinned` (`backend/mcp_registry.py:611-624`).

`injection_report` is the number that matters for cost: it prices every discovered schema and subtracts
what is actually injected, which is what the dashboard's "saved by lazy activation" counter reads
(`backend/mcp_registry.py:436-448`, `backend/metrics.py:21-22`).

**Direct MCP mode** is the escape hatch for the classic behavior of dumping every tool into the prompt.
It is off by default (`backend/mcp_registry.py:39-40`), and a profile carries it as a choice
(`backend/profiles.py:296`).

## Server settings

A server is normalised on load: `enabled` defaults to false, transport to `stdio`, `allow_tools` and
`deny_tools` to empty lists, and `require_confirmation` to true (`backend/mcp_registry.py:87-98`).
Transport is `stdio`, `http`, `https`, `sse` or either spelling of `streamable`
(`backend/mcp_registry.py:29`). Servers are not started until asked, and a crashed server is restarted
at most three times (`backend/mcp_registry.py:42-43`).

The allow and deny lists are checked when a tool is *called*, not only when it is offered, which is why
`mcp_call` is removed from read-only callers entirely: a model can hand itself `confirm=true`
(`backend/agent.py:624-630`).

## Plugins

Three rules shape the manager (`backend/plugin_manager.py:4-13`):

- **Disabled unless enabled.** Nothing a plugin provides reaches the agent until you turn it on.
- **No silent execution.** A plugin's code is imported only when it is enabled. Bundled plugins in
  `backend/plugins/` are ours and are read directly; a *user* plugin must ship a `manifest.json` so it
  can be listed without running it.
- **Visible cost.** Every plugin reports the tokens it adds to the startup prompt, and a plugin that
  would inflate the always-on prompt must say so.

A plugin contributes by defining `tools()` for schemas, `call(name, args, ctx)` to dispatch them, and
optionally `mcp_servers()` to register external servers (`backend/plugin_manager.py:15-17`). The
required manifest fields are `id`, `name` and `description` (`:39`). State is `~/.tacit/plugins.json`
(`backend/config.py:52`, read at `backend/plugin_manager.py:52`, written at `:65`), and user plugins live
under `~/.tacit/plugins/` (`backend/config.py:53`). Each plugin keeps a log capped at 200 entries
(`backend/plugin_manager.py:37`).

Plugin tools are appended to the registry at request time, never baked into the base list, and a failure
there must never break an ordinary turn (`backend/agent.py:914-921`).

### The one bundled plugin

`task_list` is the only plugin shipped (`backend/plugins/task_list.py:25-37`). A long turn holds its task
list in the transcript, which breaks two ways at once: every step re-pays for it, and a compaction can
summarise it into prose the model then has to re-derive (`:3-6`). The list therefore lives on the session
rather than in the messages, so it survives compaction by construction instead of by being summarised
well (`:14-15`), bounded at 40 tasks with a 400-token injected budget
(`:39-40`).

It is a plugin rather than a built-in tool precisely because it enlarges the starting prompt, and the rule
for such a feature is that it must be optional and must show its cost. It is off by default, the panel
reports the schema tokens before you enable it, and the block it injects appears only once there is
something in it, so an unused task list costs nothing (`:8-12`).

### Retired plugins

`RETIRED` is `{ "memory_vault" }`, and discovery skips those ids entirely
(`backend/plugin_manager.py:41-47`). The module may still be on disk in an existing install and a person
may still have it listed as enabled, so a retired feature cannot come back as a row in Settings, a tool in
the prompt, or a line in a cost estimate. Memory moved into the backend, where one switch decides both
what is injected and what the agent may call (`backend/plugin_manager.py:45-46`,
`README.md:729-732`).

## What a profile does to both

Applying a profile enables every plugin in its bundle and disables every other known plugin
(`backend/profiles.py:287-293`), and sets MCP direct mode (`backend/profiles.py:296`). A plugin's
contribution to the profile's price is measured from its real schemas rather than from the number it
declares (`backend/profiles.py:119-121`).

Next: [testing and benchmarks](testing-and-benchmarks.md).

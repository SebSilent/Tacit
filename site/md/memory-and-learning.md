# Memory and learning

Memory is a store the agent may read. Learning is a candidate rule the agent may propose. Neither one
changes a prompt until you say so, and both are off by default (`backend/providers.py:26`, `:30`).

## The four memory modes

`MODES` is `off`, `explicit`, `jit`, `full` (`backend/memory_modes.py:23`), and an unset or unknown
value means `off` (`:32-35`).

| Mode | What is injected | What is retrieved mid-turn |
|---|---|---|
| `off` | nothing persistent | nothing |
| `explicit` | only what you wrote or approved | nothing, by design |
| `jit` | the always-relevant items | the smallest relevant set per turn |
| `full` | every approved note competes for the startup block | yes |

The descriptions are the module's own (`backend/memory_modes.py:7-16`), and the rule is the same in all
four: an item that is injected can be pointed at, explained, and measured (`:15-16`).

`RETRIEVAL_MODES` is `jit` and `full` (`backend/memory_modes.py:29`). That set is why the tool list
moves: under `explicit` a recall tool could only ever answer "retrieval is not enabled", which is a dead
end with a schema attached, so it is not offered (`backend/memory_modes.py:26-28`,
`backend/agent.py:901-910`).

Memory is not a plugin and has not been for some time. It used to be, and the plugin's switch decided
what the agent could call while the memory mode decided what was injected, so the two could disagree
and the dashboard could report a cost for a setting nobody had changed
(`backend/plugin_manager.py:41-47`, the id is retired there). Now one
switch in **Settings > Memory** decides both.

## The store

One SQLite file at `~/.tacit/memory.db` (`backend/config.py:60`, opened at
`backend/memory_store.py:92`). Four design rules, in the module's own order
(`backend/memory_store.py:5-14`):

1. **Never a token furnace.** Nothing is injected at startup unless it is explicitly enabled and fits
   the budget, and the budget is small on purpose: 120 tokens by default, 500 hard maximum
   (`backend/memory_store.py:30-31`).
2. **Inspectable.** Every record carries type, scope, source, confidence and its own token estimate, so
   the interface can show exactly what a memory costs (`backend/memory_store.py:36-51`).
3. **Revocable.** Disable, unpin, edit or delete any record at any time; the startup block recomputes
   immediately.
4. **Local.** Full-text search when the SQLite build has FTS5, keyword search when it does not, and no
   vector database (`backend/memory_store.py:12-14`). Whether the database can do FTS is asked per call
   rather than cached, because caching it globally broke every other database the process touched
   (`backend/memory_store.py:122-133`).

The vocabularies are fixed lists: types `preference`, `project_fact`, `decision`, `lesson`, `pattern`,
`contact`, `other`; scopes `global`, `project`, `language`, `repository`; confidence `low`, `medium`,
`high`; sources `user`, `agent_suggestion`, `session_extract` (`backend/memory_store.py:25-28`). Those
source values are the provenance: you can always see whether a note came from you, from a proposal the
agent made, or from a session extract.

Each row also tracks use: `created_at`, `updated_at`, `last_used_at`, `use_count` and an expiry, and a
retrieval hit bumps them through `reinforce` (`backend/memory_store.py:46-49`, `:67`, `:197-208`).

## How the startup block is chosen

Priority is pinned, then confidence, then use count, then recency (`backend/memory_store.py:366`), and
the *rendered* block is what the budget caps, not the pool that was considered
(`backend/memory_store.py:366-375`). In `jit` and `full` half the budget is held back for mid-turn
retrieval (`backend/memory_modes.py:25`). An injected item is wrapped with the reason it was injected
(`backend/memory_modes.py:59-61`), which is what makes "why is this in my prompt" answerable.

When the agent calls `memory_recall` the request goes through the mode rather than around it, which is
what makes a hit count as used and push its expiry out (`backend/agent.py:426-436`). Under `explicit`
and `off` the tool answers with the rule that applied instead of an empty result the model would read as
"no memory" (`backend/agent.py:438-444`), and every retrieval is audited with its mode, count, token
cost and a truncated query (`backend/agent.py:445-447`).

## Proposing a memory

`memory_add` does not write to the prompt. It creates a record whose source is `agent_suggestion` and
which is injected nowhere until you approve it (`backend/agent.py:738-743`). The review lives in the
memory panel, and the budget applies after approval, not before.

## Learning, and where a rule can go

Tacit may notice a rule worth keeping. It may not apply one on its own unless you have said so, and even
then only within limits, so a proposal is an artifact: a stored record with its own state that you can
read, accept, edit, reject, disable or delete (`backend/learning.py:3-6`).

Four modes: `learn-off` (the default), `propose`, `auto-low-risk`, `auto`
(`backend/providers.py:107-117`, default at `:30`). `auto-low-risk` applies a proposal on its own only
when it is a preference, it reads as low risk, and it is at least medium confidence
(`backend/providers.py:111-114`).

The kinds are `skill`, `rule`, `correction`, `preference`, and the states are `proposed`, `approved`,
`rejected`, `disabled` (`backend/learning.py:32-33`). The store is bounded at 200 artifacts with a
4,000-character body cap (`:38-39`), which is why the queue cannot grow without limit.

When a proposal is approved it is not written into the prompt. It goes to one of the two places that
already exist and are already budgeted: a skill file indexed by name and read on demand, or memory under
the memory budget (`backend/learning.py:12-19`). That is the point of the design. Learning must not be a
back door around context discipline, so it borrows the front doors.

## The analyzer

The analyzer reads finished transcripts on a timer and leaves proposals behind. It is never on the
request path and it cannot change an answer by itself (`backend/main.py:39-46`,
`backend/analyzer.py:17-18`).

It runs on a daemon thread named `tacit-analyzer` (`backend/analyzer.py:716`, `:723`), and `sync_worker`
starts or stops it according to the learning mode, so a learning-off install does not even have the
worker (`backend/analyzer.py:683-694`, `backend/main.py:41-46`).

Heuristics first, always. A regex pass over the user's own turns costs nothing and is the only thing
that runs by default. The model pass is a separate module, `backend/proposal_pass.py`, off until a person
turns it on, and its findings go through the same gates the heuristic pass uses, so a confident
hallucination is dropped for the same reason a stray sentence is (`backend/analyzer.py:8-13`).

Nothing here touches the agent: no tool is registered, no prompt text is added, no schema is injected
(`backend/analyzer.py:14-16`). Every proposal is deduplicated, so a repeated correction does not become
fifty proposals; a second sighting raises confidence instead (`backend/analyzer.py:19-20`). The output
carries its provenance, which means you can see the exact sentence that caused it: autonomous in
finding, never in deciding (`backend/analyzer.py:22-24`).

## Skills and knowledge

Skills are markdown files with optional front matter, read from `~/.tacit/skills` and
`~/.tacit/knowledge` (`backend/config.py:41-42`, loaded at `backend/skills.py:21-36`). The front matter
supplies a name and a description; without it the file's stem and its first non-empty line are used
(`backend/skills.py:6-18`, `:33-34`).

Only the index goes into the prompt, capped at 24 entries (`backend/config.py:203`), and the body is
fetched by the `skill` tool when the model actually wants it (`backend/agent.py:685-686`, `:510`). That
is the lazy-loading rule the cost figures depend on.

Next: [MCP and plugins](mcp-and-plugins.md).

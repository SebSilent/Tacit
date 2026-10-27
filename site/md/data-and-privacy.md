# Data and privacy

Everything Tacit knows about you is in one directory, and the whole product is a Python program
serving plain pages over it. There are no accounts, no telemetry and no tracking
(`README.md:5-7`). Nothing leaves the machine except the requests you send to the model provider you
configured.

## Where it lives

`TACIT_USER_HOME` decides where the state directory is looked for, and `TACIT_HOME` moves the state
directory itself (`backend/config.py:29-30`). The defaults are your home and `~/.tacit`.

| Path | Declared at | What is in it |
|---|---|---|
| `sessions/index.json` | `backend/config.py:36` | one row per session: id, title, model, mode, thinking, project, created, updated |
| `sessions/<id>.json` | `backend/store.py:20` | the full transcript of that session |
| `sessions/deleted.json` | `backend/store.py:342` | the ids you have deleted, newest last, capped at 400 (`backend/store.py:335`). A delete has to outlive the copies of the list that are still holding it: a browser's `localStorage`, and any process that reopens the store. `create()` refuses an id on this list (`backend/store.py:53`) and the registry hands it to the page (`backend/store.py:317-322`) so a stale copy is dropped rather than created back |
| `prefs.json` | `backend/config.py:34` | interface and behaviour preferences, listed in [configuration](configuration.md) |
| `models.json` | `backend/config.py:32` | providers, base URLs, model list, the default ref |
| `.env` | `backend/config.py:33` | API keys |
| `capabilities.json` | `backend/config.py:57` | the sandbox, memory and learning selections |
| `profiles.json` | `backend/config.py:55` | your saved profiles and which is active |
| `mcp.json` | `backend/config.py:54` | MCP servers, allow and deny lists |
| `mcp_audit.jsonl` | written at `backend/mcp_registry.py:118`, read at `:132-134` | every external tool call; arguments are summarized to 160 characters (`:124-129`) and an `env` field is redacted (`:114-115`) |
| `plugins.json` | `backend/config.py:52` | which plugins are enabled |
| `plugins/` | `backend/config.py:53` | user-installed plugins |
| `learning.json` | `backend/config.py:59` | proposals as artifacts, bounded at 200 rows (`backend/learning.py:38`) |
| `memory.db` | `backend/config.py:60` | durable memory, one SQLite file |
| `skills/` and `knowledge/` | `backend/config.py:41-42` | markdown you or Tacit wrote, read on demand |
| `evidence.jsonl` | `backend/config.py:43` | citable facts recorded during turns |
| `workspaces/` | `backend/config.py:40` | one scratch folder per session with no project |
| `checkpoints/` | `backend/config.py:50` | workspace snapshots, one folder per turn that wrote |
| `plans/` | `backend/config.py:49` | plan artifacts |
| `folders.json` | `backend/config.py:56` | the folder list the workspace picker shows |
| `benchmarks.json` | `backend/config.py:48` | per-model settings; the name predates its current use (`:44-47`) |
| `github.json` | `backend/config.py:35` | the connected GitHub account |
| `audit.jsonl` | `backend/config.py:58` | the ledger |
| `tacit.pid` | `backend/config.py:51` | the server's process id |

## What a session is made of

The index is resolved on every call rather than cached at import (`backend/store.py:9-17`). It used to
be a module constant, which meant redirecting the sessions directory moved the session files and left
the index behind, so the two halves of the store disagreed about where they lived
(`backend/store.py:12-15`).

`merge`, the endpoint that accepts updates arriving from another browser, never creates anything. It
used to invent a session for every unrecognised id, which meant one page load could multiply the list,
because the client pushed its whole local set and the server materialised each one. An unknown id is
now ignored: a session is created by the user, not by a mention of an id (`backend/store.py:311-318`).

## Where a session's tools operate

One rule, shared by every caller (`backend/config.py:883-901`):

- a project that exists always wins, because a session with a workdir must never be re-rooted into a
  scratch folder;
- a project that is named but missing counts as no project, because silently working in the home
  folder is worse than working in a scratch folder and stale paths arrive from stored sessions;
- only then does the session get `~/.tacit/workspaces/<sanitized id>`, created on demand
  (`backend/config.py:833-847`).

The home directory is not a fallback any more. It used to be, which meant a session with no project,
or with a project that had since been deleted, read and wrote in your home folder with only a short
blocklist in the way (`backend/config.py:888-891`). The scratch folder is why
`WORKSPACES_DIR` exists at all (`backend/config.py:37-39`).

Session ids are sanitised to alphanumerics, dot, underscore and hyphen, with anything else replaced by
an underscore and the result capped at 48 characters (`backend/config.py:829-830`).

## Deleting

`remove_workspace` deletes one session's scratch folder when the session is removed, because a deleted
session's files would otherwise sit in `workspaces/` forever: nothing else collects them, and they are
the thing the model wrote while nobody had chosen a project for it (`backend/config.py:850-873`). It
refuses anything that is not a real per-session folder, since an empty id maps to the shared `default`
and a path outside `WORKSPACES_DIR` is not Tacit's to delete, and it returns false rather than raising,
because a failed cleanup must not make the delete look like it failed (`backend/config.py:857-873`).

The audit ledger is the exception. It is append-only and nothing in it is edited or removed, so the
interface action that looks like clearing it is a rotation: the file is renamed, kept, and the rotation
is recorded as the first entry of the new ledger (`backend/audit.py:115-134`). You can of course delete
`~/.tacit` yourself; the file is plain JSONL you can read, grep or remove (`backend/audit.py:8-10`).

## Keys and secrets

A provider key goes to `~/.tacit/.env`, is masked in the interface, and the agent's own file tools
refuse the path: `DENY_NAMES` is `{".env", "models.json"}` and `DENY_PARTS` is `.git`, `__pycache__`,
`node_modules`, `.venv`, `venv` (`backend/agent.py:21-22`, enforced at `:47-51`). A protected name is
refused whether the call would read or write it (`backend/agent.py:47-48`).

The ledger masks on the way in rather than on the way out, at any depth, and the MCP layer redacts
before it writes its own log (`backend/audit.py:26-44`, `backend/mcp_registry.py:25-26`).

## Memory is local, and small

One SQLite file under the state directory, with full-text search when the SQLite build has it and
keyword search when it does not. No vector database is required (`backend/memory_store.py:12-14`,
`backend/memory_store.py:92`). Nothing is injected at startup unless it is explicitly enabled and fits
the configured budget, and the default budget is 120 tokens against a ceiling of 500, which an
explicit override pref lifts to 100,000 (`backend/memory_store.py:5-7`, `:30-31`, `:147-149`).

## What is never sent anywhere

The token count is computed locally: `tiktoken` is used if it happens to be installed and a character
heuristic is used otherwise, and an estimate is never labelled exact (`backend/tokens.py:7-10`).
Compaction's summariser call goes to the model you configured, and the model is not asked to find
learning candidates unless you turn that on (`backend/analyzer.py:8-13`). The analyzer reads finished
transcripts on a timer and writes proposals; that is its entire surface (`backend/analyzer.py:14-16`).

Next: [testing and benchmarks](testing-and-benchmarks.md).

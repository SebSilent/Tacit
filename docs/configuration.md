# Configuration

Two layers decide how Tacit behaves: the environment, read once when `backend/config.py` is
imported, and `~/.tacit/prefs.json`, read on every request. Where both can set the same thing, the
environment wins and the interface reports that it has been outranked.

## The precedence rule, in code

Four settings are consulted in that order rather than stored as a plain constant:

| Setting | Environment | Preference | Decided at |
|---|---|---|---|
| Delegation model | `TACIT_DELEGATE_MODEL` | `delegateModel` | `backend/config.py:365-368` |
| Fallback model | `TACIT_FALLBACK_MODEL` | `fallbackModel` | `backend/config.py:381-384` |
| Turn worker shape | `TACIT_TURN_WORKER` | none | `backend/config.py:406` |
| Delivery guarantee | `TACIT_DELIVER_GUARANTEE` | `deliverGuarantee` | `backend/config.py:456-464` |
| Shell overlap | `TACIT_SHELL_OVERLAP` | `shellOverlap` | `backend/config.py:474-478` |

The shell overlap case is the one the interface has to show, because a pinned environment variable
makes the switch in **Skills & Tools** do nothing. `backend/config.py:456-463` exists to answer that
question so the switch can say so instead of lying.

`TACIT_TEMPERATURE` is the exception to "environment always wins": it is a person typing a number,
so a per-model `temperature` in the catalog beats it for that model
(`backend/config.py:567`, `backend/ai/engine.py:21`).

## Environment variables

Every name below is read out of `os.environ`. The defaults are the ones in the code, not the ones in
the README.

### Where things live

| Variable | Default | Read at |
|---|---|---|
| `TACIT_HOME` | `~/.tacit` | `backend/config.py:30` |
| `TACIT_USER_HOME` | your home directory | `backend/config.py:29` |
| `TACIT_PROJECTS_ROOTS` | none | `backend/config.py:697` |

`TACIT_USER_HOME` moves where `~/.tacit` is looked for without moving the state directory itself,
which is what the tests use to keep a run off your real data.

### Server

| Variable | Default | Read at |
|---|---|---|
| `TACIT_HOST` | `0.0.0.0` | `backend/config.py:65` |
| `TACIT_PORT` | `8550` | `backend/config.py:66` |

The default bind is reachable from your network. `127.0.0.1` is a choice you make, not the starting
point.

### The agent loop

| Variable | Default | Read at |
|---|---|---|
| `TACIT_MAX_STEPS` | `24` | `backend/config.py:72` |
| `TACIT_SUBAGENT_STEPS` | `12` | `backend/config.py:96` |
| `TACIT_SUBAGENT_RESULT_LIMIT` | `4000` | `backend/config.py:97` |
| `TACIT_SUBAGENT_DEPTH` | `1` | `backend/config.py:98` |
| `TACIT_DELEGATE_MODEL` | session model | `backend/config.py:200`, `:368` |
| `TACIT_FALLBACK_MODEL` | none | `backend/config.py:384` |
| `TACIT_TURN_TOKEN_BUDGET` | `0`, no ceiling | `backend/config.py:152` |
| `TACIT_COST_WARN` | `0.25,0.5,0.75` | `backend/config.py:149` |
| `TACIT_TEMPERATURE` | `0.2` | `backend/config.py:79` |
| `TACIT_DELIVER_GUARANTEE` | on | `backend/config.py:456` |
| `TACIT_TURN_WORKER` | `process` | `backend/config.py:406` |

### Tool output ceilings

| Variable | Default | Read at |
|---|---|---|
| `TACIT_TOOL_OUTPUT_LIMIT` | `6000` | `backend/config.py:84` |
| `TACIT_READ_OUTPUT_LIMIT` | `48000` | `backend/config.py:93` |
| `TACIT_FETCH_LIMIT` | `6000` | `backend/config.py:205` |
| `TACIT_BROWSER_TEXT_LIMIT` | `6000` | `backend/config.py:181` |
| `TACIT_BROWSER_TIMEOUT` | `90` | `backend/config.py:180` |
| `TACIT_BROWSER_SEARCH_TIMEOUT` | `120` | `backend/config.py:187` |
| `TACIT_SHELL_TIMEOUT` | `180` | `backend/config.py:94` |
| `TACIT_SHELL_OVERLAP` | `block` | `backend/config.py:474` |
| `TACIT_PLAYWRIGHT_PATH` | auto-detected | `backend/config.py:157` |
| `TACIT_SKILL_INDEX_LIMIT` | `24` | `backend/config.py:203` |

`read_file` has its own, much larger ceiling because a file is the one result the agent may have to
reproduce whole (`backend/config.py:84`, `:93`).

### Research

| Variable | Default | Read at |
|---|---|---|
| `TACIT_RESEARCH_ASPECTS` | `3` | `backend/config.py:188` |
| `TACIT_RESEARCH_STEPS` | `6` | `backend/config.py:193` |
| `TACIT_RESEARCH_TIMEOUT` | `240` | `backend/config.py:197` |

These three are quoted inside the `research` tool's own description, read from the config rather
than written out, so the sentence cannot drift away from the caps (`backend/agent.py:704-709`).

### Context and compaction

| Variable | Default | Read at |
|---|---|---|
| `TACIT_COMPACT_AT` | `0` | `backend/config.py:100` |
| `TACIT_CONTEXT_RESERVE` | `33000` | `backend/config.py:108` |
| `TACIT_CONTEXT_FILL_FLOOR` | `0.5` | `backend/config.py:114` |
| `TACIT_COMPACT_TAIL_TOKENS` | `8000` | `backend/config.py:119` |
| `TACIT_COMPACT_TAIL_MIN` | `4` | `backend/config.py:120` |
| `TACIT_COMPACT_TAIL_MAX` | `40` | `backend/config.py:121` |
| `TACIT_COMPACT_TAIL_SHARE` | `0.4` | `backend/config.py:126` |
| `TACIT_COMPACT_PRUNE_KEEP` | `400` | `backend/config.py:129` |
| `TACIT_COMPACT_PRUNE_MIN` | `1200` | `backend/config.py:130` |
| `TACIT_SUMMARY_INPUT_MAX` | `160000` | `backend/config.py:138` |
| `TACIT_COMPACT_KEEP` | `6` | `backend/config.py:139` |
| `TACIT_COMPACT_MID_TURN` | `1` | `backend/config.py:143` |
| `TACIT_CONTEXT_BUDGET` | `120000` | `backend/config.py:73` |
| `TACIT_CONTEXT_BUDGET_MIN` | `24000` | `backend/config.py:77` |
| `TACIT_CONTEXT_BUDGET_MAX` | `2000000` | `backend/config.py:78` |

`TACIT_COMPACT_AT` stays at `0` because `0` means "use the reserve model", which is the behavior the
code argues for at `backend/config.py:107` and `:219`. Setting it to a fraction opts out of that
model deliberately.

### Snapshots, plans, git, providers

| Variable | Default | Read at |
|---|---|---|
| `TACIT_SNAPSHOT_MAX_FILE` | 2 MB | `backend/config.py:208` |
| `TACIT_SNAPSHOT_MAX_FILES` | `3000` | `backend/config.py:214` |
| `TACIT_PLAN_TASKS` | `4` | `backend/config.py:154` |
| `TACIT_PLAN_QUESTIONS` | `4` | `backend/config.py:155` |
| `TACIT_VCS_TIMEOUT` | `120` | `backend/vcs.py:26` |
| `TACIT_VCS_NAME` | none | `backend/vcs.py:176` |
| `TACIT_VCS_EMAIL` | none | `backend/vcs.py:177` |
| `TACIT_PROMPT_CACHE` | `auto` | `backend/config.py:887` |
| `TACIT_ANTHROPIC_MAX_TOKENS` | `8192` | `backend/config.py:889` |
| `TACIT_ANTHROPIC_VERSION` | `2023-06-01` | `backend/config.py:890` |
| `TACIT_ANTHROPIC_THINKING` | `4096` | `backend/config.py:891` |

The two git identity variables work only as a pair (`backend/vcs.py:175-180`), and they are read
before the values stored in **Settings**, not after.

`TACIT_PROMPT_CACHE=auto` marks cache breakpoints only on endpoints known to understand them,
because an OpenAI-compatible server that has never seen `cache_control` may reject the request
outright (`backend/config.py:881-887`).

### Test and portability hooks

`TACIT_OS` and `TACIT_PLATFORM` let a test report a different operating system to the harness
(`backend/config.py:11`, `:17`). They exist because the shell tool's description states the platform
it runs on (`backend/agent.py:701-703`), and that sentence has to be testable.

## prefs.json

Plain JSON at `~/.tacit/prefs.json` (`backend/config.py:34`), written through
`backend/config.py:481-483`. These are the keys the backend reads:

| Key | Read at | Notes |
|---|---|---|
| `model` | `backend/routers/api.py:752` | the session default when no ref is given |
| `thinking` | `backend/routers/api.py:72`, `:778` | reasoning level, written at `:794` |
| `delegateModel` | `backend/config.py:365` | overridden by `TACIT_DELEGATE_MODEL` |
| `fallbackModel` | `backend/config.py:381` | the model a session runs on when its own is gone; without it the turn fails and says so |
| `deliverGuarantee` | `backend/config.py:444` | overridden by `TACIT_DELIVER_GUARANTEE` |
| `shellOverlap` | `backend/config.py:487-504` | overridden by `TACIT_SHELL_OVERLAP` |
| `turnSound` | `backend/config.py:413` | on by default |
| `alertSound` | `backend/config.py:426` | off by default |
| `allowVersionControl` | `backend/config.py:496` | absent means the agent may not run git |
| `disabledTools` | `backend/agent.py:866`, `backend/profiles.py:236` | tool names removed from the registry |
| `projectRoots` | `backend/config.py:701` | folders the workspace picker offers |
| `noContextFiles` | `backend/routers/api.py:825`, `:831` | stop reading `AGENTS.md` and friends |
| `memoryBudget` | `backend/memory_store.py:142` | startup memory ceiling in tokens |
| `memoryBudgetOverride` | `backend/memory_store.py:147` | lifts the 500 ceiling to 100,000 |

The sound switches have no environment variable. That is deliberate: they describe the machine the
server is on, and a person who silenced it wants every device pointed at this install to stay
silent.

## The other files

| File | Written by | Holds |
|---|---|---|
| `~/.tacit/models.json` | `backend/config.py:509`, seeded at `:262-263` | providers, models, the default ref |
| `~/.tacit/.env` | `backend/config.py:33` | API keys, masked in the interface |
| `~/.tacit/capabilities.json` | `backend/providers.py:195`, `:214` | the sandbox, memory and learning selections |
| `~/.tacit/profiles.json` | `backend/profiles.py:105-106`, read at `:93-102` | your saved profiles and which is active |
| `~/.tacit/mcp.json` | `backend/mcp_registry.py:61`, read at `:50` | servers, allow and deny lists, MCP settings |
| `~/.tacit/plugins.json` | `backend/plugin_manager.py:65`, read at `:52` | which plugins are enabled |
| `~/.tacit/learning.json` | `backend/learning.py:63`, read at `:51`, bounded at 200 rows (`:38`) | proposals as artifacts |
| `~/.tacit/audit.jsonl` | `backend/audit.py:62-64` | one line per important action |
| `~/.tacit/evidence.jsonl` | `backend/evidence.py:36-37` | citable facts recorded during a turn |

Container settings live in `capabilities.json` rather than in the environment because they are a
capability choice: `container_image`, `container_auto_pull` and `container_pids_limit` are read at
`backend/sandbox.py:585-587`.

## Installer variables

Four names are read by the installers and never by the server: `TACIT_REPO_URL` and `TACIT_BRANCH`
choose what to clone (`install.sh:10-11`, `install.ps1:13-14`), `TACIT_APP_DIR` and `TACIT_BIN_DIR`
choose where it lands (`install.sh:14-15`, `install.ps1:16-17`). `TACIT_NO_BROWSER=1` skips the
Playwright download and `TACIT_NO_PATH=1` declines to edit `PATH` (`install.ps1:18`, `:200`).
`TACIT_PORT` is read by the launcher the installer writes (`install.sh:178`, `install.ps1:205`).

Next: [tools](tools.md), or [data and privacy](data-and-privacy.md) for what each file contains.

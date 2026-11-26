# Tacit

**The coding agent where nothing is hidden and nothing moves without you.**

![license: MIT](assets/badges/license.svg)
![runtime: Python 3.10+](assets/badges/runtime.svg)
![architecture: Local-First / No DB](assets/badges/architecture.svg)
![build: Zero Build Step](assets/badges/build.svg)
![privacy: No Tracking](assets/badges/privacy.svg)

Tacit is a coding agent you run yourself and use in your browser. It reads your files, runs commands,
edits code, searches the web, and connects to external tools. Requests go to a model provider you
configure; everything else stays on your machine. No accounts, no telemetry, no service to install.

The application is one Python program serving plain web pages: no build step, no frontend toolchain, no
database. A coding harness is the part of the stack you are supposed to be able to read, and a platform
inside it is the part you cannot. Tacit is the readable kind.

> **The number to remember.** One turn costs a fixed amount before your question is read:
> 974 characters of standing instructions plus every tool description the session is offered. The pair
> to remember is 768 tokens on Minimal and 2,039 on Default, and a test re-derives both from the code on
> every run.

![A turn streaming in the Tacit interface](assets/demo.gif)

**See it before you install it:** [the live demo](https://sebsilent.github.io/Tacit/demo.html) runs a
scripted turn in your browser, and [the website](https://sebsilent.github.io/Tacit/) documents every
decision with the line of code behind it. Do not treat the demo as evidence.

---

## At a glance

| | |
|---|---|
| **Runs** | on your machine, in your browser; Python 3.10+ is the only requirement |
| **Stores** | plain files under `~/.tacit`, no database |
| **Sends** | requests to the provider you configured, and nothing else |
| **Costs** | ~768 to ~2,264 tokens of preamble per turn, measured and published |
| **Hides** | nothing; every decision lands in a file you can read |

---

## Quick start

**macOS, Linux, WSL**

```sh
curl -fsSL https://raw.githubusercontent.com/SebSilent/Tacit/HEAD/install.sh | bash
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/SebSilent/Tacit/HEAD/install.ps1 | iex
```

**Windows (cmd)**

```bat
powershell -NoProfile -c "irm https://raw.githubusercontent.com/SebSilent/Tacit/HEAD/install.ps1 | iex"
```

The installer fetches the source, makes a private virtual environment, and writes a `tacit` launcher.
Re-running it updates the checkout; `--no-browser` skips the optional browser-automation step.

```sh
tacit          # or ./run.sh, or run.bat
```

Open **http://localhost:8550**, add a provider in **Settings > Providers**, and your keys go to
`~/.tacit/.env`. From a clone instead:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # Windows: .venv\Scripts\python.exe
./run.sh                                              # or run.bat
```

Full instructions, including relocating and removing an install:
[Install](https://sebsilent.github.io/Tacit/install.html).

---

## What makes it different

**The fixed cost of a turn is measured, shown, and yours to set.** Most harnesses never say what the
standing instructions and tool schemas cost. Tacit quotes it per turn and per profile, computed from the
live configuration rather than a typed table. On an 8K model, 2,039 tokens of preamble is a quarter of the
window before work starts; the remedy is a smaller profile.

**A turn is a process, so stop means stop.** A thread cannot be killed in Python; a turn can. Each turn
runs in its own worker subprocess (`backend/turn_worker.py:11-21`) and stop is two-stage: a cooperative
cancel that closes the model stream and kills the turn's shell trees, then a process-tree kill after a
grace period. Sessions are keyed by id, so a busy session refuses a second prompt from any window and
abort cannot cross a session boundary.

**Capabilities load on first use.** Skills and knowledge are listed by name and fetched
only when asked (`backend/skills.py:50`). An MCP server's tool descriptions stay out of the prompt until
activated, and with no server configured even the helper schemas are withdrawn
(`backend/agent.py:996-983`). A server with fifty tools costs nothing until one is used.

**Long sessions are managed against a fixed reserve.** Compaction fires at the model's window
minus about 33,000 tokens, because the room the next turn needs is roughly constant
(`backend/config.py:174`). The tail is kept by token budget, bulky tool output is cleared before the
summariser sees it, a failed summary still leaves a deterministic digest, and the standing prompt and the
task are pinned against elision.

**Nothing optional is on, and nothing learns without you.** The defaults live in one dict,
`backend/providers.py:22-38`: sandbox `none`, memory `off`, learning `learn-off`. Memory's budget is
enforced on the rendered block (`backend/memory_modes.py:120-126`). Learning proposes and does not act; a
proposal carries the exact sentence that triggered it and only becomes a rule when you approve it
(`backend/proposal_pass.py:52`).

**Undo is a person's instrument.** A snapshot is taken once per turn before the first change, and the agent
is never offered `restore`: a model that can rewind its own work can hide what it just did.

[Architecture](https://sebsilent.github.io/Tacit/architecture.html) ·
[Cost and profiles](https://sebsilent.github.io/Tacit/cost-and-profiles.html) ·
[Memory and learning](https://sebsilent.github.io/Tacit/memory-and-learning.html)

---

## What a turn costs, against the alternatives

Measured on one machine with one rule: characters divided by four, the fallback heuristic Tacit uses when
no exact tokenizer is installed. Every figure counts what the harness sends, not what a bill looks like.

| System | System prompt | Tools | Tool schemas | Fixed cost per turn |
|---|---|---|---|---|
| **Tacit, Minimal** | **974 chars** | **7** | **2.0 KB** | **~768 tokens** |
| Pi | 1,352 chars | 7 | 4.5 KB | ~1,600 |
| **Tacit, default profile** | **974 chars** | **21** | **7.0 KB** | **~2,039** |
| little-coder | 7,747 chars | ~29 | 10.4 KB | ~4,600 |
| DSH | 6,195 chars | 25 | 26.7 KB | ~8,390 |
| Hermes Agent | 23,370 chars | 32 | 51.3 KB | ~18,970 |

Tacit appears twice because the profile is the variable; the two rows are the same program:
768 tokens on Minimal and 2,039 on Default. The Minimal row compares the same seven capabilities on both
sides: read, write, edit, shell, grep, list and glob.

| Same seven capabilities | Tacit | Pi |
|---|---|---|
| Tool schemas | **2,001 chars** | 4,626 chars |
| System prompt | **974 chars** | 1,352 chars |
| **Fixed cost per turn** | **~768 tokens** | ~1,600 |

> **What this does not prove.** A smaller fixed cost is not a lower API bill and not a higher solve rate.
> Total spend is the preamble times the number of steps plus the transcript; a harness that sends less per
> step but takes more steps can lose. It is the term a person can control, which is why it is measured and
> published rather than asserted.

### The six built-in profiles

| Profile | Tools | Sandbox | Memory | Learning | Fixed cost |
|---|---|---|---|---|---|
| **minimal** | 7 | none | off | off | ~768 tokens |
| **default** | 21 | none | off | off | ~2,039 tokens |
| **safe** | 22 | tacit-micro | explicit | off | `default` + the memory tools + budget |
| **power-isolation** | 22 | tacit-micro | explicit | off | `default` + the memory tools + budget |
| **power-memory** | 23 | none | full, budgeted | off | `default` + the memory tools + budget |
| **full** | 23 | tacit-micro | full, budgeted | off | `default` + the memory tools + budget |

**The tool count moves with the memory mode,** because memory is a capability the profile selects rather
than a plugin it enables: `explicit` is offered the propose tool only, `jit` and `full` also get the search
tool.

**No profile turns learning on, not even proposing:** a profile sets cost and containment, and does not
decide what Tacit is allowed to remember on its own.

### Caching, because a long session is the whole bill

An agent loop re-sends its transcript on every step, so Tacit uses the caching strategy the endpoint
actually supports.

| Endpoint | Strategy | What is sent |
|---|---|---|
| Anthropic (`api.anthropic.com`) | native Messages API | breakpoints on tools, system and the last message |
| OpenRouter | OpenAI shape, passthrough | one breakpoint on the system block |
| Any other OpenAI-compatible server | provider-side automatic | **nothing added** |

The last row matters most: a server that has never seen `cache_control` may reject the body outright, so
nothing is added there. A rejection is retried once without the markers before any event streams
(`backend/config.py:1098-1103`). One measured run on an OpenAI-compatible endpoint reported **71.1%** of
prompt tokens served from cache.

---

## Evidence: OpenBench

One model for every row: GLM 5.2 on the fp8 endpoint, run by OpenBench's own runner with its `checker.sh`
files untouched. Three tasks, three trials each, one build, both operating systems. Tokens are uncached
input plus output, cache reads excluded. All rows are Tacit v1.0.0.

**Linux**

| Profile | Solved | Tokens per solve | Wall seconds per cell |
|---|---|---|---|
| Default | 9/9 | 156,466 | 705 |
| Minimal | 9/9 | 133,881 | 648 |

**Windows**

| Profile | Solved | Tokens per solve | Wall seconds per cell |
|---|---|---|---|
| Default | 6/6 | 106,522 | 530 |
| Minimal | 5/6 | 82,841 | 418 |

Schemelike is graded on Linux only so far: its checker reads `os.O_NONBLOCK`, which Windows Python does not
provide.

**Against the published arms, same model, same three tasks**

| Arm | Solved | Cells that errored |
|---|---|---|
| Tacit, Linux, default | 9/9 | 0 |
| Tacit, Linux, minimal | 9/9 | 0 |
| Pi | 8/9 | 3 |
| OpenCode | 7/9 | 3 |
| Claude | 5/9 | 5 |
| Grok | 5/9 | 6 |
| Codex | 4/9 | 5 |

An errored cell is a run where the arm's own plumbing decided the outcome, and its tokens still got spent.
That column is what a token table cannot show.

> **Read the scope with the result.** One model, one endpoint, one machine, twenty cells. Three trials per
> task is enough to show a plumbing failure and not enough to rank harnesses on a hard task. The Windows
> Minimal row at 5/6 is the counterexample already in the table.

**Three harness behaviours changed these scores, and they are here so you can discount them.**

- **The delivery guarantee,** on by default, finishes a write the turn started and missed when it is about
  to end with nothing on disk, gives the salvage phase three rounds rather than one, and spends the last
  minutes of wall budget landing it (`backend/config.py:634-649`, `backend/agent.py:896-2639`): on, a failed
  cell is still a graded artifact, and `TACIT_DELIVER_GUARANTEE=0` is the raw mode where the turn ends when
  the agent ends it.
- **The repeat guard** answers an identical call from cache after three runs and ends a turn after four
  blocked repeats (`backend/agent.py:880-886`), because one cell spent 26 of its 29 shell calls on refused
  repeats and ended with nothing written.
- **The uncapped retry** re-asks a round this harness's own output ceiling destroyed, so thinking is never
  billed as waste, and that one is not part of the delivery switch.

**Run it yourself; that is the point of publishing it.** A three-trial run of one task lands before your
coffee is cold. The cells this table wants next: any first-party or fp8 route for kimi and qwen, DeepSeek
and Qwen as the model, an Apple silicon lane, a rerun of ours on whatever you have, and a Windows port of
the schemelike checker. Tuning is wanted too, since the other arms ship tuned for the models they run and
Tacit's profiles and prefs are the tuning surface. Send your rows back through a pull request or an issue
with the jsonl attached and your arm gets a line in the table with your name on it.

Methodology, the per-task breakdown, and how each row is audited:
[Testing and benchmarks](https://sebsilent.github.io/Tacit/testing-and-benchmarks.html).

---

## Security, privacy and control

**It reports what it enforces.** A command either runs under the backend you chose or it does not run;
nothing is silently downgraded (`backend/sandbox.py:9-10`). On Windows the base OS has no isolation
primitive, so the result says plainly that filesystem and network isolation are not in force. `tacit-micro`
uses the strongest primitive the OS offers (bubblewrap on Linux, `sandbox-exec` on macOS); the opt-in
`container` backend adds a network namespace, project bind, private `/tmp`, resource ceilings and a PID
limit wherever Docker or Podman runs.

**The timeout is enforced.** The whole process tree is killed and the drain is
bounded, so a timeout returns in about the time it names, even when a wrapper leaves grandchildren holding
the pipe.

**Every decision is in a file you can read.** Tool calls, sandbox decisions, memory injections and learning
approvals append to `~/.tacit/audit.jsonl`, credentials masked at any depth. Nothing is edited or removed;
*clear* rotates the file and records the rotation.

**Off unless you say otherwise:** version control for the agent, plugins, external tool servers, memory,
sandboxing, learning, and the per-turn token ceiling. New MCP servers start disabled; tools that look
destructive ask first. Tacit installs nothing on its own.

**On the network.** No telemetry, no analytics, no tracking. The outbound calls are the provider you
configured, the optional update check, the GitHub panel, the `fetch` and `browser` tools when asked, and
MCP servers you added. `TACIT_HOST` is yours; there is no login system, so bring your own.

[Isolation and audit](https://sebsilent.github.io/Tacit/isolation-and-audit.html) ·
[Data and privacy](https://sebsilent.github.io/Tacit/data-and-privacy.html)

---

## Documentation

The README states what Tacit is and what it costs. The website states how it works, where each setting
lives, and which line of code decides it; a test opens every citation. Start at
[the documentation](https://sebsilent.github.io/Tacit/), or jump to
[Install](https://sebsilent.github.io/Tacit/install.html),
[Configuration](https://sebsilent.github.io/Tacit/configuration.html),
[Tools](https://sebsilent.github.io/Tacit/tools.html),
[Architecture](https://sebsilent.github.io/Tacit/architecture.html).

Every `TACIT_*` name in this README is read from the environment by name in `backend/config.py`; a name
absent from that table is not a setting. Where a variable duplicates something **Settings** can set, the
environment wins and the interface says so.

---

## Contributing and testing

```sh
python -m unittest discover -s tests -t .
python scripts/check_prose.py
```

The suite runs offline: no network, no real model, no container daemon, no benchmark runner, and Tacit
installs neither. The count is whatever the suite reports, not a number maintained by hand. The files that
guard the architecture rather than a feature are `test_turn_worker.py`, `test_session_isolation.py`,
`test_isolation.py`, `test_engine.py`, `test_docs.py`, `test_frontend_weight.py`, and `test_claims.py`,
which enforces each behaviour this README claims, including the agent loop end to end.

Two rules are executable rather than editorial: the documentation is a specification (every `path:line`
citation must point at a real, non-blank line), and the prose has a denylist (no em dashes, hollow
adjectives or throat-clearing openers). If you cannot point at the line, you do not write the sentence.

[CONTRIBUTING.md](CONTRIBUTING.md) covers the layout and the house rules.

---

## Status and known limits

**Working and covered by tests:** agent turns against any OpenAI-compatible endpoint, sub-agents, plan
mode, skills, compaction, background processes, snapshots with restore, the terminal, providers, sessions,
token accounting, capability profiles, session isolation, the killable turn worker, MCP with lazy
activation, plugins, isolation, budgeted memory, approval-first learning, and the per-session Assistant.

**What is not finished:**

- **Windows has no isolation primitive.** The timeout plus the change report are all that is enforced.
- **Token counts are estimates without `tiktoken`.** Figures are marked `~` when they are estimates.
- **The default shell timeout is 180 seconds.** Lower `TACIT_SHELL_TIMEOUT` to fail faster.
- **Container isolation needs a runtime you install yourself**, checked by hand rather than a shipped test.
- **Retrieval is keyword-only**, and the task strip is read-only.
- **Anthropic cache breakpoints cover the stable prefix and the last message only.**
- **There are no releases.** The default branch is the product, which is why the updater exists.

---

## License

MIT. See [LICENSE](LICENSE).

# Tacit

**The Silent Harness.** A local/LAN coding-agent you run as a website. One Python process hosts
its own agent loop and serves a zero-build, vanilla-JS frontend.

Lean where it counts: a ~980-character system prompt, capability loaded on demand, and sub-agents
that keep exploration out of your context window.

---

## Install

**macOS / Linux / WSL**

```sh
curl -fsSL https://raw.githubusercontent.com/Silent/tacit/main/install.sh | bash
```

**Windows** (PowerShell)

```powershell
irm https://raw.githubusercontent.com/Silent/tacit/main/install.ps1 | iex
```

The installer downloads the source, builds a private virtualenv in the checkout, and writes a
`tacit` launcher (`~/.local/bin/tacit`, or `%LOCALAPPDATA%\Tacit\bin\tacit.cmd`). Re-running it
updates the checkout. Useful flags: `--no-browser` / `TACIT_NO_BROWSER=1` skips the optional
Playwright step (about 130 MB), `--dir` picks the checkout location, `--help` lists the rest.

## Requirements

- **Python 3.10+** — the whole backend
- **Node 22+** — *only* for the optional browser tool

## Run from source

```sh
python -m pip install -r requirements.txt
python -m backend.main
```

Windows: double-click **`run.bat`**. macOS/Linux: `./run.sh`.

Open **http://localhost:8550**. On first boot Tacit creates `~/.tacit` — empty. Add a provider in
**Settings ▸ Providers** and your keys land in `~/.tacit/.env`. Nothing is imported from anywhere
else on the machine.

### Browser automation (optional)

The `browser` tool drives a real Chromium. It is the only part that needs Node. Install **either**:

```sh
npm install                                   # uses the playwright declared in package.json
```
```sh
pip install playwright && playwright install chromium
```

Then restart. If neither is present, the tool says exactly this instead of failing silently.
To reuse an install you already have, point `TACIT_PLAYWRIGHT_PATH` at it.

## Where your data lives

Everything Tacit reads or writes is under **`~/.tacit`** — never inside this repo, and never in
another tool's directory.

```
~/.tacit/
├─ models.json        providers + models      (Settings ▸ Providers)
├─ .env               API keys
├─ prefs.json         project roots, defaults
├─ skills/            your skills      ─┐ listed by name, loaded on demand
├─ knowledge/         reference cards  ─┘
├─ benchmarks.json    per-model temperature / max_tokens
├─ evidence.jsonl     recorded citable facts
├─ checkpoints/       project snapshots for undo
├─ plans/             approved plans
├─ sessions/          transcripts + metadata
└─ github.json        hosting panel state
```

Delete `~/.tacit` and Tacit forgets you — that is intended.

## How it works

```
backend/
├─ main.py          FastAPI app, serves static/, /health
├─ config.py        every path + setting  (the single source of truth)
├─ agent.py         the agent loop, the tools, the sandbox
├─ engine.py        one OpenAI-compatible streaming client   (ai/)
├─ prompts.py       the system prompt                        (ai/)
├─ skills.py        skills + knowledge, progressive disclosure
├─ plan.py          plan mode: decompose → explore → questions → plan
├─ research.py      multi-source research with citations
├─ browser.py       optional Playwright driver
├─ evidence.py      citable fact store
├─ benchmarks.py    per-model profiles
├─ extras.py        background processes, fetch, snapshots
├─ store.py         sessions, transcripts, cross-device registry
├─ vcs.py           version-control bridge (the human's panel)
├─ hosting.py       repository hosting bridge
└─ routers/         api · chat (WebSocket) · vcs · hosting
```

**Context discipline** is the design principle. The always-on prompt is ~980 characters. Skills and
knowledge are indexed by name and read only when used (~84 chars each up front instead of the full
body). `task` runs a sub-agent in a throwaway context and returns only its report. Compaction
summarises old turns in place. `windowed()` caps what is sent.

**The agent works in a project you pick.** Relative paths resolve there. There is no bundled
sandbox directory.

## Tools

| | |
|---|---|
| `list_files` `read_file` `write_file` `edit_file` `glob_files` `grep_files` | files |
| `run_shell` · `bg_start` `bg_output` `bg_stop` | commands, foreground and background |
| `snapshot` `list_snapshots` `restore` | undo for the agent's own edits |
| `task` | delegate to a sub-agent (throwaway context) |
| `skill` · `research` · `fetch` · `browser` | knowledge on demand |
| `evidence` · `benchmark` | citable facts · per-model settings |

Version control is deliberately **not** available to the model. The Git panel is the human's route,
and `run_shell` refuses version-control binaries.

## Configuration

| Variable | Default | |
|---|---|---|
| `TACIT_PORT` | `8550` | web host port |
| `TACIT_HOME` | `~/.tacit` | all state |
| `TACIT_HOST` | `0.0.0.0` | bind address |
| `TACIT_PROJECTS_ROOTS` | auto-discovered | folders the project picker lists |
| `TACIT_PLAYWRIGHT_PATH` | auto-discovered | an existing playwright install |
| `TACIT_MAX_STEPS` | `24` | tool steps per turn |
| `TACIT_SUBAGENT_STEPS` | `12` | steps per sub-agent |
| `TACIT_COMPACT_AT` | `0.65` | compact at this fraction of the window |
| `TACIT_TOOL_OUTPUT_LIMIT` | `6000` | chars kept from a tool result |
| `TACIT_SHELL_TIMEOUT` | `180` | seconds a shell command may run |
| `TACIT_TEMPERATURE` | `0.2` | sampling temperature |

## Security

- **No authentication.** It binds `0.0.0.0` — anyone who can reach the port can drive the agent.
  Run it on a trusted LAN, or set `TACIT_HOST=127.0.0.1`.
- API keys stay in `~/.tacit/.env` and are never written into a project.
- The agent's file tools refuse the key store; the shell tool refuses version control.

## Status

Working: chat and agent turns against any OpenAI-compatible endpoint, sub-agents, plan mode,
skills, compaction, background processes, snapshots, the terminal, providers, sessions.

Not yet verified end-to-end: the version-control and hosting panels (the routes and parsers are
built and unit-tested, but they have not been exercised against a live repository).

## License

MIT — see [LICENSE](LICENSE).

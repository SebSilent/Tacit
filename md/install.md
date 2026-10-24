# Install

Python 3.10 is the only prerequisite. Everything else here is optional, and Tacit installs none of it without asking.

## One command

**macOS, Linux, WSL**

```sh
curl -fsSL https://raw.githubusercontent.com/SebSilent/Tacit/HEAD/install.sh | bash
```

**Windows, PowerShell**

```powershell
irm https://raw.githubusercontent.com/SebSilent/Tacit/HEAD/install.ps1 | iex
```

**Windows, cmd**

```bat
powershell -NoProfile -c "irm https://raw.githubusercontent.com/SebSilent/Tacit/HEAD/install.ps1 | iex"
```

What the installer does, in order:

1. Clones the repository at depth 1 into `%LOCALAPPDATA%\Tacit\app` on Windows, or the folder you name with `--dir` (`install.sh:20`, `install.ps1:16`).
2. Creates a private `.venv` inside that checkout.
3. Installs what `requirements.txt` lists: `fastapi`, `uvicorn[standard]`, `httpx`. Three lines. That is the whole runtime.
4. Offers the Playwright browser driver, about 130 MB, and takes no for an answer.
5. Writes a launcher: `%LOCALAPPDATA%\Tacit\bin\tacit.cmd` or `~/.local/bin/tacit`.
6. Starts the server on port 8550.

Re-running it updates the checkout in place.

The POSIX installer takes `--dir PATH`, `--tacit-home PATH`, `--bin-dir PATH`, `--branch NAME`, `--no-browser` and `--help` (`install.sh:20-34`). On Windows the parser covers two of those, `--no-browser` and `--branch` (`install.ps1:21-25`), and the rest of the choices arrive as environment variables: `TACIT_HOME`, `TACIT_APP_DIR`, `TACIT_BIN_DIR`, `TACIT_NO_BROWSER=1`, `TACIT_PORT`.

## Requirements

| You need | Why |
|---|---|
| **Python 3.10 or newer** | The only real prerequisite. |
| `curl` | To fetch the installer on macOS and Linux. Windows uses PowerShell, which is already there. |
| `git` | Optional. With it the installer clones and can update a checkout; without it the installer downloads a source archive and says so out loud (`install.sh:109-121`). |
| Node 22 | Optional, and only for browser automation. Skip it with `--no-browser`. |

No database to install, no build pipeline, no frontend toolchain, nothing to configure before the first run. Neither Hermes nor DSH is required, referenced or contacted at any stage.

## Run it from source

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run.sh
```

On Windows both lines use `.venv\Scripts\python.exe`, then you double-click `run.bat`.

Both launchers refuse to start without `.venv`, and print the two commands above rather than falling back to whatever `python` happens to be first on your `PATH` (`run.bat:9-27`). The reason is in the file's own comment: an ambient interpreter may belong to another tool, and a venv built by that tool ties Tacit to an interpreter you never chose.

Open **http://localhost:8550**.

## First run

Tacit creates `~/.tacit` on first start. It is empty, and everything it later holds is described in [Where your data lives](data-and-privacy.md).

Two things need doing before the agent is useful:

**A provider.** Settings > Providers takes a base URL and a key. Keys go to `~/.tacit/.env`, are masked in the interface, are never written into a project or a log, and the agent's own file tools refuse that path. Any OpenAI-compatible endpoint works, including Ollama, llama.cpp and LM Studio on your own box.

**A folder to work in.** The workspace picker shows nothing until you point it somewhere: `TACIT_PROJECTS_ROOTS`, or the `projectRoots` list in `~/.tacit/prefs.json`. Tacit does not guess where your projects live, which is why the list starts empty.

## Browser automation

The `browser` tool drives a real Chromium and is the only part that needs Node. Install either:

```sh
npm install
```

```sh
pip install playwright && playwright install chromium
```

Then restart. If neither is present the tool reports itself unavailable instead of failing silently, and the rest of Tacit is unaffected. To reuse an installation you already have, set `TACIT_PLAYWRIGHT_PATH`.

## Moving it, and removing it

State is one folder: `~/.tacit`. Copy it to another machine and your providers, sessions, skills, memory and settings come with it. Delete it and Tacit has never been here.

Nothing is written into the checkout at runtime, and nothing is written into another tool's directory. There is no service to uninstall and no entry to remove from anywhere except the launcher file the installer wrote.

Next: [the interface](interface.md), then [the settings map](settings.md).

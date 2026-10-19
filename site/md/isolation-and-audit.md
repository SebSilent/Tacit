# Isolation and audit

Two questions have to have readable answers after the fact: what was this command allowed to touch,
and what did it actually change (`backend/sandbox.py:4-6`). Neither is a security boundary against a
determined attacker, and the module says so in the first sentence of its docstring
(`backend/sandbox.py:3-4`).

The rule that holds the design together: a command either runs under the backend you chose, or it does
not run. It is never silently downgraded to something weaker (`backend/sandbox.py:9-10`).

## Three backends

| Backend | What it is | Declared at |
|---|---|---|
| `none` | direct execution, fully transparent, trusted local use | `backend/providers.py:72-74` |
| `tacit-micro` | the strongest primitive this OS offers, plus a timeout, limits where the platform allows them, and a report of every file that changed | `backend/providers.py:75-81` |
| `container` | Docker or Podman, opt-in | `backend/providers.py:82-90` |

Selection happens at `backend/sandbox.py:258-280`. An unknown backend, one with no adapter, and one
that is unavailable each return an error rather than falling back
(`backend/sandbox.py:260-269`).

## What the platform can actually enforce

`mechanism()` answers which primitive would be used right now (`backend/sandbox.py:60-69`):
`container` if a container runtime is present and chosen, `bubblewrap` or `posix-limits` on Linux,
`sandbox-exec` or `posix-limits` on macOS, and `none` on Windows.

`platform_capabilities()` turns that into a flag set (`backend/sandbox.py:72-93`). On Windows the
answer is a timeout and nothing else: `cpu_limit`, `memory_limit`, `file_size_limit`, `network`,
`readonly_project`, `private_tmp` and `process_isolation` are all false, because `IS_POSIX` is false
(`backend/sandbox.py:43`) and no base-OS equivalent exists (`backend/sandbox.py:25-27`). A container
runtime enforces every one of those except the file size limit and overlay writes, on every platform
including Windows, which is the reason it is offered at all (`backend/sandbox.py:75-81`).

Nothing is faked to fill that gap. The isolation ladder is stated plainly in the module header
(`backend/sandbox.py:21-27`), and a run on Windows carries a note saying that this system offers no
isolation primitive Tacit can use, that the timeout and the change report still apply, and that
nothing here is pretending otherwise (`backend/sandbox.py:529-532`).

## What a run reports back

Every result has the same shape (`backend/sandbox.py:283-294`): `ok`, `backend`, `code`, `stdout`,
`stderr`, `blocked`, `notes`, `changed` with added, modified, removed and a count, `enforced`,
`requested`, and `duration_ms`.

`enforced` is computed per run, not per platform: a limit counts as enforced only when the platform
can apply it and the box asked for it (`backend/sandbox.py:511-520`). `requested` records what the
settings asked for (`backend/sandbox.py:543`), so a reader can compare the two. Where they disagree,
a note says which guarantee is missing: network not cut off and proxy variables advisory only
(`backend/sandbox.py:521-523`), the project not mountable read-only (`:524-525`), CPU or memory limits
not enforced (`:533-536`).

`changed` comes from scanning the project root before and after and diffing the two
(`backend/sandbox.py:501`, `:510`). That report is produced whatever the backend, which is why a
command that rewrote the tree on Windows still tells you what it rewrote.

## The timeout is enforced, not reported

`GRACE_SECONDS` is 10 (`backend/sandbox.py:297`), and a timed-out run returns exit code 124 with the
output it printed before the kill (`backend/sandbox.py:540-541`).

Killing only the direct child is not enough. On Windows a `.bat` or `cmd /c` wrapper leaves its
grandchildren running, and they keep the captured pipe open, so the reader blocks long after the
timeout fired (`backend/sandbox.py:300-309`). `_kill_tree` therefore kills the process group on POSIX
and runs `taskkill /T /F` on Windows (`backend/sandbox.py:310-322`). This is the one guarantee the
sandbox makes on a platform where it can enforce nothing else.

Output is streamed line by line from a reader thread per stream, which is what keeps a chatty stderr
from deadlocking a quiet stdout (`backend/sandbox.py:325-343`). All three backends stream
(`backend/sandbox.py:271-280`). What does not stream is a container image pull, which happens before
the command runs and can legitimately take twenty minutes with a silent card
(`backend/sandbox.py:274-275`).

## Limits, where they exist

`_limits_applier` returns a `preexec_fn` that sets `RLIMIT_CPU` and the memory rlimit in the child
before exec, and returns `None` where the platform cannot do it (`backend/sandbox.py:110-126`).
`_can_unshare_net` probes `unshare --net true` rather than assuming a network namespace is available,
because that probe is the only portable way to cut the network off (`backend/sandbox.py:96-107`).

## Container settings

The image, the pull policy and the PID ceiling are read from the capability registry rather than the
environment: `container_image` defaulting to `python:3.12-slim`, `container_auto_pull` defaulting to
false, and `container_pids_limit` defaulting to 256 (`backend/sandbox.py:573`, `:585-587`). A missing
image is not fetched unless you allowed it, and the refusal names the runtime and the command to run
by hand (`backend/sandbox.py:663-666`). `--pids-limit` is passed on the command line
(`backend/sandbox.py:609`). Docker is tried before Podman and both are accepted, because which one a
person has is a matter of platform and taste, not trustworthiness (`backend/sandbox.py:554-560`).

## The audit ledger

One line per important action, in `~/.tacit/audit.jsonl` (`backend/config.py:58`,
`backend/audit.py:62-64`). The point is not debugging: a claim about what a system did should be
checkable afterwards, so each row records which backend ran something, under which mode, how much it
cost, and whether it was blocked (`backend/audit.py:3-10`).

A row is fixed-shape before any extras are merged: `ts`, `event`, `session`, `tool`, `backend`, `mode`,
`status`, `tokens` (`backend/audit.py:50-60`). `record` never raises, because the ledger must not break
a feature (`backend/audit.py:49`, `:65-66`).

Secrets are masked on the way in, at any depth. A dict under a key named `env`, `headers`, `token`,
`api_key` or `apikey` has every entry whose key looks secret replaced with `***`, a string under a key named
`command`, `url` or `endpoint` is passed through `mask_url`, and a string whose own key name looks like
a secret is replaced entirely (`backend/audit.py:22-44`). Lists are truncated to 50 entries on the way
in (`backend/audit.py:37`).

Reading it back: `recent` filters by session and event and returns the newest first, capped at 2000
rows per call (`backend/audit.py:21`, `:70-89`), `count` walks the file (`:92-97`), and `export`
serialises it (`:137-139`).

**There is no delete.** `clear()` is a rotation: the active ledger is renamed to
`audit.jsonl.<timestamp>`, the archived files stay and are listed by `archives()`, and the rotation
itself is written as the first entry of the new ledger, so the fact that the history was turned over is
on the record too (`backend/audit.py:100-134`). That is why `clear` cannot mean delete: the ledger is
append-only, and nothing in it is ever edited or removed (`backend/audit.py:116-122`).

## Snapshots

The undo half of the same promise. Before the first `write_file`, `edit_file` or `run_shell` of a turn,
the server checkpoints the workspace (`backend/agent.py:748-752`, `:867-871`), one capture per turn
rather than per step, and the result says a snapshot was taken.

A snapshot copies the project tree into `~/.tacit/checkpoints/<stamp>-<id>` (`backend/extras.py:155-180`,
`backend/config.py:50`), skipping paths whose project-relative parts are in `SNAPSHOT_SKIP`
(`backend/extras.py:170-174`, defined at `backend/config.py:206`) and files larger than `TACIT_SNAPSHOT_MAX_FILE` (`backend/extras.py:175`). The skip test
looks only at parts inside the project, because testing absolute parts would skip everything whenever
the project itself sat in a folder named `build/` or `checkpoints/` (`backend/extras.py:170-172`).

The agent has no snapshot tools. It cannot save its own work or rewind it; restore belongs to the
person (`backend/agent.py:624-629`).

Next: [data and privacy](data-and-privacy.md).

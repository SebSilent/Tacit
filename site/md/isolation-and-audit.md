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
nothing here is pretending otherwise (`backend/sandbox.py:595`-592`).

## What a run reports back

Every result has the same shape (`backend/sandbox.py:283-294`): `ok`, `backend`, `code`, `stdout`,
`stderr`, `blocked`, `notes`, `changed` with added, modified, removed and a count, `enforced`,
`requested`, and `duration_ms`.

`enforced` is computed per run, not per platform: a limit counts as enforced only when the platform
can apply it and the box asked for it (`backend/sandbox.py:577`-580`). `requested` records what the
settings asked for (`backend/sandbox.py:611`), so a reader can compare the two. Where they disagree,
a note says which guarantee is missing: network not cut off and proxy variables advisory only
(`backend/sandbox.py:587`-583`), the project not mountable read-only (`:584-585`), CPU or memory limits
not enforced (`:533-536`).

`changed` comes from scanning the project root before and after and diffing the two
(`backend/sandbox.py:567`, `:570`). That report is produced whatever the backend, which is why a
command that rewrote the tree on Windows still tells you what it rewrote.

## The timeout is enforced, not reported

`GRACE_SECONDS` is 10 (`backend/sandbox.py:297`), and a timed-out run returns exit code 124 with the
output it printed before the kill (`backend/sandbox.py:608`-603`).

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
false, and `container_pids_limit` defaulting to 256 (`backend/sandbox.py:641`, `:647-649`). A missing
image is not fetched unless you allowed it, and the refusal names the runtime and the command to run
by hand (`backend/sandbox.py:731`-728`). `--pids-limit` is passed on the command line
(`backend/sandbox.py:677`). Docker is tried before Podman and both are accepted, because which one a
person has is a matter of platform and taste, not trustworthiness (`backend/sandbox.py:622`-622`).

## The container backend, checked against a real daemon

These are hand results, not assertions: Docker Engine 29.8.1 with a WSL2 backend, run once, on this
machine. `--network none` really refused a connection to `1.1.1.1:53`. A read-only bind was rejected by
the kernel (`cannot create /workspace/...: Read-only file system`). A write from inside landed on the
host and appeared in the change report. `/tmp` was a `tmpfs`. `NoNewPrivs` read back `1`. `pids.max`
read back `64` when 64 was asked for, and `memory.max` read back `268435456` for `memory_mb=256`.
Nothing of the host filesystem was reachable. A 4-second timeout returned in 5.8 seconds with exit 124
and no surviving container in `docker ps -a`.

The project is bind-mounted rather than copied, so the snapshot, the restore and the change report
describe the files you actually have. Nothing is downloaded for you: a missing image is reported with
the exact `docker pull` command that would fetch it, and installing it is your call.

`test_isolation.py` covers the same ground against a stubbed runtime so the logic is tested on every
commit. No live-daemon test is shipped, which means the results above are not re-run automatically, and
a person with a daemon who wants them re-run is doing a useful thing by reporting the difference.

## Root elevation

`sudo` is off, and it is a capability only two of the three backends can even talk about. The switch
is honoured where the binary exists (`backend/sudo.py:315`), which means Linux and macOS.

| Backend | Supplies the saved password | What happens to a `sudo` command there |
|---|---|---|
| `none` | yes (`backend/sandbox.py:496`) | runs as root, and the audit line records how many commands it fed (`backend/sandbox.py:543`) |
| `tacit-micro` | no (`backend/sandbox.py:603`) | fails at once, with a note saying this backend does not supply it |
| `container` | no (`backend/sandbox.py:767`) | fails at once, and the note says why: `no_new_privileges` drops sudo's setuid bit before any password matters |

Both sandboxed backends leave stdin at `DEVNULL`, so an unanswered `sudo` fails immediately instead of
hanging until the turn timeout. Failing fast is not the same as being explained, which is why the note
exists: a model that reads only `sudo: a password is required` spends its next steps trying to type
into a pipe.

The command is rewritten to `sudo -S -p ''` before it runs (`backend/sudo.py:200`), so the password can
only arrive on stdin and sudo can never open a terminal and wait. One password line is fed per command
that actually asks, not one per turn: `sudo apt-get update && sudo apt-get install` runs two commands as
root, and a pipe closed after the first read leaves the second asking. `sudo` counted only in command position,
so `echo sudo` is not an elevation site and does not get the credential.

Three shapes are deliberately not fed, and each says so in the result instead of leaving sudo's own
complaint as the only evidence (`backend/sudo.py:255`). A `sudo` at the end of a pipe has stdin full of
the piped data, so a password fed there is read as a wrong password and the real one goes to the program:
`echo x | sudo tee /etc/hosts` fails, and the note says to write the file somewhere first and `sudo cp`
it into place. A `sudo` inside a quoted string is not rewritten either, because splicing `-p ''` into a
`'…'` changes what the string says, so `sh -c 'sudo …'` is left as typed and the working form is
`sudo sh -c '…'`. And a `sudo -n` asked sudo not to prompt at all; answering a question its writer chose
not to ask would put the credential somewhere it was not wanted.

Command position includes the words the shell stands in front of a command (`backend/sudo.py:53`), so
`for f in …; do sudo chmod …; done`, `if …; then sudo …; fi` and a `sudo` inside `$(…)` are sites. They
were not: those lines ran without a password and without a note, because the switch was on, and the model
read a bare `sudo: a terminal is required to read the password` as root having been withdrawn and told a
person so. The missing piece was never the capability, it was the sentence. A non-zero exit and a sudo
complaint in the output are both required before one is written (`backend/sudo.py:306`), so `grep sudo
backend/` succeeding does not get told about a password nobody asked for.

The prompt says elevation is armed only while it is (`backend/ai/prompts.py:27`), names the three shapes
that get nothing, and tells the model to read the note rather than probe: a failed command whose note says
the switch is ON was refused by sudo, not left without a password. A model that was never given the
capability is not told the password exists at all.

## The audit ledger

One line per important action, in `~/.tacit/audit.jsonl` (`backend/config.py:64`,
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
the server checkpoints the workspace (`backend/agent.py:801`-801`, `:876-903`), one capture per turn
rather than per step, and the result says a snapshot was taken.

A snapshot copies the project tree into `~/.tacit/checkpoints/<stamp>-<id>` (`backend/extras.py:155-180`,
`backend/config.py:50`), skipping paths whose project-relative parts are in `SNAPSHOT_SKIP`
(`backend/extras.py:170-174`, defined at `backend/config.py:222`) and files larger than `TACIT_SNAPSHOT_MAX_FILE` (`backend/extras.py:175`). The skip test
looks only at parts inside the project, because testing absolute parts would skip everything whenever
the project itself sat in a folder named `build/` or `checkpoints/` (`backend/extras.py:170-172`).

The agent has no snapshot tools. It cannot save its own work or rewind it; restore belongs to the
person (`backend/agent.py:640`-641`).

Next: [data and privacy](data-and-privacy.md).

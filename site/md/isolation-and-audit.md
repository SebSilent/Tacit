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

Selection happens at `backend/sandbox.py:327-386`. An unknown backend, one with no adapter, and one
that is unavailable each return an error rather than falling back
(`backend/sandbox.py:348-355`).

## What the platform can actually enforce

`mechanism()` answers which primitive would be used right now (`backend/sandbox.py:62-71`):
`container` if a container runtime is present and chosen, `bubblewrap` or `posix-limits` on Linux,
`sandbox-exec` or `posix-limits` on macOS, and `none` on Windows.

Under bubblewrap the command also gets a private PID namespace
(`backend/sandbox.py:272-279`): it cannot see, name or signal any other process of
the person's account - least of all the shell that launched the server or the
server itself. Without it a wrapped command could still `kill` anything the user
owns, which is filesystem containment with the front door open; `ps` and
friends inside a sandboxed command therefore report only the sandbox's own
processes. One route is deliberate and narrow: when the sudo switch is on and a
command carries a feedable sudo, it runs on the host path instead of the
sandbox, because a wrapper that strips setuid can never gain root; the switch is
the standing yes for exactly that command, and the result says so; every
non-sudo command of the same turn stays contained.

The walls are also told to the model. The standing prompt carries a sandbox
note derived at build time from the same primitives `run()` uses
(`backend/sandbox.py` `prompt_note`): a wrapped run says what its walls are
(read-only system directories, its own PID namespace, the private `/tmp`), a
`none` backend says plainly that commands have the full reach of the user, and
a selected-but-broken backend is named as refusing runs. Nothing in the prompt
claims isolation the code would not enforce.

`platform_capabilities()` turns that into a flag set (`backend/sandbox.py:74-95`). On Windows the
answer is a timeout and nothing else: `cpu_limit`, `memory_limit`, `file_size_limit`, `network`,
`readonly_project`, `private_tmp` and `process_isolation` are all false, because `IS_POSIX` is false
(`backend/sandbox.py:45`) and no base-OS equivalent exists (`backend/sandbox.py:25-27`). A container
runtime enforces every one of those except the file size limit and overlay writes, on every platform
including Windows, which is the reason it is offered at all (`backend/sandbox.py:77-83`).

Nothing is faked to fill that gap. The isolation ladder is stated plainly in the module header
(`backend/sandbox.py:21-27`), and a run on Windows carries a note saying that this system offers no
isolation primitive Tacit can use, that the timeout and the change report still apply, and that
nothing here is pretending otherwise (`backend/sandbox.py:732-731`).

## What a run reports back

Every result has the same shape (`backend/sandbox.py:393-400`): `ok`, `backend`, `code`, `stdout`,
`stderr`, `blocked`, `notes`, `changed` with added, modified, removed and a count, `enforced`,
`requested`, and `duration_ms`.

`enforced` is computed per run, not per platform: a limit counts as enforced only when the platform
can apply it and the box asked for it (`backend/sandbox.py:713-719`). `requested` records what the
settings asked for (`backend/sandbox.py:748-750`), so a reader can compare the two. Where they disagree,
a note says which guarantee is missing: network not cut off and proxy variables advisory only, the
project not mountable read-only (`backend/sandbox.py:724-724`), or CPU and memory limits not enforced
(`:732-735`).

`changed` comes from scanning the project root before and after and diffing the two
(`backend/sandbox.py:738`, `:716-717`). That report is produced whatever the backend, which is why a
command that rewrote the tree on Windows still tells you what it rewrote.

## Timeout and cancellation are enforced, not reported

`GRACE_SECONDS` is 10 (`backend/sandbox.py:407`). A timeout returns exit code 124 and a cancellation
returns 130, both with the output collected before the stop (`backend/sandbox.py:479-527`).

Captured and streamed calls share `_run_streamed`, which supervises the root and both pipes against
one deadline (`backend/sandbox.py:458-533`, `:540-565`). Windows containment uses a per-command Job
Object through `proctools.spawn`; POSIX uses a per-command process group through `start_new_session`
(`backend/proctools.py:118-173`, `backend/sandbox.py:560-561`). Cancellation follows the same path as
a timeout rather than abandoning a command that still owns a pipe.

Killing only the direct child is not enough. On Windows a `.bat` or `cmd /c` wrapper can leave
grandchildren running with the captured pipe open. Root exit therefore does not skip cleanup: the
supervisor keeps waiting while descendants hold a pipe, and timeout or cancellation terminates the
durable Job Object or POSIX group even after the root is gone (`backend/sandbox.py:410-413`,
`backend/sandbox.py:484-514`, `backend/proctools.py:192-225`). Windows uses `TerminateJobObject` first and falls back to
`taskkill /T /F` only for a live root without a usable job.

Output is drained in 8192-byte binary chunks by a reader thread per stream, then incrementally decoded
with replacement for invalid input, not split into lines (`backend/sandbox.py:420-451`). This keeps a
chatty stderr from deadlocking a quiet stdout, and a viewer callback cannot stop drainage. All three
backends use this supervisor (`backend/sandbox.py:361-386`). A container image inspect or pull happens
before the command and does not stream, but both run under a deadline and the turn's stop flag, so a
cancel during a pull returns promptly instead of waiting it out (`backend/sandbox.py:834-881`,
`:955-971`).

A container timeout or cancellation also kills the daemon-side container. The kill is verified by an
inspect afterwards, not by the kill command's own exit code, and the result says it was killed only
when the runtime confirms the container is down; otherwise the note reports that the runtime did not
confirm it (`backend/sandbox.py:913-929`, `:1014-1015`). The container's name is registered with the
long-lived parent before the client starts, so a hard worker death leaves a row the parent (or the
next boot's sweep) can settle by name; an unconfirmed cleanup keeps the row for a retry
(`backend/sandbox.py:985-1006`, `backend/proctools.py:673-743`).

## Limits, where they exist

`_limits_applier` returns a `preexec_fn` that sets `RLIMIT_CPU` and the memory rlimit in the child
before exec, and returns `None` where the platform cannot do it (`backend/sandbox.py:112-135`).
`_can_unshare_net` probes `unshare --net true` rather than assuming a network namespace is available,
because that probe is the only portable way to cut the network off (`backend/sandbox.py:98-109`).

## Container settings

The image, the pull policy and the PID ceiling are read from the capability registry rather than the
environment: `container_image` defaulting to `python:3.12-slim`, `container_auto_pull` defaulting to
false, and `container_pids_limit` defaulting to 256 (`backend/sandbox.py:805-820`). A missing image is
not fetched unless you allowed it, and the refusal names the runtime and the command to run by hand
(`backend/sandbox.py:970-977`). `--pids-limit` is passed on the command line
(`backend/sandbox.py:811-815`). Docker is tried before Podman and both are accepted, because which one
a person has is a matter of platform and taste, not trustworthiness (`backend/sandbox.py:763-767`).

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
is honoured where the binary exists (`backend/sudo.py:329`), which means Linux and macOS.

| Backend | Supplies the saved password | What happens to a `sudo` command there |
|---|---|---|
| `none` | yes (`backend/sandbox.py:591-625`) | runs as root, and the audit line records how many commands it fed (`backend/sandbox.py:670-669`) |
| `tacit-micro` | no (`backend/sandbox.py:692-697`) | fails at once, with a note saying this backend does not supply it |
| `container` | no (`backend/sandbox.py:1020-1022`) | fails at once, and the note says why: `no_new_privileges` drops sudo's setuid bit before any password matters |

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

One line per important action, in `~/.tacit/audit.jsonl` (`backend/config.py:116`,
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
the server checkpoints the workspace (`backend/agent.py:888-892`, `:985-1011`, `:3273-3278`), one
capture per turn rather than per step, and the result says a snapshot was taken.

A snapshot copies the project tree into `~/.tacit/checkpoints/<stamp>-<id>` (`backend/extras.py:425-450`,
`backend/config.py:102`), skipping paths whose project-relative parts are in `SNAPSHOT_SKIP`
(`backend/extras.py:433-444`, defined at `backend/config.py:320-321`) and files larger than
`TACIT_SNAPSHOT_MAX_FILE` (`backend/extras.py:445-446`). The skip test looks only at parts inside the
project, because testing absolute parts would skip everything whenever the project itself sat in a
folder named `build/` or `checkpoints/` (`backend/extras.py:433-444`).

The agent has no snapshot tools. It cannot save its own work or rewind it; restore belongs to the
person (`backend/agent.py:717-740`).

Next: [data and privacy](data-and-privacy.md).

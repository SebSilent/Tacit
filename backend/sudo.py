"""Opt-in root elevation for the agent's shell commands, on the platforms that have sudo.

Off by default, like everything optional here. Turned on it does one thing: when a command
the agent runs reaches `sudo` at a command position, the stored password is fed to it
instead of the command dying on "no tty present and no askpass program specified". Without
that, an agent told to install a package spends its steps discovering it cannot type into a
terminal it does not have.

The mechanism is `sudo -S`, which reads the password from stdin, and stdin is a pipe the
harness writes and then closes. Not `SUDO_ASKPASS`, and not the password in the command
line or in the child's environment: a command line shows up in `ps`, in the transcript and
in every shell card on screen, and an environment variable shows up the moment the agent
runs `env`. With `-S` the secret goes to one reader and nowhere else.

Two decisions worth their explanation:

`-p ''` is added with `-S`. sudo sends its prompt to stderr even in `-S` mode, so without
an empty prompt the result of every elevated command carries a `[sudo] password for
someone:` line that the model reads as unexplained output.

The password is written once per elevation site, not once per command. `sudo a && sudo b`
in one line is two sudo processes and each reads exactly one line from the shared stdin;
feeding one line would leave the second one sitting on EOF. Extra lines are not a leak -
whatever the last command inherits is an exhausted pipe, the same as DEVNULL was before.
A site that carries `-n` is not fed and not touched: that command asked sudo not to
prompt, and answering a question nobody asked would put the secret somewhere else.

What this does not do is pretend the switch is safe. It hands the agent root on this
machine for the length of a turn, and the person turning it on is the one who decided that
was worth the steps it saves. The panel says so, the prompt block tells the model which
commands ran as root, and the ledger records the site count on every command that used it.
"""

import os
import re

from . import config, secrets as _store

NAME = "sudo"
POSIX_OS = ("Linux", "Darwin")

# Words that sit in front of a command without becoming it. Mirrors the guardrail in
# `backend/agent.py:269` so both agree about what a command position is; `sudo` itself is
# deliberately absent, because here it is the thing being looked for.
_WRAPPERS = {"env", "command", "nohup", "time", "nice", "xargs", "doas"}

# A segment is one command: split on the shell's own sequencing characters.
_SEGMENT_BREAK = re.compile(r"[&|;\n]+")
_TOKEN = re.compile(r"[^\s()<>`|;&]+")
_ENV_ASSIGN = re.compile(r"^\w+=")


def _segments(text: str):
    """(start, end) spans of each command in a line, the same split the version-control
    guard uses (`backend/agent.py:281`), but keeping offsets so a rewrite can be exact."""
    pos = 0
    for m in _SEGMENT_BREAK.finditer(text):
        yield pos, m.start()
        pos = m.end()
    yield pos, len(text)


def _command_word(text: str, start: int, end: int):
    """The span of the word that would be executed, or None.

    Leading wrappers and `VAR=value` prefixes are stepped over, exactly as the
    version-control guard steps over them, so `sudo git push` and `sudo apt install`
    are both found at the position that matters.
    Scanning the whole string with pos and endpos, not a slice: the first draft sliced
    the segment and passed the offset anyway, so every segment after a `&&` lost its
    first word and `echo done && sudo reboot` was reported as containing no sudo."""
    for m in _TOKEN.finditer(text, start, end):
        w = m.group().strip("'\"")
        if not w:
            continue
        low = os.path.basename(w).lower()
        if low in _WRAPPERS or _ENV_ASSIGN.match(w):
            continue
        return m.start(), m.end(), low
    return None


def _options(rest: str) -> tuple[bool, bool]:
    """(reads_stdin, asks_nothing) for the option run that follows the word `sudo`.

    Scanning stops at the first word that is not an option, which is where sudo's own
    options end. Only the capital `S` is the stdin flag: lowercase `-s` asks sudo for a
    shell, and feeding a password to that would put the secret on the shell's stdin
    instead of on sudo's."""
    reads_stdin = asks_nothing = False
    for m in re.finditer(r"\S+", str(rest or "")):
        w = m.group().strip("'\"")
        if w.startswith("--"):
            name = w[2:].split("=", 1)[0]
            if name == "stdin":
                reads_stdin = True
            elif name == "non-interactive":
                asks_nothing = True
            continue
        if w.startswith("-") and len(w) > 1:
            if "S" in w[1:]:
                reads_stdin = True
            if "n" in w[1:]:
                asks_nothing = True
            continue
        break
    return reads_stdin, asks_nothing


def sites(command: str) -> list[dict]:
    """Every `sudo` reached at a command position, with the flags it already carries.

    `sudo` as an argument (`echo sudo`, `grep sudo file`) is not a site, because the
    first word of the segment is `echo` or `grep`. An absolute path to it is a site."""
    text = str(command or "")
    found = []
    for start, end in _segments(text):
        word = _command_word(text, start, end)
        if not word:
            continue
        ws, we, low = word
        if low != "sudo":
            continue
        reads_stdin, asks_nothing = _options(text[we:end])
        found.append({"start": ws, "end": we, "stdin": reads_stdin,
                      "noninteractive": asks_nothing})
    return found


def uses_sudo(command: str) -> bool:
    return bool(sites(command))


def prepare(command: str) -> dict:
    """What to actually run, and what to write to it.

    Returns ``{"command", "stdin", "sites", "elevated"}``. The command is unchanged when
    sudo is not armed, so a line that mentions sudo still runs and fails the way it always
    did rather than being silently rewritten; `sites` is still counted so the caller can
    say why it failed.
    """
    text = str(command or "")
    found = sites(text)
    out = {"command": text, "stdin": None, "sites": len(found), "fed": 0,
           "elevated": False}
    if not found or not armed():
        return out
    try:
        password = _store.get(NAME)
    except _store.SecretError as exc:
        # The store is there and will not open. Say so in the result rather than
        # running the command without a password and letting "sorry, try again" be the
        # only evidence, three layers from the file that actually changed.
        out["error"] = str(exc)
        return out
    if not password:
        return out

    # `-n` means the command explicitly asked sudo not to prompt. Overriding that with
    # `-S` would answer a question the writer of the command chose not to ask, and the
    # line fed for it would land on the program's stdin instead.
    feed = [s for s in found if not s["noninteractive"]]
    # Rewrite back to front so earlier offsets stay valid.
    new = text
    for s in reversed(feed):
        insert = "" if s["stdin"] else " -S -p ''"
        new = new[:s["end"]] + insert + new[s["end"]:]
    out["command"] = new
    out["stdin"] = (password + "\n") * len(feed)
    out["fed"] = len(feed)
    out["elevated"] = bool(feed)
    return out


# ── the switch ─────────────────────────────────────────────────────────────

def available() -> bool:
    """Whether this platform has sudo to elevate with.

    Read from `config.OS`, which `TACIT_OS` may set (`backend/config.py:11`), so the
    behaviour can be tested from a Windows box the same way the shell description is."""
    return str(config.OS) in POSIX_OS


def enabled() -> bool:
    """The pref, and only where it could mean anything. A pref left over from a Linux
    install read on Windows stays off here rather than offering elevation that cannot
    work."""
    return bool(available() and config.prefs().get("sudoEnabled"))


def has_password() -> bool:
    try:
        return _store.has(NAME)
    except Exception:   # noqa: BLE001  a broken store must not break the settings panel
        return False


def armed() -> bool:
    """On, and able to supply something. The two are separate because a person can turn
    the switch on before typing the password, and the honest state then is "on, nothing to
    feed", not "ready"."""
    return bool(enabled() and has_password())


def set_enabled(on: bool) -> None:
    config.save_prefs({"sudoEnabled": bool(on)})


def set_password(value: str) -> bool:
    """Save (or clear, for an empty value). Returns whether a secret is on file after."""
    _store.put(NAME, value)
    return has_password()


def clear_password() -> None:
    _store.delete(NAME)


def status() -> dict:
    """Everything the panel needs, and never the password.

    `protection` is the honest sentence about what the store is: a file the same user can
    read is not a vault, and a settings panel that implied otherwise would be selling the
    wrong thing."""
    return {
        "available": available(),
        "os": str(config.OS),
        "enabled": enabled(),
        "armed": armed(),
        "has_password": has_password(),
        "saved_at": _store.saved_at(NAME) if has_password() else 0,
        "key_file": str(_store.key_path()),
        "protection": (
            "Encrypted at rest, not protected from this user. The key sits beside the "
            "store, so anything that can read both as this account, including the agent, "
            "can recover the password."),
    }

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

# Shell words that stand where a command would, without being one. They were not skipped
# at all, so `for f in …; do sudo chmod …; done` and `if …; then sudo systemctl …; fi`
# each had a sudo that Tacit never saw: no `-S` was added, nothing was fed, and the result
# carried no sudo note either, because the switch was on. The model read a bare
# "a terminal is required to read the password" with nothing explaining it and reported
# back that root had been taken away.
_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for",
             "in", "case", "esac", "select", "{", "}", "("}

# A segment is one command: split on the shell's own sequencing characters, plus the two
# ways a command hides inside another one. `$(sudo …)` and backticks are their own command
# positions and inherit stdin, so they are feedable, and were silently missing.
_SEGMENT_BREAK = re.compile(r"[&|;\n]+|\$\(|`|\)")
_TOKEN = re.compile(r"[^\s()<>`|;&]+")
_ENV_ASSIGN = re.compile(r"^\w+=")


def _quote_spans(text: str) -> list[tuple[int, int]]:
    """Regions the shell would treat as literal, single or double quoted.

    Backslash escapes count inside double quotes and not inside single ones, which is the
    difference between `"sudo \\" x"` ending the span and `'sudo \\' x"` not.
    An unbalanced quote runs to the end of the line, because that is what the shell does
    with it, and a site inside it must not be rewritten either."""
    spans, start, quote, escaped = [], None, "", False
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if quote == '"' and ch == "\\":
            escaped = True
            continue
        if quote:
            if ch == quote:
                spans.append((start, i))
                quote = ""
        elif ch in "'\"":
            quote, start = ch, i + 1
    if quote:
        spans.append((start, len(text)))
    return spans


def _segments(text: str):
    """(start, end, separator-before) for each command in a line.

    The same split the version-control guard uses (`backend/agent.py:281`), keeping offsets
    so a rewrite can be exact, and carrying the characters that ended the previous command
    because they decide what this one's stdin is: `a || sudo b` inherits the shell's stdin
    and can be fed, `a | sudo b` has stdin full of `a`'s output and cannot."""
    pos = 0
    sep = ""
    for m in _SEGMENT_BREAK.finditer(text):
        yield pos, m.start(), sep
        pos = m.end()
        sep = m.group()
    yield pos, len(text), sep


def _piped(sep: str) -> bool:
    """Whether the separator handing off to this segment is an input pipe.

    `|&` pipes stderr too, still a pipe. `||` is a conditional and leaves stdin alone,
    so it is spotted by the absence of a doubled bar rather than by counting."""
    return "|" in sep and "||" not in sep


def _command_word(text: str, start: int, end: int):
    """The span of the word that would be executed, or None.

    Leading wrappers, shell keywords and `VAR=value` prefixes are stepped over, so
    `sudo git push`, `sudo apt install` and `then sudo systemctl restart x` are all found
    at the position that matters. `!` is stripped from the front: `!sudo x` is a sudo.
    Scanning the whole string with pos and endpos, not a slice: the first draft sliced
    the segment and passed the offset anyway, so every segment after a `&&` lost its
    first word and `echo done && sudo reboot` was reported as containing no sudo."""
    for m in _TOKEN.finditer(text, start, end):
        w = m.group().strip("'\"").lstrip("!")
        if not w:
            continue
        low = os.path.basename(w).lower()
        if low in _WRAPPERS or low in _KEYWORDS or _ENV_ASSIGN.match(w):
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
    first word of the segment is `echo` or `grep`. An absolute path to it is a site.
    A site whose stdin is already full is marked `piped`: it is still a sudo the person
    is trying to run, and the caller has to say why it did not get the password."""
    text = str(command or "")
    spans = _quote_spans(text)
    found = []
    for start, end, sep in _segments(text):
        word = _command_word(text, start, end)
        if not word:
            continue
        ws, we, low = word
        if low != "sudo":
            continue
        reads_stdin, asks_nothing = _options(text[we:end])
        found.append({"start": ws, "end": we, "stdin": reads_stdin,
                      "noninteractive": asks_nothing, "piped": _piped(sep),
                      "quoted": any(s <= ws < e for s, e in spans)})
    return found


def uses_sudo(command: str) -> bool:
    return bool(sites(command))


def mentions(command: str) -> int:
    """How many times the word `sudo` appears at all, in any position.

    Looser than `sites` on purpose, and only ever used to decide whether the result owes
    the model an explanation. `sh -c "sudo apt update"` has no site, because rewriting
    inside a quoted string would change what the string says, and `grep sudo log` has no
    site either; the first is a command the model meant to run as root and the second is not,
    and the difference is drawn by whether sudo actually complained, not by this count."""
    return len(re.findall(r"\bsudo\b", str(command or "")))


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
           "elevated": False, "piped": sum(1 for s in found if s["piped"]),
           "noint": sum(1 for s in found if s["noninteractive"]),
           "quoted": sum(1 for s in found if s["quoted"]),
           "mentions": mentions(text), "armed": armed()}
    if not found or not out["armed"]:
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
    feed = [s for s in found
            if not s["noninteractive"] and not s["piped"] and not s["quoted"]]
    # A piped-into sudo is left alone, and that is the whole reason it is left alone:
    # `echo x | sudo tee /etc/hosts` gives sudo a stdin full of `x`, so `-S` would read
    # the piped data as the password, report "1 incorrect password attempt", and then hand
    # the real password to tee as its input. Not feeding it costs one failed command;
    # feeding it costs the secret's appearance in a program that was never meant to see it.
    if not feed:
        # Every sudo in the line was one of the shapes above. Return before writing a
        # password of length zero into stdin: an empty string and no string are different
        # things to a child process, and the line has to run exactly as it was typed.
        return out
    # Rewrite back to front so earlier offsets stay valid.
    new = text
    for s in reversed(feed):
        insert = "" if s["stdin"] else " -S -p ''"
        new = new[:s["end"]] + insert + new[s["end"]:]
    out["command"] = new
    out["stdin"] = (password + "\n") * len(feed)
    out["fed"] = len(feed)
    out["elevated"] = True
    return out


def explain(run: dict, code: int = 0, output: str = "") -> str:
    """What to tell the model about a sudo that Tacit did not feed, or "" for nothing.

    The case that produced the bug report: the switch ON, a sudo in the command, nothing
    supplied. Nothing used to be said about it, so the only evidence in the transcript was
    sudo's own complaint, and the model wrote back that root had been taken away from it.
    It had not: it had run a shape Tacit cannot feed, in a loop, or behind a pipe. A note
    that names the shape and the rewrite is the difference between a corrected command and
    a session spent arguing about a capability it still has.

    It stays quiet when there is nothing to explain: no sudo in the command, the switch off
    (which `backend/agent.py` states on every backend already), the command fed, or a
    command that ran fine without being fed.
    """
    if run.get("fed") or not run.get("armed"):
        return ""
    if not (run.get("mentions") or run.get("sites")):
        return ""
    if code == 0 and not _COMPLAINT.search(str(output or "")):
        return ""
    if run.get("piped"):
        return ("sudo: the switch is ON and Tacit did not feed this one, because its sudo "
                "sits at the end of a pipe. Its stdin is the piped data, so a password fed "
                "here would be read as a wrong password and the real one would go to the "
                "program. Write the content to a file, then elevate the copy: "
                "`printf '%s\\n' … > /tmp/x && sudo cp /tmp/x /etc/file`. Root has not been "
                "withdrawn.")
    # Both halves of this branch are the same mistake seen from two sides: a sudo the
    # scanner found inside quotes, and a sudo the scanner could not find because the segment
    # starts at `sh`. Rewriting the first would splice `-p ''` into a single-quoted string
    # and change what it says, so neither is touched and both are explained.
    if run.get("quoted") or run.get("mentions", 0) > run.get("sites", 0):
        return ("sudo: the switch is ON and Tacit did not feed this one, because its sudo is "
                "inside a quoted string, where adding the flags would change what the string "
                "says. Put the sudo outside the quotes: `sudo sh -c '…'`, not "
                "`sh -c 'sudo …'`. Root has not been withdrawn.")
    if run.get("noint"):
        return ("sudo: the switch is ON and Tacit did not feed this one, because every sudo "
                "in it carried `-n`, which asks sudo not to prompt at all. Drop `-n` to have "
                "the saved password supplied. Root has not been withdrawn.")
    # Reached from one place only: the switch is on and the store holds an empty string, so
    # `armed()` is true and there is nothing to write. A sudoers refusal or a wrong password
    # lands on the fed path instead, with sudo's own answer beside it.
    return ("sudo: the switch is ON and this sudo sits where Tacit can feed it, but nothing "
            "was supplied, because the saved password is empty. Save one under Settings > "
            "Tools, Sudo, or turn the switch off. Nobody has withdrawn root from you.")


# What a sudo that wanted a password and did not get one sounds like, from either end of
# the pipe. A command that merely mentions the word sudo (`grep sudo /var/log/auth.log`)
# and exits 0 does not match, and gets no note.
_COMPLAINT = re.compile(
    r"sudo:\s|a password is required|no password was provided|incorrect password|"
    r"terminal is required|not in the sudoers file|may not run|must be root|"
    r"operation not permitted|permission denied|denied by policy|ttyprompt",
    re.I)

# The narrower half of that: lines only sudo itself writes. Used where the claim being
# gated is about sudo and nothing else is allowed to trigger it, because `permission
# denied` on its own is what a hundred unrelated failures say, and a sentence about a
# password would be a false accusation about a command that never asked for one.
_SUDO_ONLY = re.compile(
    r"sudo:|a password is required|no password was provided|incorrect password|"
    r"terminal is required|not in the sudoers file|sudoers file",
    re.I)


def complained(output: str) -> bool:
    """Whether something in the output is sudo talking, not the shell failing elsewhere."""
    return bool(_SUDO_ONLY.search(str(output or "")))


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

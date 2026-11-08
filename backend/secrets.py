"""A small secret store, encrypted at rest with nothing outside the standard library.

Provider keys live in `~/.tacit/.env` as plain text and that is fine for them: a key is
useless without the endpoint it belongs to, the file is the convention people expect, and
the interface masks it. A sudo password is different in kind. It is the credential that
lets a command run as root on the machine the person is sitting at, and the whole point of
storing it is that nobody has to be present when it is used. Keeping that in `prefs.json`
would put it in a file the backend reads constantly, that people paste into bug reports,
and that backup tools sweep up as ordinary configuration.

So: each value is encrypted under a key held in its own file (`secret.key`), written
`0600` where the platform honours that, and the store never hands a value back to anything
that prints it. The construction is HMAC-SHA256 in counter mode as the stream, with an
encrypt-then-MAC tag over the nonce and the ciphertext, and separate subkeys for the two
roles. That is stdlib-only because `requirements.txt` carries three packages and a fourth
binary wheel would break the property the installers are built on.

What this is not, stated because a docstring that oversells it would be the worst part of
this file: there is no OS keychain here and no passphrase, because a passphrase would have
to be typed at every use and the feature exists to avoid exactly that. Anyone who can read
both files, or who can run as the same user, can recover the password. The agent runs as
that user. Its file tools are refused these two names (`backend/agent.py:24`), which stops
the casual read and nothing determined. The store keeps the credential out of plaintext
configuration, out of transcripts and out of a prefs backup. It does not keep it from the
machine's own user, and nobody should turn this on who needs that.
"""

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path

from . import config

MARK = "v1"
NONCE_BYTES = 16
TAG_BYTES = 32

_LOCK = threading.Lock()


# ── the key file ───────────────────────────────────────────────────────────

def key_path() -> Path:
    """Resolved at call time, not cached at import: `tests/helpers.py:9-13` exists
    because a cached path once let a redirected test write into the real ~/.tacit."""
    return Path(config.SECRET_KEY_FILE)


def store_path() -> Path:
    return Path(config.SECRETS_FILE)


KEY_BYTES = 32
_UNUSABLE = (" exists but is not a usable key. Restore it from a backup, or delete it "
             "and re-enter every stored secret; nothing in between is safe to guess.")


def _make_key() -> bytes:
    key = os.urandom(KEY_BYTES)
    p = key_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # os.open with the mode in the create call, so there is no window in which the
    # file exists and is world-readable. The umask can still widen it on some
    # systems, hence the explicit chmod after.
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # Hex, not raw bytes. The first draft wrote the 32 random bytes straight out
        # and read them back with `.strip()`, and a key that happened to end in a byte
        # the strip treats as whitespace came back 31 bytes long: the store then
        # refused to open, on about one install in twenty-five, with nothing wrong but
        # the luck of the key.
        os.write(fd, (key.hex() + "\n").encode("ascii"))
    finally:
        os.close(fd)
    try:
        os.chmod(str(p), 0o600)
    except OSError:
        pass   # Windows has no per-file mode here; the docstring says what that means
    return key


def _master() -> bytes:
    p = key_path()
    try:
        raw = p.read_text(encoding="ascii", errors="strict").strip()
    except FileNotFoundError:
        return _make_key()
    except (OSError, UnicodeDecodeError):
        raise ValueError(f"{p}{_UNUSABLE}") from None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        raise ValueError(f"{p}{_UNUSABLE}") from None
    if len(key) < KEY_BYTES:
        # Present but too short to be one: a truncated write, or something that is not
        # ours. Refuse rather than run a 4-byte key, because deleting the file loses
        # every secret already in the store and this is the one message that says so.
        raise ValueError(f"{p}{_UNUSABLE}")
    return key


def _subkeys(master: bytes) -> tuple[bytes, bytes]:
    """Two roles, two keys. A single key doing both the stream and the tag is the
    shape that turns a subtle reuse into an actual break, and splitting them costs
    two HMAC calls."""
    enc = hmac.new(master, b"tacit-secret-enc-" + MARK.encode(), hashlib.sha256).digest()
    mac = hmac.new(master, b"tacit-secret-mac-" + MARK.encode(), hashlib.sha256).digest()
    return enc, mac


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


# ── the primitive ──────────────────────────────────────────────────────────

def encrypt(plaintext: str, master: bytes | None = None) -> str:
    """`v1:<base64 of nonce | ciphertext | tag>`. Never deterministic: a fresh nonce
    per call, so saving the same password twice does not produce a matching pair for
    anyone comparing rows in the store."""
    master = master or _master()
    enc_key, mac_key = _subkeys(master)
    nonce = os.urandom(NONCE_BYTES)
    data = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()[:TAG_BYTES]
    return MARK + ":" + base64.b64encode(nonce + ct + tag).decode("ascii")


class SecretError(RuntimeError):
    """The token is ours but does not open: wrong key, edited file, truncated copy."""


def decrypt(token: str, master: bytes | None = None) -> str:
    master = master or _master()
    enc_key, mac_key = _subkeys(master)
    text = str(token or "")
    if not text.startswith(MARK + ":"):
        raise SecretError("stored value is not in the format this store writes")
    try:
        blob = base64.b64decode(text[len(MARK) + 1:], validate=True)
    except Exception as exc:   # noqa: BLE001
        raise SecretError(f"stored value will not decode: {exc}") from exc
    if len(blob) < NONCE_BYTES + TAG_BYTES:
        raise SecretError("stored value is truncated")
    nonce, ct, tag = blob[:NONCE_BYTES], blob[NONCE_BYTES:-TAG_BYTES], blob[-TAG_BYTES:]
    want = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()[:TAG_BYTES]
    if not hmac.compare_digest(tag, want):
        # The one case where guessing is worse than stopping: a tag that does not
        # match is a rewritten file or a different key, and returning whatever the
        # keystream happens to produce would hand back a wrong password that fails
        # as "permission denied" three layers away from here.
        raise SecretError("stored value does not authenticate: the key file or the "
                          "store changed. Re-enter the secret.")
    return bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct)))).decode(
        "utf-8", errors="replace")


# ── the store ──────────────────────────────────────────────────────────────

def _read() -> dict:
    return config.read_json(store_path(), {})


def put(name: str, value: str) -> None:
    """Encrypt and save. An empty value deletes rather than storing an empty secret,
    because "no password" and "a password that is the empty string" are different
    answers and only one of them can be typed into the panel by accident."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("no secret name given")
    with _LOCK:
        rows = _read()
        if value is None or str(value) == "":
            rows.pop(name, None)
        else:
            rows[name] = {"value": encrypt(str(value)), "saved": int(time.time())}
        config.write_json(store_path(), rows)
        _tighten(store_path())


def get(name: str) -> str:
    """The plaintext, or "" when there is no such secret.

    A value that will not open raises rather than returning "": a wrong password sent to
    sudo reads as a failed authentication, and the person then tries the password instead
    of the file that was never the problem."""
    row = _read().get(str(name or "").strip())
    if not row:
        return ""
    raw = row.get("value") if isinstance(row, dict) else row
    return decrypt(str(raw))


def has(name: str) -> bool:
    row = _read().get(str(name or "").strip())
    return bool(row and (row.get("value") if isinstance(row, dict) else row))


def saved_at(name: str) -> int:
    row = _read().get(str(name or "").strip())
    if isinstance(row, dict):
        try:
            return int(row.get("saved") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def delete(name: str) -> bool:
    with _LOCK:
        rows = _read()
        had = str(name or "").strip() in rows
        rows.pop(str(name or "").strip(), None)
        config.write_json(store_path(), rows)
        _tighten(store_path())
    return had


def names() -> list[str]:
    """Which secrets exist. Values are never part of this answer, so a status route can
    call it without any chance of printing one."""
    return sorted(_read().keys())


def _tighten(path: Path) -> None:
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass

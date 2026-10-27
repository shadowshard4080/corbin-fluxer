"""Encryption at rest for the files that hold what people said or are.

Memories (per user and per server), warnings and reminders go through :func:`read_json` / :func:`write_json`. With a key configured
(``DATA_KEY`` or ``DATA_KEY_FILE``, a Fernet key: AES-128-CBC + HMAC-SHA256) they are
written as ``MAGIC`` followed by the token; without one, as plain JSON.

What this protects, and what it doesn't: a copy of the files, a
backup, or someone browsing the disk gets ciphertext. The bot has to
read memories to use them, so the key lives on the same server, in
``.env`` (or the file ``DATA_KEY_FILE`` names); anyone who controls the
running server can read them.

**A file we can't decrypt is an error, never "empty".** Every store
here used to treat an unreadable file as a fresh start and would then
save over it: with a missing or wrong key that destroys every memory.
:class:`DataKeyError` is raised instead, and ``bot.py`` checks the key
against the data at boot (:func:`check_dir`) and refuses to start.

Plain files are still read, so turning encryption on needs no
migration: each file is encrypted the next time it is saved, and
``tools/encrypt_data.py`` does them all at once.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional

#: Marks an encrypted file. Plain JSON can never start with it.
MAGIC = b"CORBIN-ENC1\n"


class DataKeyError(RuntimeError):
    """The data is encrypted and the key is missing or wrong."""


_fernet: Any = None


def generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")


def configure(key: Optional[str]) -> None:
    """Set (or with an empty key, clear) the key. Raises on a bad key or
    a missing ``cryptography`` package: a key that was asked for and
    can't be used must not quietly mean "plain text"."""
    global _fernet
    key = (key or "").strip()
    if not key:
        _fernet = None
        return
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise DataKeyError("DATA_KEY is set but the 'cryptography' "
                           "package isn't installed (pip install "
                           "cryptography)") from exc
    try:
        _fernet = Fernet(key.encode("ascii"))
    except Exception as exc:
        raise DataKeyError("DATA_KEY isn't a valid key; make one "
                           "with tools/encrypt_data.py --new-key") from exc


def enabled() -> bool:
    return _fernet is not None


def is_encrypted(raw: bytes) -> bool:
    return raw.startswith(MAGIC)


def encode(obj: Any, **json_kwargs: Any) -> bytes:
    text = json.dumps(obj, **json_kwargs).encode("utf-8")
    if _fernet is None:
        return text
    return MAGIC + _fernet.encrypt(text)


def decode(raw: bytes, label: str = "data") -> Any:
    """Parse a file's bytes, decrypting if needed. JSON errors propagate
    as ``ValueError`` (the stores' "corrupt file" path); key problems as
    :class:`DataKeyError`, which no store may swallow."""
    if is_encrypted(raw):
        if _fernet is None:
            raise DataKeyError(f"{label} is encrypted and DATA_KEY "
                               "isn't set")
        from cryptography.fernet import InvalidToken

        try:
            raw = _fernet.decrypt(raw[len(MAGIC):])
        except InvalidToken as exc:
            raise DataKeyError(f"{label} can't be decrypted with this "
                               "DATA_KEY (wrong key?)") from exc
    return json.loads(raw.decode("utf-8-sig"))


def read_json(path: Path) -> Any:
    """``decode`` of a file. ``FileNotFoundError`` propagates."""
    return decode(Path(path).read_bytes(), str(path))


def write_json(path: Path, obj: Any, *, fsync: bool = False,
               **json_kwargs: Any) -> None:
    """Atomic write (temp file + rename), encrypted when a key is set."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(encode(obj, **json_kwargs))
        if fsync:
            handle.flush()
            os.fsync(handle.fileno())
    os.replace(tmp, path)


def check_files(paths: Iterable[Path]) -> Optional[str]:
    """Why these files can't be read with the current key, or None.

    Run at boot: finding out on the first memory lookup would mean a
    bot that is up and silently can't remember anyone.
    """
    for path in paths:
        try:
            raw = Path(path).read_bytes()
        except OSError:
            continue
        if not is_encrypted(raw):
            continue
        try:
            decode(raw, str(path))
        except DataKeyError as exc:
            return str(exc)
        except ValueError:
            continue    # corrupt, not a key problem; the store handles it
        return None     # one good decrypt proves the key
    return None


def check_dir(root: Path, pattern: str = "*.json") -> Optional[str]:
    root = Path(root)
    if not root.exists():
        return None
    return check_files(sorted(root.rglob(pattern)))

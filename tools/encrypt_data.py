"""Encrypt (or decrypt) the bot's stored personal data in place.

    .venv/bin/python tools/encrypt_data.py --new-key   # print a fresh key
    .venv/bin/python tools/encrypt_data.py             # encrypt everything
    .venv/bin/python tools/encrypt_data.py --decrypt   # back to plain JSON

Stop the bot first: it keeps warnings and reminders in memory and would
write its own copy back. Uses DATA_KEY_FILE (or DATA_KEY)
and DATA_ROOT from .env, like the bot. Files already in the
wanted form are left alone. A tar of every file it changes is written
beside them first
(``data-backup-<time>.tar.gz``); delete it once the bot has started
cleanly, since it holds the plain text this exists to remove.

Turning a key on needs no migration (each file is encrypted the next
time it's saved) but memories of people who never come back would stay
plain forever; this does them all.
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (loads .env)
from corbin.core import crypto  # noqa: E402

FILES = ("spamblock_warnings.json", "fluxer_reminders.json")
DIRS = ("user_memories", "server_memories")


def targets(root: Path) -> list[Path]:
    found = [root / name for name in FILES if (root / name).is_file()]
    for name in DIRS:
        if (root / name).is_dir():
            found += sorted(p for p in (root / name).glob("*.json")
                            if ".corrupt." not in p.name)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--new-key", action="store_true",
                        help="print a new key for DATA_KEY and exit")
    parser.add_argument("--decrypt", action="store_true",
                        help="write everything back as plain JSON")
    args = parser.parse_args()

    if args.new_key:
        print(crypto.generate_key())
        print("Save it to a file outside the bot's folder (chmod 600), "
              "point DATA_KEY_FILE at it in .env, and keep a copy "
              "somewhere other than this server.", file=sys.stderr)
        return 0
    key = config.DATA_KEY
    if config.DATA_KEY_FILE:
        key = Path(config.DATA_KEY_FILE).read_text(encoding="ascii").strip()
    if not key:
        print("Neither DATA_KEY_FILE nor DATA_KEY is set in "
              ".env (make a key with --new-key).", file=sys.stderr)
        return 2
    crypto.configure(key)

    root = Path(config.DATA_ROOT)
    todo = []
    for path in targets(root):
        raw = path.read_bytes()
        if crypto.is_encrypted(raw) != args.decrypt:
            continue        # already in the wanted form
        try:
            data = crypto.decode(raw, str(path))
        except crypto.DataKeyError as exc:
            print(f"stopping, nothing changed: {exc}", file=sys.stderr)
            return 1
        except ValueError:
            print(f"skipping unreadable {path}", file=sys.stderr)
            continue
        todo.append((path, data))
    if not todo:
        print("Nothing to do.")
        return 0

    backup = root / f"data-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(backup, "w:gz") as tar:
        for path, _ in todo:
            tar.add(path, arcname=str(path.relative_to(root)))
    backup.chmod(0o600)

    if args.decrypt:
        crypto.configure(None)
    for path, data in todo:
        crypto.write_json(path, data, indent=2, default=str)
    # Read every one back with the key before calling it done.
    crypto.configure(key)
    for path, data in todo:
        if crypto.read_json(path) != data:
            print(f"VERIFY FAILED for {path}; restore from {backup}",
                  file=sys.stderr)
            return 1
    verb = "Decrypted" if args.decrypt else "Encrypted"
    print(f"{verb} {len(todo)} file(s); every one read back correctly.\n"
          f"Backup of the originals: {backup}\n"
          "Delete it once the bot has started cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

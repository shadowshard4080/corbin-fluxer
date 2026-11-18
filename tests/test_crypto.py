"""Offline suite for encryption at rest (corbin/core/crypto.py): what's
on disk, plain files still read, and above all that a missing or wrong
key fails loudly instead of reading as "no memories" and saving over
them."""
from __future__ import annotations
import logging, tempfile
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core import crypto
from corbin.core.memory import MemoryStore
from corbin.core.reminders import ReminderStore
from corbin.core.warnings import WarningStore


def _raises(fn, kind=crypto.DataKeyError) -> bool:
    try:
        fn()
    except kind:
        return True
    return False


def suite_files(r: Results) -> None:
    key, other = crypto.generate_key(), crypto.generate_key()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            crypto.configure(key)
            store = MemoryStore(root)
            mem = store.get_user("42")
            mem.apply(["loves sourdough baking"])
            store.save_user(mem)
            path = store.user_path("42")
            raw = path.read_bytes()
            r.ok("a memory file is written encrypted",
                 crypto.is_encrypted(raw))
            r.ok("...with none of its text readable",
                 # Not b"42": random base64 contains it about one run in
                 # fifteen.
                 b"sourdough" not in raw and b"loves" not in raw
                 and b'"42"' not in raw)
            r.ok("and reads back with the key",
                 "loves sourdough baking" in
                 " ".join(MemoryStore(root).get_user("42").facts))

            crypto.configure(other)
            r.ok("a wrong key raises, never reads as empty",
                 _raises(lambda: MemoryStore(root).get_user("42")))
            r.ok("...and nothing was overwritten or quarantined",
                 path.read_bytes() == raw
                 and not list(path.parent.glob("*.corrupt.*")))
            r.ok("the boot check names the problem",
                 "wrong key" in (crypto.check_dir(root / "user_memories")
                                 or ""))
            crypto.configure(None)
            r.ok("no key at all raises too",
                 _raises(lambda: MemoryStore(root).get_user("42")))
            r.ok("...and the boot check says it isn't set",
                 "isn't set" in (crypto.check_dir(root) or ""))

            # Plain files from before the key keep working.
            plain = MemoryStore(root / "old")
            m = plain.get_user("7")
            m.apply(["plays the cello"])
            plain.save_user(m)
            p7 = plain.user_path("7")
            r.ok("without a key files are plain JSON",
                 not crypto.is_encrypted(p7.read_bytes()))
            crypto.configure(key)
            r.ok("a plain file still reads once a key is set",
                 "plays the cello" in " ".join(
                     MemoryStore(root / "old").get_user("7").facts))
            r.check("the boot check passes plain files",
                    crypto.check_dir(root / "old"), None)
            plain.save_user(MemoryStore(root / "old").get_user("7"))
            r.ok("...and is encrypted the next time it's saved",
                 crypto.is_encrypted(p7.read_bytes()))

            ws = WarningStore(root)
            ws._data = {"g": {"u": [{"reason": "spam links"}]}}
            ws._write()
            r.ok("warnings are encrypted",
                 b"spam links" not in ws.path.read_bytes())
            r.check("and read back", WarningStore(root)._data, ws._data)
            crypto.configure(other)
            r.ok("a wrong key on warnings raises, not 'no warnings'",
                 _raises(lambda: WarningStore(root)))
            crypto.configure(key)
            rs = ReminderStore(root)
            rs._flush()
            r.ok("reminders are encrypted",
                 crypto.is_encrypted(rs.path.read_bytes()))

            r.ok("a malformed key is refused, not ignored",
                 _raises(lambda: crypto.configure("not-a-key")))
        finally:
            crypto.configure(None)


def suite_log_text(r: Results) -> None:
    from corbin.core.logtext import said

    root = logging.getLogger("corbin")
    level = root.level
    try:
        root.setLevel(logging.INFO)
        r.check("at INFO only the length is logged",
                said("my address is 12 Elm St"), "<23 chars>")
        r.check("...None too", said(None), "<0 chars>")
        root.setLevel(logging.DEBUG)
        r.check("at DEBUG the text, cut to the limit",
                said("my address is 12 Elm St", 10), "'my address'")
    finally:
        root.setLevel(level)


#: Names that hold what someone wrote (or a reply that can repeat it).
_TEXT_NAMES = {"text", "content", "query", "question", "reply",
               "transcript", "reasoning", "raw_content", "raw", "topic",
               "heard", "utterance"}


def suite_logs_redacted(r: Results) -> None:
    """No log call passes message text except through said(). A line
    that really is safe says so with a ``# log-ok: why`` comment."""
    import ast

    repo = Path(__file__).resolve().parent.parent
    files = [repo / "bot.py"] + [p for p in (repo / "corbin").rglob("*.py")
                                 if "cloud" not in p.parts]
    leaks = []
    for path in files:
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("info", "warning", "error",
                                           "exception", "critical")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "logger"):
                continue
            if "log-ok" in lines[node.lineno - 1]:
                continue
            for arg in node.args[1:]:
                safe = {id(n) for c in ast.walk(arg)
                        if isinstance(c, ast.Call)
                        and getattr(c.func, "id", "") in ("said", "len")
                        for n in ast.walk(c)}
                if any(isinstance(n, ast.Name) and n.id in _TEXT_NAMES
                       and id(n) not in safe for n in ast.walk(arg)):
                    leaks.append(f"{path.relative_to(repo)}:{node.lineno}")
                    break
    r.check("no log line prints message text unredacted", leaks, [])


SUITES = [("encryption at rest", suite_files),
          ("logs: said() redacts", suite_log_text),
          ("logs: message text only through said()", suite_logs_redacted)]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))

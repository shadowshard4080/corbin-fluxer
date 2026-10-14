"""Offline suite for server event logging."""
from __future__ import annotations
import logging, tempfile
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.audit import (CATEGORIES, DEFAULT_OFF, CachedMessage,
                               MessageCache, normalise_categories, truncate)
from corbin.core.settings import ChatSettings


def _m(mid, cid="c", content="hello"):
    return CachedMessage(id=mid, channel_id=cid, author_id="u",
                         author_name="Sam", content=content)


def suite_cache(r: Results) -> None:
    c = MessageCache(per_channel=3)
    for i in range(5):
        c.put(_m(str(i), content=f"msg {i}"))
    r.check("bounded per channel", len(c), 3)
    r.check("oldest evicted", c.get("c", "0"), None)
    r.check("newest kept", c.get("c", "4").content, "msg 4")

    r.check("pop returns it", c.pop("c", "4").content, "msg 4")
    r.check("pop removes it", c.get("c", "4"), None)
    r.check("pop twice is safe", c.pop("c", "4"), None)
    r.check("unknown channel is safe", c.get("zz", "1"), None)

    # Channels are independent, so a busy channel cannot evict a quiet one.
    c2 = MessageCache(per_channel=2)
    c2.put(_m("a", cid="one")); c2.put(_m("b", cid="two"))
    for i in range(5):
        c2.put(_m(f"x{i}", cid="two"))
    r.ok("quiet channel survives a busy one", c2.get("one", "a") is not None)

    empty = MessageCache()
    empty.put(CachedMessage(id="", channel_id="c", author_id="u",
                            author_name="x", content="y"))
    r.check("an id-less message is not cached", len(empty), 0)


def suite_categories(r: Results) -> None:
    default = normalise_categories({})
    r.check("all categories present", set(default), set(CATEGORIES))
    r.check("noisy ones default off",
            {k for k, v in default.items() if not v}, set(DEFAULT_OFF))
    r.ok("the rest default on",
         all(v for k, v in default.items() if k not in DEFAULT_OFF))

    custom = normalise_categories({"voice": True, "message_edit": False})
    r.ok("explicit on respected", custom["voice"])
    r.ok("explicit off respected", not custom["message_edit"])
    r.ok("unknown keys dropped", "bogus" not in
         normalise_categories({"bogus": True}))
    r.check("garbage input yields defaults",
            normalise_categories("not a dict"), default)

    r.check("short text untouched", truncate("abc", 10), "abc")
    r.check("long text trimmed", len(truncate("x" * 500, 100)), 100)
    r.ok("trim is marked", truncate("x" * 500, 100).endswith("…"))


def suite_settings(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = ChatSettings(Path(tmp) / "c.json")
        r.ok("off by default", not s.audit_enabled("g", "message_delete"))
        r.check("no channel by default", s.audit_channel("g"), None)

        # Enabled but channel-less must still log nothing.
        s.set_audit("g", enabled=True)
        r.ok("enabled without a channel logs nothing",
             not s.audit_enabled("g", "message_delete"))

        s.set_audit("g", enabled=True, channel_id="99")
        r.ok("now enabled", s.audit_enabled("g", "message_delete"))
        r.check("channel stored", s.audit_channel("g"), "99")
        r.ok("noisy category still off", not s.audit_enabled("g", "voice"))

        s.set_audit_category("g", "voice", True)
        r.ok("category turned on", s.audit_enabled("g", "voice"))
        s.set_audit_category("g", "message_edit", False)
        r.ok("category turned off", not s.audit_enabled("g", "message_edit"))
        r.ok("others unaffected", s.audit_enabled("g", "message_delete"))

        r.raises("unknown category rejected", ValueError,
                 s.set_audit_category, "g", "nope", True)

        s2 = ChatSettings(Path(tmp) / "c.json")
        r.ok("survives reload", s2.audit_enabled("g", "voice"))
        r.ok("disabling kills everything",
             (s2.set_audit("g", enabled=False),
              not s2.audit_enabled("g", "voice"))[1])
        r.check("channel retained while off", s2.audit_channel("g"), "99")
        r.ok("no guild is safe", not s2.audit_enabled(None, "voice"))


def _fn_source(name: str) -> str:
    import ast

    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8-sig")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def suite_edit_delete_logs(r: Results) -> None:
    """No phantom edits, and none of Corbin's own messages."""
    edit = _fn_source("on_message_edit")
    delete = _fn_source("on_message_delete")
    handle = _fn_source("_handle")
    r.ok("the edit log resolves the new text before comparing",
         "_readable_content(" in edit)
    r.ok("...the same conversion the cache is filled with",
         "_readable_content(" in handle)
    r.ok("...and logs the resolved text, not raw <@id> tokens",
         '("After", after, False)' in edit
         and '("After", msg.content, False)' not in edit)
    r.ok("an empty partial update is not an edit",
         "not after.strip() and not msg.attachments" in edit)
    r.ok("Corbin's own edits are skipped",
         "_is_me(msg.author.id)" in edit)
    r.ok("Corbin's own deletes are skipped",
         "_is_me(cached.author_id)" in delete)

    # The phantom edit itself: same text, one copy resolved, one raw.
    from corbin.ai.conversation import resolve_mentions
    raw = "<@1506> set https://x/red.png as the welcomer background"
    cached = resolve_mentions(raw, users={"1506": "Corbin"})
    r.ok("a preview loading doesn't read as a change once both are "
         "resolved",
         cached == resolve_mentions(raw, users={"1506": "Corbin"})
         and cached != raw)


SUITES = [
    ("audit message cache", suite_cache),
    ("audit categories", suite_categories),
    ("audit settings", suite_settings),
    ("edit and delete logs", suite_edit_delete_logs),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))

"""Offline suite for bulk message deletion.

``Channel.delete_messages`` takes a **list of message ids**. It is not a
"delete the last N" helper, and passing it a count raised ``'int'
object is not iterable`` from inside the SDK. So ids are collected
first, then deleted.
"""
from __future__ import annotations
import asyncio, logging
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.platforms.fluxer.adapter import (BULK_DELETE_CHUNK,
                                             MESSAGE_PAGE, FluxerAdapter)


def build(total, *, bulk_ok=True, single_ok=True, has_bulk=True):
    messages = [{"id": str(1000 + i)} for i in range(total)]
    seen = {"bulk": [], "single": [], "pages": 0, "max_limit": 0}

    class HTTP:
        async def get_messages(self, channel_id, *, limit=50, before=None):
            seen["pages"] += 1
            seen["max_limit"] = max(seen["max_limit"], limit)
            start = 0
            if before is not None:
                start = next(i for i, m in enumerate(messages)
                             if m["id"] == before) + 1
            return messages[start:start + min(limit, MESSAGE_PAGE)]

        async def delete_message(self, channel_id, message_id):
            if not single_ok:
                raise RuntimeError("403")
            seen["single"].append(str(message_id))

    if has_bulk:
        async def delete_messages(self, channel_id, ids):
            if not bulk_ok:
                raise RuntimeError("400 messages too old")
            seen["bulk"].extend(str(i) for i in ids)
        HTTP.delete_messages = delete_messages

    adapter = FluxerAdapter(type("C", (), {"_http": HTTP()})())
    return adapter, seen


def purge(adapter, count):
    return asyncio.run(adapter.purge("c1", count))


def suite_bulk(r: Results) -> None:
    a, seen = build(200)
    r.check("thirty of two hundred", purge(a, 30), 30)
    r.check("...deleted in bulk", len(seen["bulk"]), 30)
    r.check("...in one page", seen["pages"], 1)
    r.ok("...and they are ids, not a count",
         all(x.isdigit() for x in seen["bulk"]))


def suite_paging(r: Results) -> None:
    """Asking for more than a page must not silently delete one page."""
    a, seen = build(300)
    r.check("a request past one page is paged", purge(a, 120), 120)
    r.check("...with no duplicates", len(set(seen["bulk"])), 120)
    r.ok("...over several requests", seen["pages"] >= 3)
    r.ok("...never asking for more than a page at once",
         seen["max_limit"] <= MESSAGE_PAGE)

    a, seen = build(300)
    purge(a, BULK_DELETE_CHUNK + 5)
    r.ok("deletions are chunked for the bulk endpoint",
         len(seen["bulk"]) == BULK_DELETE_CHUNK + 5)


def suite_honest_count(r: Results) -> None:
    """The number reported is what went, never what was asked for."""
    a, _ = build(10)
    r.check("asking for more than exists reports the truth", purge(a, 50), 10)

    a, _ = build(0)
    r.check("an empty channel deletes nothing", purge(a, 30), 0)

    # The adapter's own delete_message swallows errors by design, so
    # counting its "successes" reported messages that are still there.
    a, seen = build(30, bulk_ok=False, single_ok=False)
    r.check("when every delete fails, nothing is claimed", purge(a, 30), 0)
    r.check("...and none were recorded", len(seen["single"]), 0)


def suite_fallback(r: Results) -> None:
    """Bulk delete is refused for old messages on platforms like this."""
    a, seen = build(30, bulk_ok=False)
    r.check("a refused bulk falls back to one at a time", purge(a, 30), 30)
    r.check("...deleting each", len(seen["single"]), 30)

    # A single message is not worth a bulk call.
    a, seen = build(5)
    r.check("one message deletes singly", purge(a, 1), 1)
    r.check("...not through bulk", len(seen["bulk"]), 0)

    a, seen = build(20, has_bulk=False)
    r.check("an SDK with no bulk endpoint still works", purge(a, 20), 20)
    r.check("...one at a time", len(seen["single"]), 20)


SUITES = [
    ("purge bulk path", suite_bulk),
    ("purge paging", suite_paging),
    ("purge reports honestly", suite_honest_count),
    ("purge fallbacks", suite_fallback),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))

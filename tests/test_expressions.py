"""Offline suite for copying emojis and stickers across from Discord."""
from __future__ import annotations
import asyncio, logging, sys, types
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

import corbin.core.expressions as X
from corbin.core.expressions import Expression, emoji_url, sticker_url


class FakeResp:
    def __init__(self, content=b"\x89PNG" + b"x" * 100, code=200):
        self.content, self.status_code = content, code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeClient:
    def __init__(self, **_k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *_a): return False
    async def get(self, url):
        if "huge" in url:
            return FakeResp(b"\x89PNG" + b"x" * (300 * 1024))
        if "gone" in url:
            return FakeResp(b"", 404)
        return FakeResp()


class FakeAdapter:
    def __init__(self, reject=()):
        self.created, self.reject = [], set(reject)

    async def create_expression(self, guild_id, *, kind, name, image):
        if name in self.reject:
            return "HTTPException: 400 rejected"
        self.created.append((kind, name))
        return None


def copy(items, existing=frozenset(), reject=()):
    adapter = FakeAdapter(reject)
    saved_delay, X.UPLOAD_DELAY = X.UPLOAD_DELAY, 0
    saved_httpx = sys.modules.get("httpx")
    sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=FakeClient)
    try:
        report = asyncio.run(X.copy_to_fluxer(
            list(items), adapter=adapter, guild_id="g",
            existing=set(existing)))
    finally:
        X.UPLOAD_DELAY = saved_delay
        if saved_httpx is not None:
            sys.modules["httpx"] = saved_httpx
        else:
            sys.modules.pop("httpx", None)
    return report, adapter


def suite_urls(r: Results) -> None:
    r.check("animated emoji is a gif",
            emoji_url({"id": "1", "animated": True}).endswith(".gif?size=256"),
            True)
    r.check("static emoji is a png",
            emoji_url({"id": "1", "animated": False}).endswith(".png?size=256"),
            True)
    r.check("png sticker", sticker_url({"id": "9", "format_type": 1}),
            "https://cdn.discordapp.com/stickers/9.png")
    r.check("gif sticker ends .gif",
            sticker_url({"id": "9", "format_type": 4}).endswith(".gif"), True)
    r.check("apng sticker is fetched as png",
            sticker_url({"id": "9", "format_type": 2}).endswith(".png"), True)


def suite_copy(r: Results) -> None:
    items = [Expression("emoji", "corbin", "u/corbin.gif", animated=True),
             Expression("sticker", "wave", "u/wave.png")]
    report, adapter = copy(items)
    r.check("both uploaded", report.copied, 2)
    r.check("...as the right kinds", sorted(adapter.created),
            [("emoji", "corbin"), ("sticker", "wave")])
    r.check("nothing failed", report.failed, 0)


def suite_rerunnable(r: Results) -> None:
    """An interrupted copy is finished by running it again."""
    items = [Expression("emoji", "here", "u/a.png"),
             Expression("emoji", "new", "u/b.png")]
    report, adapter = copy(items, existing={"emoji:here"})
    r.check("existing one skipped", report.skipped_existing, 1)
    r.check("only the new one uploaded", adapter.created, [("emoji", "new")])

    r.check("matching is case-insensitive",
            copy([Expression("emoji", "HERE", "u/a.png")],
                 existing={"emoji:here"})[0].skipped_existing, 1)
    # Same name, different kind: both should land.
    report, adapter = copy(
        [Expression("sticker", "here", "u/a.png")], existing={"emoji:here"})
    r.check("a sticker is not blocked by an emoji of that name",
            report.copied, 1)


def suite_unsupported(r: Results) -> None:
    report, adapter = copy([
        Expression("sticker", "dance", "u/d.png", lottie=True),
        Expression("emoji", "huge", "u/huge.png"),
        Expression("emoji", "gone", "u/gone.png"),
    ])
    r.check("lottie counted separately, not failed",
            (report.skipped_lottie, report.copied), (1, 0))
    r.ok("oversized reported with its size",
         any("over the" in e and "KB" in e for e in report.errors))
    r.ok("a dead CDN link is reported, not raised",
         any("gone" in e for e in report.errors))
    r.check("neither was uploaded", adapter.created, [])


def suite_gives_up(r: Results) -> None:
    """A wall of rejections means rate limited; stop rather than grind."""
    names = [f"e{i}" for i in range(20)]
    items = [Expression("emoji", n, f"u/{n}.png") for n in names]
    report, adapter = copy(items, reject=names)
    r.ok("stopped early", report.stopped_early)
    r.check("stopped at the streak limit", report.failed, X.FAIL_STREAK)
    r.ok("identical errors are not blamed on rate limiting",
         any("That's not rate limiting" in e for e in report.errors))
    r.ok("did not try all twenty", len(report.errors) < 20)

    # An oversized file is that file's problem, not a signal to stop, so
    # it does not count toward the streak.
    mixed = [Expression("emoji", "huge", "u/huge.png")] + \
        [Expression("emoji", f"r{i}", f"u/r{i}.png") for i in range(3)]
    report2, _ = copy(mixed, reject=[f"r{i}" for i in range(3)])
    r.ok("an oversized file alone does not stop the run",
         not report2.stopped_early)
    r.check("...but is still counted", report2.failed, 4)

    # Genuinely varied failures do read as the server pushing back.
    varied = [Expression("emoji", "gone", "u/gone.png")] + \
        [Expression("emoji", f"x{i}", f"u/x{i}.png") for i in range(5)]
    report3, _ = copy(varied, reject=[f"x{i}" for i in range(5)])
    r.ok("mixed causes still read as rate limiting or slots",
         any("rate limited" in e for e in report3.errors))


SUITES = [
    ("expression cdn urls", suite_urls),
    ("expression copy", suite_copy),
    ("expression copy is re-runnable", suite_rerunnable),
    ("expressions that cannot be copied", suite_unsupported),
    ("expression copy gives up", suite_gives_up),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))

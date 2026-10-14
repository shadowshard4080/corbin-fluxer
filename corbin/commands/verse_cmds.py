"""Bible verse lookup and auto-detection."""

from __future__ import annotations

from typing import Any, Optional

from ..core import verses
from ..core.context import CommandContext, Embed
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command

TRANSLATION_CHOICES = ("kjv", "nasb", "hebrew", "greek", "original",
                       "commentary")
COLOUR = 0x8B6F47


#: Words that ask for something other than the default translation. The
#: auto-detector must honour these: it pre-empts the dispatcher entirely,
#: so "Romans 1:1 commentary" would otherwise just get the KJV verse.
_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # "mean" is safe here: this only runs on messages that already
    # contain a verse reference, so the context is narrow.
    ("commentary", ("commentary", "commentaries", "exposition", "gill",
                    "notes", "explain", "explanation", "meaning", "mean",
                    "about this verse", "interpret")),
    ("hebrew", ("hebrew", "original hebrew")),
    ("greek", ("greek", "original greek")),
    ("original", ("original", "original language", "interlinear")),
    ("nasb", ("nasb", "new american standard")),
    ("kjv", ("kjv", "king james")),
)


def detect_translation(text: str) -> Optional[str]:
    """Pick a translation out of the surrounding message, if asked for."""
    low = (text or "").lower()
    for choice, words in _HINTS:
        if any(w in low for w in words):
            return choice
    return None


def _slug(choice: Optional[str], book: int) -> str:
    choice = (choice or "kjv").lower()
    if choice in ("hebrew", "greek", "original"):
        return verses.original_code(book)
    return {"kjv": "KJV", "nasb": "NASB"}.get(choice, "KJV")


async def build_verse_embed(ref: dict, *, translation: str = "kjv",
                            gill: Any = None) -> Embed:
    """One embed for a reference. Used by both the command and detection."""
    title = verses.ref_title(ref)

    if (translation or "").lower() == "commentary":
        rows = gill.lookup(ref) if gill is not None and gill.available else []
        source = "John Gill's Exposition"
        if not rows:
            rows = await verses.fetch_net_notes(ref)
            source = "NET translators' notes"
        if not rows:
            return Embed(title=title,
                         description="No commentary available for that one.",
                         colour=COLOUR)
        body = "\n\n".join(f"**{n}.** {t}" for n, t in rows)
        embed = Embed(title=f"{title} — commentary", colour=COLOUR)
        # Commentary runs long; the adapter clips, but split so nothing is
        # silently lost at the 4096 description limit.
        embed.add_list_field(source, body.split("\n\n"))
        return embed

    slug = _slug(translation, ref["book"])
    rows = await verses.fetch_passage(ref, slug)
    if not rows:
        return Embed(title=title,
                     description="Couldn't fetch that passage just now.",
                     colour=COLOUR)
    body = " ".join(f"**{n}** {t}" for n, t in rows) if len(rows) > 1 \
        else rows[0][1]
    return Embed(title=title, description=body, colour=COLOUR,
                 footer=verses.translations().get(slug, slug))


@command(
    name="verse",
    description="Look up a bible passage",
    risk=Risk.SAFE,
    guild_only=False,
    aliases=("bible", "scripture"),
    params=(
        Param("reference", "e.g. John 3:16 or Gen 1:1-3"),
        Param("translation", "kjv, nasb, original, or commentary",
              required=False, default="kjv", choices=TRANSLATION_CHOICES),
    ),
    examples=("look up John 3:16", "what does Romans 8:28 say",
              "show me Genesis 1:1 in hebrew",
              "commentary on John 3:16"),
)
async def verse(ctx: CommandContext, reference: str,
                translation: str = "kjv") -> Any:
    refs = verses.parse(reference)
    if not refs:
        return (f"I couldn't read “{reference}” as a reference. Try "
                "something like `John 3:16` or `Gen 1:1-3`.")
    return await build_verse_embed(refs[0], translation=translation,
                                   gill=ctx.extras.get("gill"))


@command(
    name="versefinder",
    description="Automatically post verses when someone cites one in chat",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    params=(Param("enabled", "on or off", kind="boolean"),),
    confirm_phrase=lambda a: ("watch for bible references in chat"
                              if a.get("enabled")
                              else "stop watching for bible references"),
    examples=("turn on verse detection", "stop auto-posting verses"),
)
async def versefinder(ctx: CommandContext, enabled: bool) -> str:
    settings = ctx.extras["settings"]
    settings.set_versefinder(ctx.guild_id, enabled)
    if not enabled:
        return "I'll stop watching for verse references."
    return ("I'll post the passage whenever someone cites one. Ask me for "
            "another translation or the commentary on any of them.")

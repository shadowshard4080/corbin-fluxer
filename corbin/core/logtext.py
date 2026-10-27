"""What people wrote, as it may appear in a log line.

Logs don't hold what people write. At INFO and above, :func:`said` gives only its length (``<42 chars>``); the text
itself appears only when the ``corbin`` logger is at DEBUG
(``LOG_LEVEL=DEBUG``), which is for a short debugging session.
Use it for anything a person wrote or said, and for model output that
can repeat it (his replies, refusals, reasoning). Ids, display names
and song titles are not covered by this.
"""

from __future__ import annotations

import logging

_ROOT = logging.getLogger("corbin")


def said(text: object, limit: int = 80) -> str:
    """``repr`` of the first ``limit`` characters at DEBUG, else only
    the length. Use with ``%s``."""
    value = "" if text is None else str(text)
    if _ROOT.isEnabledFor(logging.DEBUG):
        return repr(value[:limit])
    return f"<{len(value)} chars>"

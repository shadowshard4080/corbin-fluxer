"""Lenient JSON extraction for model output.

Mirrors the parent repo's rule: never call ``json.loads`` directly on model
output.  ``MODEL_FAST`` routinely wraps JSON in prose or truncates it at
``max_tokens`` mid-string, and a bare parse throws the whole result away.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def extract_json_object(text: str) -> Optional[dict[str, Any]]:
    """Pull the first JSON object out of ``text``, salvaging truncation."""
    if not text:
        return None

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1)

    start = text.find("{")
    if start == -1:
        return None

    # Fast path: a clean object somewhere in the string.
    depth, in_string, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    break

    # Salvage path: the object never closed (truncated). Close it and retry,
    # dropping whatever trailing fragment is incomplete.
    fragment = text[start:]
    for cut in range(len(fragment), 0, -1):
        candidate = fragment[:cut].rstrip().rstrip(",")
        if not candidate:
            break
        for suffix in ('"}', "}", '"]}', "]}"):
            try:
                return json.loads(candidate + suffix)
            except json.JSONDecodeError:
                continue
    return None


def coerce_float(value: Any, default: float = 0.0) -> float:
    """Models emit 0.9, '0.9', '90%' and 'high'. Take them all."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = value.strip().rstrip("%")
        try:
            number = float(text)
        except ValueError:
            return {"high": 0.95, "medium": 0.7, "low": 0.4,
                    "none": 0.0}.get(text.lower(), default)
        return number / 100.0 if number > 1.0 else number
    return default


def coerce_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "y", "1"):
            return True
        if text in ("false", "no", "n", "0"):
            return False
    return default

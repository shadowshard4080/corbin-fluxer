"""No hardcoded vendor or model names in user-facing strings.

What the bot says about its model is the persona's business, and the
configured model name is the operator's. Embeds, command replies and
error strings are written by us, so this scans the source for a
hardcoded vendor name instead of trusting review.

Docstrings and comments are exempt: they explain the architecture to
whoever is reading the code, and no user ever sees them.
"""
from __future__ import annotations
import ast, logging
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

ROOT = Path(__file__).resolve().parent.parent

#: Vendor and model names that must not reach a user.
BANNED = ("grok", "xai", "x.ai", "anthropic", "claude", "openai", "gpt-",
          "llama", "mistral", "qwen", "deepseek")

#: Modules whose strings can reach a user. The ai/ package is excluded:
#: its strings are prompts sent to a model, not text shown to anyone,
#: and the confidence check legitimately names the answering model.
USER_FACING = ["bot.py"] + sorted(
    str(p.relative_to(ROOT)) for p in (ROOT / "corbin" / "commands").glob("*.py"))


def user_strings(path: Path):
    """Every string literal that is not a docstring, with its line."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            yield node.lineno, node.value


def suite_no_vendor_names(r: Results) -> None:
    leaks = []
    scanned = 0
    for rel in USER_FACING:
        path = ROOT / rel
        if not path.exists():
            continue
        scanned += 1
        for line, text in user_strings(path):
            low = text.lower()
            for word in BANNED:
                if word in low:
                    leaks.append(f"{rel}:{line}: {text.strip()[:70]!r}")
    r.ok(f"scanned {scanned} user-facing module(s)", scanned > 5)
    r.check("no vendor or model name in any user-facing string",
            leaks, [])
    if leaks:                       # surface them all, not just the count
        for leak in leaks[:10]:
            r.ok(f"LEAK {leak}", False)


SUITES = [
    ("no vendor names in user-facing strings", suite_no_vendor_names),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))

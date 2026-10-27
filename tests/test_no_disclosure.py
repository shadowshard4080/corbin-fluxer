"""Corbin never names the vendor behind him; he is Corbin:latest.

SOUL.md forbids it in the persona, but a persona rule only governs what
the *model* writes. Embeds, command replies and error strings are
written by us, and one of them named the backend within minutes of
being added. This scans the source for the leak instead of trusting
review.

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

#: What Corbin may call himself. The configured local model name.
ALLOWED_MODEL = "corbin"

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


def suite_persona_rules(r: Results) -> None:
    """Both rule sets must carry the instruction, not just the long one.

    The local model is the default and only ever sees COMPACT_RULES;
    the full SOUL.md sections are too long for it to act on.
    """
    from corbin.ai.conversation import COMPACT_RULES

    low = COMPACT_RULES.lower()
    # Asked what he runs on, he answers as Corbin:latest -- the owner's
    # call. The rule used to be "never name the model", and the local
    # model obeyed it: "I don't name the model under the hood".
    r.ok("compact rules give the answer: Corbin:latest",
         "corbin:latest" in low)
    r.ok("...and no longer tell him to refuse",
         "never name the model" not in low)
    r.ok("...quoting the dodge he actually gave, as a thing not to say",
         "don't name the model under the hood" in low)
    r.ok("...while still naming no other model or company",
         "no other" in low)
    r.ok("...and still forbid the 'as an AI' tell", "as an ai" in low)
    import re
    # Whole words: "Ollama", the host he is told to name, contains "llama".
    r.ok("no vendor or base-model name in the rules themselves",
         not any(re.search(rf"(?<![a-z]){re.escape(w)}", low)
                 for w in BANNED))

    modelfile = ROOT / "ollama" / "Corbin.Modelfile"
    if modelfile.exists():
        persona = modelfile.read_text(encoding="utf-8").lower()
        system = persona.split('system """', 1)[-1]
        r.ok("the Modelfile persona agrees: Corbin:latest",
             "corbin:latest" in system)
        r.ok("...and doesn't still forbid naming it",
             "naming the model you run on" not in system)

    soul = ROOT / "persona" / "SOUL.md"
    if soul.exists():
        text = soul.read_text(encoding="utf-8", errors="replace").lower()
        r.ok("SOUL.md gives the same answer", "corbin:latest" in text)
        r.ok("...and forbids claiming another vendor",
             "doesn't claim any other vendor" in text)
    else:
        r.ok("SOUL.md not present in this checkout (skipped)", True)


def boot_embed_node():
    """The AST of ``_post_boot_diagnostics``, wherever it sits in main()."""
    tree = ast.parse((ROOT / "bot.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name == "_post_boot_diagnostics":
            return node
    return None


def suite_status_embed(r: Results) -> None:
    """The boot embed reports the local model name, never the backend."""
    node = boot_embed_node()
    r.ok("found _post_boot_diagnostics", node is not None)
    if node is None:
        return

    # String literals only. Checking the raw text catches comments,
    # which is how an explanation of why we avoid a word registered as
    # the word itself.
    literals = [n.value.lower() for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    joined = " ".join(literals)
    names = [n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)]

    for word in ("grok", "claude", "anthropic", "xai"):
        r.ok(f"boot embed never says {word!r}", word not in joined)
    r.ok("the name comes from OLLAMA_MODEL", "OLLAMA_MODEL" in names)
    # Where he is thinking, not what is doing the thinking.
    r.ok("a reachable local host reads as Local", "local" in literals)
    r.ok("...and an unreachable one as Remote", "remote" in literals)
    # "offline" would read as the bot being down when it answers fine.
    r.ok("neither state says offline", "offline" not in joined)


SUITES = [
    ("no vendor names in user-facing strings", suite_no_vendor_names),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))

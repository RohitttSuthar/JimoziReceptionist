"""Prompt loader: every LLM prompt lives in a text file, never inside Python code.

Files are looked up in settings.prompts_dir (env PROMPTS_DIR, default: app/prompts),
cached by modification time (edit a file and the next request picks it up without a restart),
and rendered with simple {placeholder} substitution. Unknown {braces} are left untouched,
so prompt text may safely contain literal curly brackets.
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

from .config import settings

log = logging.getLogger("app.prompts")

_EXT = ".txt"
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Prompt files the answer pipeline needs (file name without extension).
REQUIRED_PROMPTS: tuple[str, ...] = (
    "answer_system",       # system prompt; {business_name} {audience} {max_answer_chars} {terminology}
    "answer_user",         # user message;  {history} {context} {question}
    "script_correction",   # system prompt for the Devanagari-only rewrite pass
    "correction_user",     # user message for the rewrite; {answer} {violations}
)
# Only needed when ENABLE_INGEST_ENRICHMENT=true (one-time GPT indexing of uploaded documents).
ENRICH_PROMPTS: tuple[str, ...] = ("enrich_system", "enrich_user")
# Optional files; if absent they are simply skipped:
#   brand_terminology - the business's preferred vocabulary / spelling, edited by the business
#   fallback_default  - spoken reply when the product config has no fallback text


class PromptError(RuntimeError):
    """A prompt file is missing, unreadable or empty."""


class PromptStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self._cache: dict[str, tuple[int, str]] = {}
        self._lock = threading.Lock()

    def path(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", name or ""):
            raise PromptError(f"Invalid prompt name: {name!r}")
        return self.directory / f"{name}{_EXT}"

    def read(self, name: str) -> str:
        path = self.path(name)
        try:
            stamp = path.stat().st_mtime_ns
            with self._lock:
                cached = self._cache.get(name)
                if cached and cached[0] == stamp:
                    return cached[1]
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PromptError(f"Prompt file not found or unreadable: {path}") from exc
        if not text:
            raise PromptError(f"Prompt file is empty: {path}")
        with self._lock:
            self._cache[name] = (stamp, text)
        return text

    def read_optional(self, name: str, default: str = "") -> str:
        try:
            return self.read(name)
        except PromptError:
            return default

    def fingerprint(self) -> str:
        """Changes whenever any prompt file is edited (used to invalidate cached answers)."""
        try:
            return str(max((p.stat().st_mtime_ns for p in self.directory.glob(f"*{_EXT}")), default=0))
        except OSError:
            return "0"

    def render(self, name: str, /, **values: Any) -> str:
        text = self.read(name)
        return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)

    def validate(self, names: tuple[str, ...] = REQUIRED_PROMPTS) -> None:
        """Raise one PromptError listing every missing/empty prompt file."""
        problems = []
        for name in names:
            try:
                self.read(name)
            except PromptError as exc:
                problems.append(str(exc))
        if problems:
            raise PromptError("; ".join(problems) + f" (prompts directory: {self.directory})")


prompts = PromptStore(settings.prompts_dir)

"""Shared helpers for skill-oriented agent tasks."""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(?P<code>.*)\n\s*```\s*$", re.DOTALL)
_REQUIRED_HEADINGS = (
    "## What it is",
    "## Core concepts",
    "## Common patterns",
    "## Gotchas",
    "## Testing notes",
)


def strip_markdown_fences(text: str) -> str:
    m = _FENCE_RE.match(text or "")
    if not m:
        return (text or "").strip()
    return (m.group("code") or "").strip()


def validate_skill_markdown(text: str) -> list[str]:
    errs: list[str] = []
    raw = (text or "").strip()
    if not raw:
        return ["Skill markdown is empty."]

    if _FENCE_RE.match(raw):
        errs.append("Skill markdown must not be wrapped in outer code fences.")

    stripped = strip_markdown_fences(raw)
    for heading in _REQUIRED_HEADINGS:
        if heading not in stripped:
            errs.append(f"Missing required heading: {heading}")

    return errs

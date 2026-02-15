"""Shared utilities for LLM generation backends."""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path


def render_template(text: str, mapping: dict[str, str]) -> str:
    """Very small template renderer: replaces `{{name}}` placeholders."""

    rendered = text
    for key, value in mapping.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(?P<code>.*)\n\s*```\s*$", re.DOTALL)


def strip_markdown_fences(text: str) -> str:
    m = _FENCE_RE.match(text or "")
    if not m:
        return (text or "").strip()
    return (m.group("code") or "").strip()


def fmt_kv_block(items: list[tuple[str, str]], *, empty: str = "(none)") -> str:
    if not items:
        return empty
    chunks: list[str] = []
    for key, value in items:
        chunks.append(f"# {key}\n{value.rstrip()}\n")
    return "\n".join(chunks).rstrip() + "\n"


def load_prompt(default_name: str, override_path: str | None) -> str:
    """Load a prompt template from the packaged defaults or a user-specified path."""
    if override_path:
        return Path(override_path).read_text(encoding="utf-8")
    p = resources.files("jaunt") / "prompts" / default_name
    return p.read_text(encoding="utf-8")

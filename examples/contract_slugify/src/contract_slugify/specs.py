"""Contract-mode example: committed code is the source of truth."""

from __future__ import annotations

import re

import jaunt

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@jaunt.contract
def slugify(title: str) -> str:
    """
    Convert a human title into a URL-safe slug.

    Examples:
    - "  Hello, World!  " -> "hello-world"
    - "C++ > Java" -> "c-java"
    - "already-slug" -> "already-slug"

    Raises:
    - "" raises ValueError
    - "!!!" raises ValueError
    """
    cleaned = _NON_ALNUM.sub("-", title.strip().lower()).strip("-")
    if not cleaned:
        raise ValueError("title is empty after cleaning")
    return cleaned


@jaunt.contract
def describe(n: int) -> str:
    """
    Loosely describe a number. (Deliberately weak contract: one example only,
    so its strength score is low — the < 0 branch is unpinned.)

    Examples:
    - 0 -> "zero"
    """
    if n == 0:
        return "zero"
    if n < 0:
        return "negative"
    return "positive"

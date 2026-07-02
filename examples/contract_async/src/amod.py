"""Async contract-mode example: committed async code is the source of truth."""

from __future__ import annotations

import re

import jaunt  # noqa: F401  (adopt adds the @jaunt.contract marker below)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@jaunt.contract
async def fetch_slug(title: str) -> str:
    """
    Asynchronously convert a human title into a URL-safe slug.

    (The body is async to exercise contract mode over coroutine functions; it is
    deterministic so the derived battery is offline-verifiable.)

    Examples:
    - fetch_slug("  Hello, World!  ") == "hello-world"
    - fetch_slug("C++ > Java") == "c-java"

    Raises:
    - fetch_slug("") raises ValueError
    """
    cleaned = _NON_ALNUM.sub("-", title.strip().lower()).strip("-")
    if not cleaned:
        raise ValueError("title is empty after cleaning")
    return cleaned

from __future__ import annotations

import jaunt


@jaunt.magic()
def slugify(title: str) -> str:
    """
    Convert a human title into a URL-safe slug.

    Rules:
    - Trim surrounding whitespace.
    - Lowercase.
    - Replace any run of non-alphanumeric characters with a single "-".
    - Strip leading/trailing "-".
    - Must return a non-empty string.

    Examples:
    - "  Hello, World!  " -> "hello-world"
    - "---Already--Slug---" -> "already-slug"
    - "C++ > Java" -> "c-java"

    Errors:
    - Raise ValueError if `title` is empty or becomes empty after cleaning.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=slugify)
def post_slug(title: str, *, post_id: int) -> str:
    """
    Create a stable post slug with a numeric suffix.

    - Uses slugify(title) for the base slug.
    - Suffix format: "<base>-<post_id>".
    - post_id must be >= 1 (otherwise raise ValueError).

    Examples:
    - post_slug("Hello", post_id=42) == "hello-42"
    """
    raise RuntimeError("spec stub (generated at build time)")


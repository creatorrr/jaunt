from __future__ import annotations

import jaunt


@jaunt.magic()
def normalize_email(raw: str) -> str:
    """
    Normalize an email address for stable comparisons.

    Rules:
    - Strip surrounding whitespace.
    - Lowercase the entire string.
    - Must contain exactly one "@".
    - Local-part and domain must both be non-empty after splitting.
    - Domain must contain at least one "." (example: "example.com").

    Returns:
    - The normalized address in the form "<local>@<domain>".

    Errors:
    - Raise ValueError if `raw` is invalid by the rules above.

    Examples:
    - normalize_email("  A.B+tag@Example.COM  ") == "a.b+tag@example.com"
    """


@jaunt.magic(deps=[normalize_email])
def is_corporate_email(raw: str, *, domain: str = "example.com") -> bool:
    """
    Return True iff `normalize_email(raw)` belongs to `domain`.

    Behavior:
    - Uses `normalize_email(raw)` for parsing/validation.
    - `domain` is normalized via `.strip().lower()` before comparison.
    - If `normalize_email(raw)` raises ValueError, propagate it unchanged.

    Examples:
    - is_corporate_email("User@EXAMPLE.com") is True
    - is_corporate_email("user@other.com") is False
    - is_corporate_email("u@corp.example", domain="corp.example") is True
    """


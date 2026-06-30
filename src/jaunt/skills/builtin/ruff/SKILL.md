---
name: "ruff"
description: "Use whenever writing or editing Python that must pass `ruff check` and `ruff format`. Covers the lint rules Jaunt enforces (E, F, I, UP, B), line-length 100, import sorting, and writing code that is clean on the first pass without noqa."
---

# ruff

## What it is
Ruff is the linter and formatter Jaunt-generated code must satisfy. Configured for line-length 100, target py312+, rules E/F/I/UP/B.

## Core concepts
- Import ordering (I): stdlib, third-party, first-party groups, each alphabetized; no unused imports (F401).
- Modern syntax (UP): `X | None` over `Optional[X]`, `list[int]` over `List[int]`, `from __future__ import annotations` at top when needed.
- Bugbear (B): no mutable default args, no `except:` bare, no unused loop vars.

## Common patterns
- Annotate everything; remove dead imports; keep lines <= 100 chars.
- Prefer f-strings; avoid `== None` (use `is None`).

## Gotchas
- Do not add `# noqa` to silence fixable issues -- fix the code.
- Trailing whitespace and missing final newline fail `ruff format --check`.

## Testing notes
- Code that lints clean still needs behavior tests; ruff is not a correctness check.

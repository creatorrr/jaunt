---
name: "uv"
description: "Use when generated code must run under uv-managed environments. Covers importing only declared dependencies, not assuming globally-installed packages, and keeping module imports consistent with the project's declared dependency set."
---

# uv

## What it is
uv manages the project's virtualenv and dependencies. Generated code runs inside `uv run`.

## Core concepts
- Only import packages that are declared dependencies of the project.
- Standard library is always available; third-party libs must be declared.

## Common patterns
- Prefer stdlib when a dependency is not already present in the project.
- Keep imports at module top; no dynamic install at runtime.

## Gotchas
- Do not shell out to `pip install`; do not import undeclared packages.

## Testing notes
- Tests run under the same uv environment; they may import only declared deps.

---
name: "ty"
description: "Use whenever writing Python that must pass the `ty` type checker. Covers full annotations on all params and returns, and avoiding ty diagnostics like possibly-unbound, possibly-missing-attribute, invalid-assignment, and invalid-return-type."
---

# ty

## What it is
`ty` is the static type checker Jaunt runs on generated code. Full type coverage is expected.

## Core concepts
- Annotate every parameter and return type. Use precise types, not bare `object`/`Any`.
- Narrow `X | None` before use (guard with `if x is None`).
- Keep return types consistent across all branches.

## Common patterns
- Use `typing`/`collections.abc` protocols (`Callable`, `Sequence`, `Mapping`).
- For optional attributes, initialize in `__init__`; avoid conditional attribute creation.

## Gotchas
- Returning `None` implicitly from a function annotated `-> T` fails ty.
- Accessing an attribute that may be missing raises possibly-missing-attribute -- assign defaults.

## Testing notes
- ty passing does not prove runtime correctness; still write pytest tests.

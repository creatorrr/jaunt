# CSV -> Typed Dataclass Parser (Jaunt Example)

This is "boring glue code nobody wants to write": turning CSV rows into typed
dataclass records with header mapping, whitespace cleanup, type coercion, and
clear strict vs lenient behavior. It is easy to get wrong (edge cases like extra
columns, missing required fields, odd boolean values, and half-broken rows) and
annoying to maintain because tiny spec changes can ripple across parsing and
validation logic.

## Commands

Build:

```bash
uv run jaunt build --root examples/csv_parser
```

Test:

```bash
PYTHONPATH=examples/csv_parser/src uv run jaunt test --root examples/csv_parser
```


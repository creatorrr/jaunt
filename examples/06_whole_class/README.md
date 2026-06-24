# Whole-Class Magic (Jaunt Example)

This example demonstrates whole-class `@jaunt.magic` authoring.

`Stack` uses mix mode: the class declares generated method stubs for `push`,
`pop`, and `peek`, while `@jaunt.preserve` keeps `is_empty` as hand-written
code. `Inventory` uses docstring-only mode, letting Jaunt design the public API
from the class contract. `TempStats` opts into implicit auto-testing with
`@jaunt.magic(test=True)`, and this example also enables `[test]
auto_class_tests`.

> **Note:** `jaunt.toml` sets `[agent] engine = "legacy"`. Whole-class `@magic`
> generation currently requires the direct backend; the default `aider` engine
> does not yet emit a full class body for whole-class specs.

## Build

This example requires an OpenAI API key:

```bash
cd examples/06_whole_class && uv run --project ../.. jaunt build
```

## Test

Generate the implementation, synthesize class tests, and run pytest:

```bash
cd examples/06_whole_class && uv run --project ../.. jaunt test
```

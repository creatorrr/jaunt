# Whole-Class Magic (Jaunt Example)

This example demonstrates whole-class `@jaunt.magic` authoring.

`Stack` uses mix mode: the class declares generated method stubs for `push`,
`pop`, and `peek`, while `@jaunt.preserve` keeps `is_empty` as hand-written
code. `Inventory` uses docstring-only mode, letting Jaunt design the public API
from the class contract.

## Build

This example requires an OpenAI API key:

```bash
cd examples/06_whole_class && uv run --project ../.. jaunt build
```

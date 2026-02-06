# PROMPT-140: Example Project (CSV -> Typed Dataclass Parser)

Repo: `/Users/ishitajindal/Documents/jaunt`

## Objective
Show “boring glue code nobody wants to write” with strong wow gap:
- header mapping to dataclass fields
- type coercion
- strict vs lenient behavior
- skipping bad rows in lenient mode

## Owned Files (edit only these)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/csv_parser/jaunt.toml` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/csv_parser/src/csv_demo/__init__.py` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/csv_parser/src/csv_demo/specs.py` (edit)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/csv_parser/tests/__init__.py` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/csv_parser/tests/specs.py` (edit)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/csv_parser/README.md` (new)

## Hard Requirements (Do Not Skip)
Make files valid Python:
- Every `@jaunt.magic` stub must include:
  - `raise RuntimeError("spec stub (generated at build time)")`
- Every `@jaunt.test` stub must include:
  - `raise AssertionError("spec stub (generated at test time)")`

## Deliverables

### 1) `jaunt.toml`
Minimal config:
- `source_roots=["src"]`, `test_roots=["tests"]`, `generated_dir="__generated__"`
- OpenAI config with `OPENAI_API_KEY`, model `gpt-5.2`

### 2) `src/csv_demo/__init__.py`
Export:
- `parse_csv`
- `parse_csv_file`

### 3) `src/csv_demo/specs.py` (edit)
Keep the current spec but ensure stub bodies raise.
Ensure the spec is explicit on:
- strict vs lenient behaviors
- supported coercions (str/int/float/bool)
- whitespace trimming
- skipping empty trailing rows

### 4) `tests/__init__.py`
Empty file so `tests` is a package.

### 5) `tests/specs.py` (edit)
Ensure each `@jaunt.test` stub raises.
Test intent to include:
- basic parsing into a dataclass
- whitespace handling
- strict extra column raises
- lenient skips bad rows
- non-dataclass target raises TypeError
- bool coercion variants

### 6) `README.md`
Include:
- Build/test commands:
  - `uv run jaunt build --root jaunt-examples/csv_parser`
  - `PYTHONPATH=jaunt-examples/csv_parser/src uv run jaunt test --root jaunt-examples/csv_parser`
- One paragraph “why this is annoying / error-prone to write by hand”.

## Quality Gates
```bash
.venv/bin/python -m compileall jaunt-examples/csv_parser/src jaunt-examples/csv_parser/tests
```

## Constraints
- No external dependencies.
- Do not modify Jaunt core code.


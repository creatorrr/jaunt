# PROMPT-120: Example Project (Markdown -> HTML Renderer)

Repo: `/Users/ishitajindal/Documents/jaunt`

## Objective
Make the “state machine” wow gap obvious: a tiny, readable spec for Markdown support, with an implementation that would normally be a pile of edge cases.

## Owned Files (edit only these)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/markdown_render/jaunt.toml` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/markdown_render/src/md_demo/__init__.py` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/markdown_render/src/md_demo/specs.py` (edit)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/markdown_render/tests/__init__.py` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/markdown_render/tests/specs.py` (edit)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/markdown_render/README.md` (new)

## Hard Requirements (Do Not Skip)
Make files valid Python:
- Every `@jaunt.magic` stub must include:
  - `raise RuntimeError("spec stub (generated at build time)")`
- Every `@jaunt.test` stub must include:
  - `raise AssertionError("spec stub (generated at test time)")`

## Deliverables

### 1) `jaunt.toml`
Create the same minimal config pattern as other examples:
- `source_roots=["src"]`, `test_roots=["tests"]`, `generated_dir="__generated__"`
- OpenAI config using `OPENAI_API_KEY` and model `gpt-5.2`

### 2) `src/md_demo/__init__.py`
Export:
- `md_to_html`
- `md_to_html_fragment`

### 3) `src/md_demo/specs.py` (edit)
Keep the existing scope but ensure the stub bodies raise.
Ensure the spec includes:
- headings
- fenced code blocks
- unordered lists
- blockquotes
- paragraphs
- inline formatting (bold/italic/code/links)
- HTML escaping rules
- explicit rule: inline formatting NOT applied inside code blocks

### 4) `tests/__init__.py`
Empty file so `tests` is a package.

### 5) `tests/specs.py` (edit)
Ensure each `@jaunt.test` stub raises `AssertionError(...)`.
Keep test intent clear and “demo-friendly”:
- headings
- inline formatting
- fenced code block behavior (escaping + no inline formatting)
- unordered list
- escaping `<`, `>`, `&`
- empty input behavior (`md_to_html("") == ""`, fragment wrapper `"<div />"`)

### 6) `README.md`
Include:
- Build/test commands (same pattern as JWT):
  - `uv run jaunt build --root jaunt-examples/markdown_render`
  - `PYTHONPATH=jaunt-examples/markdown_render/src uv run jaunt test --root jaunt-examples/markdown_render`
- A short “why this is annoying to implement” paragraph.

## Quality Gates
```bash
.venv/bin/python -m compileall jaunt-examples/markdown_render/src jaunt-examples/markdown_render/tests
```

## Constraints
- No external dependencies for the renderer (keep it pure Python).
- Do not modify Jaunt core code.


# Markdown Render (Jaunt Example)

This example defines a tiny subset of Markdown and renders it to HTML, purely in Python (no external renderer deps).

## Commands

Build (generate implementation from the specs):
```bash
uv run jaunt build --root examples/markdown_render
```

Test (run the generated implementation against the test specs):
```bash
PYTHONPATH=examples/markdown_render/src uv run jaunt test --root examples/markdown_render
```

## Why This Is Annoying To Implement

Even a "small" Markdown subset turns into a pile of precedence and escaping rules: you need block parsing (blank-line
separation, multi-line constructs like fences), inline parsing (bold/italic/code/links), correct HTML escaping, and
clear boundaries (for example, inline formatting must not run inside code blocks).


# PROMPT-101: Demo Dependency (pydantic) For Skills Story

Repo: `/Users/ishitajindal/Documents/jaunt`

## Objective
Ensure the hackathon demos can reliably import `pydantic` so the new “auto-generate PyPI skills” feature has an obvious, concrete external library to generate skills for.

This prompt is intentionally tiny: add the dependency and refresh the lockfile.

## Owned Files (edit only these)
- `/Users/ishitajindal/Documents/jaunt/pyproject.toml`
- `/Users/ishitajindal/Documents/jaunt/uv.lock`

## Deliverables
1) Add `pydantic>=2,<3` to `[project].dependencies` in `/Users/ishitajindal/Documents/jaunt/pyproject.toml`.

2) Update `/Users/ishitajindal/Documents/jaunt/uv.lock` accordingly using `uv`:
- recommended: `uv add "pydantic>=2,<3"` (if you want uv to edit `pyproject.toml`)
- or: edit `pyproject.toml` then run `uv lock` / `uv sync`

3) Ensure the venv is synced:
```bash
uv sync
```

4) Sanity import:
```bash
.venv/bin/python -c "import pydantic; print(pydantic.__version__)"
```

## Quality Gates
```bash
uv sync
.venv/bin/python -c "import pydantic; print(pydantic.__version__)"
```

## Constraints
- Do not change any other dependencies.


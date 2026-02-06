# Toy Example (Jaunt)

This folder is a tiny, runnable example of using Jaunt in a consumer project.

## What You'll See

- Spec stubs in `src/toy_app/email_specs.py` (decorated with `@jaunt.magic`).
- Test specs in `tests/specs_email.py` (decorated with `@jaunt.test`).
- Generated code under `src/toy_app/__generated__/...` and `tests/__generated__/...`.

## Run It (From The Jaunt Repo Root)

```bash
uv sync

# Export a real key (Jaunt reads it from env at runtime).
export OPENAI_API_KEY=...

# 1) Generate implementation modules for @jaunt.magic specs.
uv run jaunt build --root examples/toy_app

# 2) Generate pytest tests for @jaunt.test specs and run them.
uv run jaunt test --root examples/toy_app

# 3) Call the generated implementation via the runtime wrappers.
PYTHONPATH=examples/toy_app/src uv run python - <<'PY'
from toy_app.email_specs import is_corporate_email, normalize_email

print(normalize_email("  A.B+tag@Example.COM  "))
print(is_corporate_email("User@example.com"))
print(is_corporate_email("User@other.com"))
PY
```

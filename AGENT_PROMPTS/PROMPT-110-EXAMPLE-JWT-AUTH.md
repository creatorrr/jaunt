# PROMPT-110: Example Project (JWT Auth) - Primary Live Demo

Repo: `/Users/ishitajindal/Documents/jaunt`

## Objective
Turn `/Users/ishitajindal/Documents/jaunt/jaunt-examples/jwt_auth/` into the primary live demo:
- Spec is dead simple.
- Implementation is fiddly (base64url, HMAC, JSON, expiry).
- Uses `pydantic` so auto skill generation is visible and compelling.

## Owned Files (edit only these)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/jwt_auth/jaunt.toml` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/jwt_auth/src/jwt_demo/__init__.py` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/jwt_auth/src/jwt_demo/specs.py` (edit)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/jwt_auth/tests/__init__.py` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/jwt_auth/tests/specs.py` (edit)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/jwt_auth/README.md` (new)

## Hard Requirements (Do Not Skip)
Make files valid Python:
- Every `@jaunt.magic` stub must include:
  - `raise RuntimeError("spec stub (generated at build time)")`
- Every `@jaunt.test` stub must include:
  - `raise AssertionError("spec stub (generated at test time)")`

Do NOT leave empty function bodies or docstring-only bodies.

## Deliverables

### 1) `jaunt-examples/jwt_auth/jaunt.toml`
Create a minimal config:
```toml
version = 1

[paths]
source_roots = ["src"]
test_roots = ["tests"]
generated_dir = "__generated__"

[llm]
provider = "openai"
model = "gpt-5.2"
api_key_env = "OPENAI_API_KEY"
```

### 2) `src/jwt_demo/__init__.py`
Export the user-facing API:
- `create_token`, `verify_token`, `rotate_token`, `Claims`

### 3) `src/jwt_demo/specs.py` (edit)
Update the spec to use `pydantic.BaseModel` for `Claims`:
- `sub: str`, `iat: float`, `exp: float`

Keep the contract explicit:
- base64url encoding without padding
- HS256 HMAC signing
- strict structure validation
- exact error messages:
  - `ValueError("malformed")`
  - `ValueError("invalid signature")`
  - `ValueError("expired")`

Ensure all `@jaunt.magic` functions have the proper `raise RuntimeError(...)` stub body.

### 4) `tests/__init__.py`
Create an empty file so `tests` is a package.

### 5) `tests/specs.py` (edit)
Ensure each `@jaunt.test` function has `raise AssertionError(...)` as its body.
Tests to include in the spec docstrings (not implementation):
- Roundtrip create+verify
- Expired token raises `ValueError("expired")` using `timedelta(seconds=-1)`
- Wrong secret raises `ValueError("invalid signature")`
- Tampered signature raises `ValueError("invalid signature")`
- Malformed token raises `ValueError("malformed")`
- Rotate preserves subject and produces later iat/exp

### 6) `README.md` (new)
Short demo-focused instructions:
- Build:
  - `uv run jaunt build --root jaunt-examples/jwt_auth`
- Test:
  - `PYTHONPATH=jaunt-examples/jwt_auth/src uv run jaunt test --root jaunt-examples/jwt_auth`
- Skills proof:
  - After build, show `jaunt-examples/jwt_auth/.agents/skills/pydantic/SKILL.md` exists.

Include one paragraph “why this is impressive” for the audience.

## Quality Gates
```bash
.venv/bin/python -m compileall jaunt-examples/jwt_auth/src jaunt-examples/jwt_auth/tests
```

## Constraints
- Do not add any new external runtime dependencies beyond `pydantic` already added at the repo level.
- Do not change Jaunt core code in this prompt; only the example project.


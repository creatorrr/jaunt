# JWT Auth (Jaunt Example)

This example is designed for a simple, compelling live demo: the spec is small,
but the implementation details are fiddly (base64url without padding, HMAC signing,
JSON, and expiry validation). It also uses `pydantic`, so you can clearly see Jaunt
generate a skill for a real dependency.

## Build

```bash
uv run jaunt build --root examples/jwt_auth
```

## Test

```bash
PYTHONPATH=examples/jwt_auth/src uv run jaunt test --root examples/jwt_auth
```

## Skills Proof

After build, verify this file exists (it is generated and gitignored):

`examples/jwt_auth/.agents/skills/pydantic/SKILL.md`

You should also see generated modules appear under:

- `examples/jwt_auth/src/jwt_demo/__generated__/`
- `examples/jwt_auth/tests/__generated__/`

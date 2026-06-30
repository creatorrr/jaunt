---
name: "pydantic"
description: "Use when generating code that defines or uses Pydantic v2 models — BaseModel, field types and validators, model_config, serialization (model_dump), and ValidationError handling."
---

# pydantic

## What it is
Pydantic v2 validates Python data using type annotations. Use it for request and response
models, configuration objects, structured tool outputs, and boundaries where untrusted data
must become typed Python objects.

Generated models should be explicit about optional fields, defaults, validation rules, and
serialization shape. Prefer Pydantic at boundaries and plain Python objects inside simple
business logic when validation is no longer needed.

## Core concepts
- `BaseModel` defines typed fields and creates validated instances.
- `Field(...)` adds defaults, aliases, descriptions, constraints, and examples.
- `@field_validator` validates or normalizes one field; `@model_validator` handles checks
  across fields.
- `model_config = ConfigDict(...)` controls behavior such as `extra`, aliases, and strictness.
- `model_dump()` and `model_dump_json()` serialize models.
- `ValidationError` contains structured error details for invalid input.

## Common patterns
Define models with concrete types and clear constraints:

```python
from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3)
    display_name: str | None = None

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value:
            raise ValueError("email must contain @")
        return value
```

Validate incoming dictionaries and serialize only what the caller should see:

```python
def parse_user(payload: dict[str, object]) -> dict[str, object]:
    user = UserCreate.model_validate(payload)
    return user.model_dump(exclude_none=True)
```

Handle validation failures at the boundary:

```python
from pydantic import ValidationError


try:
    user = UserCreate.model_validate(payload)
except ValidationError as exc:
    errors = exc.errors()
```

## Gotchas
- `Optional[str]` or `str | None` does not imply a default. Add `= None` when the field may be
  omitted.
- Pydantic coerces many values by default. Use strict field types or config when coercion is
  unsafe.
- Validators should raise `ValueError` or `TypeError` with concise messages; do not return
  unrelated types.
- Use `model_dump()` in v2; avoid v1-only patterns such as `.dict()` in new code.
- Do not store secrets in models that are logged or serialized unless the type or serializer
  masks them.

## Testing notes
Test valid input, missing required fields, extra fields if `extra="forbid"`, coercion rules,
and validator failures. Assert `ValidationError.errors()` shapes only as much as needed; exact
message text can be brittle. For API boundaries, test both `model_validate()` and
`model_dump()` so validation and serialization stay aligned.

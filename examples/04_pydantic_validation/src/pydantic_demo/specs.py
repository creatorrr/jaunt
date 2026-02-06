from __future__ import annotations

import pydantic

import jaunt


@jaunt.magic()
class User(pydantic.BaseModel):
    """
    A small validated user model using pydantic v2.

    Behavior:
    - Extra keys are forbidden.
    - id must be >= 1.
    - email is normalized by stripping surrounding whitespace and lowercasing.
    - email must contain exactly one "@" and must not contain any whitespace.

    Errors:
    - Invalid inputs should raise pydantic.ValidationError.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    id: int
    email: str
    is_active: bool = True


@jaunt.magic(deps=User)
def parse_user(data: dict[str, object]) -> User:
    """
    Parse a dict into a User.

    - Use User.model_validate(data) (pydantic v2).
    - Raise pydantic.ValidationError unchanged for invalid inputs.
    """


@jaunt.magic(deps=User)
def user_to_public_dict(user: User) -> dict[str, object]:
    """
    Return a JSON-safe dict for the public user shape.

    - Use user.model_dump(mode="json").
    - Return only keys: id, email, is_active.
    """

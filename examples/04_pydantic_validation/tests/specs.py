from __future__ import annotations

import jaunt


@jaunt.test()
def test_parse_user_normalizes_email_and_applies_defaults() -> None:
    """
    Target: pydantic_demo.parse_user / pydantic_demo.User

    parse_user should:
    - normalize email by stripping + lowercasing
    - default is_active to True
    """

    from pydantic_demo import parse_user

    u = parse_user({"id": 7, "email": "  A@B.Com  "})
    assert u.id == 7
    assert u.email == "a@b.com"
    assert u.is_active is True


@jaunt.test()
def test_parse_user_rejects_invalid_inputs() -> None:
    """
    Target: pydantic_demo.parse_user / pydantic_demo.User

    Invalid inputs should raise pydantic.ValidationError:
    - id == 0
    - invalid email ("no-at-sign")
    - email containing whitespace ("a @ b.com")
    - extra key present
    """

    import pydantic
    from pydantic_demo import parse_user

    for raw in [
        {"id": 0, "email": "a@b.com"},
        {"id": 1, "email": "no-at-sign"},
        {"id": 1, "email": "a @ b.com"},
        {"id": 1, "email": "a@b.com", "extra": "nope"},
    ]:
        try:
            parse_user(raw)
        except pydantic.ValidationError:
            pass
        else:
            raise AssertionError(f"Expected ValidationError for: {raw!r}")


@jaunt.test()
def test_user_to_public_dict_is_json_safe_and_selects_keys() -> None:
    """
    Target: pydantic_demo.user_to_public_dict

    user_to_public_dict should:
    - return a JSON-safe dict for the user
    - return only keys: id, email, is_active
    """

    from pydantic_demo import parse_user, user_to_public_dict

    u = parse_user({"id": 7, "email": "  A@B.Com  "})
    assert user_to_public_dict(u) == {"id": 7, "email": "a@b.com", "is_active": True}

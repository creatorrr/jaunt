from __future__ import annotations

import jaunt


@jaunt.test()
def test_normalize_email__lowercases_and_strips() -> None:
    """
    Target: toy_app.email_specs.normalize_email

    Assert it strips and lowercases, preserving '+' and '.' in the local-part.

    Examples:
    - normalize_email("  A.B+tag@Example.COM  ") == "a.b+tag@example.com"
    """
    from toy_app.email_specs import normalize_email

    assert normalize_email("  A.B+tag@Example.COM  ") == "a.b+tag@example.com"


@jaunt.test()
def test_normalize_email__rejects_invalid_inputs() -> None:
    """
    Target: toy_app.email_specs.normalize_email

    Invalid inputs should raise ValueError:
    - "" (empty)
    - "no-at-sign"
    - "@example.com"
    - "a@"
    - "a@example" (domain missing dot)
    - "a@@example.com" (more than one @)
    """
    import pytest
    from toy_app.email_specs import normalize_email

    invalid_emails = [
        "",
        "no-at-sign",
        "@example.com",
        "a@",
        "a@example",
        "a@@example.com",
    ]
    for raw in invalid_emails:
        with pytest.raises(ValueError):
            normalize_email(raw)


@jaunt.test()
def test_is_corporate_email__matches_domain() -> None:
    """
    Target: toy_app.email_specs.is_corporate_email

    Assert it:
    - returns True for "user@example.com"
    - returns False for "user@other.com"
    - is case-insensitive on the email's domain
    - supports overriding `domain`
    """
    from toy_app.email_specs import is_corporate_email

    assert is_corporate_email("user@example.com") is True
    assert is_corporate_email("user@other.com") is False
    assert is_corporate_email("user@EXAMPLE.com") is True

    assert is_corporate_email("user@corp.example", domain="corp.example") is True
    assert is_corporate_email("user@example.com", domain="corp.example") is False


@jaunt.test()
def test_is_corporate_email__propagates_value_error() -> None:
    """
    Target: toy_app.email_specs.is_corporate_email

    Assert invalid emails propagate ValueError from normalize_email:
    - is_corporate_email("not-an-email") raises ValueError
    """
    import pytest
    from toy_app.email_specs import is_corporate_email

    with pytest.raises(ValueError):
        is_corporate_email("not-an-email")

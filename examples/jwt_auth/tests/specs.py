"""
JWT Authentication — Test Specs
"""

from __future__ import annotations

import jaunt


@jaunt.test()
def test_roundtrip_create_and_verify() -> None:
    """
    Roundtrip create+verify:

    - token = create_token("user-42", "s3cret")
    - claims = verify_token(token, "s3cret")
    - assert claims.sub == "user-42"
    - assert claims.exp > claims.iat
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_expired_token_raises() -> None:
    """
    Expired token raises ValueError("expired"):

    - token = create_token("user-42", "s3cret", ttl=timedelta(seconds=-1))
    - verify_token(token, "s3cret") raises ValueError("expired")
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_wrong_secret_raises() -> None:
    """
    Wrong secret raises ValueError("invalid signature"):

    - token = create_token("user-42", "s3cret")
    - verify_token(token, "different") raises ValueError("invalid signature")
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_tampered_signature_raises() -> None:
    """
    Tampered signature raises ValueError("invalid signature"):

    - token = create_token("user-42", "s3cret")
    - split token on ".", flip one character in the signature segment
    - verify_token(tampered, "s3cret") raises ValueError("invalid signature")
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_malformed_token_raises() -> None:
    """
    Malformed token raises ValueError("malformed"):

    Examples:
    - empty string
    - missing segments (no ".")
    - too many segments
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_rotate_preserves_subject_and_advances_timestamps() -> None:
    """
    Rotate preserves subject and produces later iat/exp:

    - token1 = create_token("user-7", "s3cret")
    - token2 = rotate_token(token1, "s3cret")
    - claims1 = verify_token(token1, "s3cret")
    - claims2 = verify_token(token2, "s3cret")
    - assert claims2.sub == claims1.sub == "user-7"
    - assert claims2.iat > claims1.iat
    - assert claims2.exp > claims1.exp
    """
    raise AssertionError("spec stub (generated at test time)")


"""Aider-specific runtime contract guidance."""

from __future__ import annotations

from typing import Literal

_AIDER_TEST_COVERAGE_GUIDANCE = """## Aider Test Coverage Policy

- Implement every literal setup, call, and assertion described in the test specs.
- Add at most 1-2 extra cases for the generated test module, and only when they
  are direct, obvious extensions of the stated contract.
- Prefer direct contract-adjacent coverage such as boundary/error symmetry or
  one minimal stateful edge case.
- Good extra cases are things like one additional invalid-input symmetry check
  or one minimal cache/missing-key scenario when the contract makes them
  obvious.
- If the written specs already cover the obvious edge cases, do not add more.
- Do not speculate beyond the contract or invent new APIs, wrappers, helpers, or internals.
- Keep generated tests public-API-first.
"""


def aider_contract_addendum(kind: Literal["build", "test"]) -> str:
    if kind == "test":
        return _AIDER_TEST_COVERAGE_GUIDANCE
    return ""


def aider_generation_fingerprint_parts(kind: Literal["build", "test"]) -> list[str]:
    addendum = aider_contract_addendum(kind)
    return [addendum] if addendum else []

"""Aider-specific runtime contract guidance."""

from __future__ import annotations

from typing import Literal

_AIDER_BUILD_GUIDANCE = """## Aider Build Policy

- Reuse handwritten source-module symbols directly when they already exist.
- Prefer static imports from the known dependency modules in the provided context.
- Do not use `importlib`, runtime module loaders, or dynamic imports unless the
  written spec explicitly requires dynamic loading behavior.
- Do not add defensive introspection around known handwritten dataclasses,
  enums, constants, or fields unless the written spec explicitly requires it.
- Avoid helper wrappers that only rename, normalize, or proxy already-typed
  handwritten attributes or constants without adding real behavior.
- When the written spec names visible headings, prompts, help text, labels, or
  other user-facing output, preserve those requirements in the implementation
  rather than dropping or renaming them away.
- Keep implementations straightforward and type-check clean.
"""

_AIDER_TEST_COVERAGE_GUIDANCE = """## Aider Test Coverage Policy

- Implement every literal setup, call, and assertion described in the test specs.
- Add at most 1-2 extra cases for the generated test module, and only when they
  are direct, obvious extensions of the stated contract.
- Prefer direct contract-adjacent coverage such as boundary/error symmetry or
  one minimal stateful edge case.
- If you add extra cases, choose the smallest ones that exercise nearby
  contract-implied behavior rather than expanding the surface area.
- If the written specs already cover the obvious edge cases, do not add more.
- Do not speculate beyond the contract or invent new APIs, wrappers, helpers, or internals.
- Do not monkeypatch, replace, or spy on production module attributes unless a
  written test spec explicitly requires that technique.
- Only assert a specific exception type when the test spec names one
  explicitly. If the spec only says a call should be rejected, assert rejection
  without inventing a stricter exception class.
- For formatted or styled output, prefer semantic content and structure
  assertions over exact control-sequence placement, padding, or wrapped spacing
  unless the spec explicitly requires byte-exact formatting.
- For interactive input flows, assert the visible transcript and behavior
  rather than assuming all user-facing wording appears in the raw prompt string
  passed to an input function.
- Keep generated tests public-API-first.
"""

_AIDER_RUNTIME_POLICY = """## Aider Runtime Policy

- Retry attempts are stateful: the editable target file may already contain a
  previous candidate. Preserve correct code instead of regenerating blindly.
- Treat `context/error_context.md` and `context/retry_strategy.md` as
  authoritative instructions for how to repair the current candidate.
- When fixing a narrow type-check or public-API contract issue, prefer the
  smallest targeted change that makes the candidate pass.
"""

_AIDER_RETRY_MINIMAL = """## Retry Strategy

Failure kind: minimal_repair

- Preserve correct existing code in the target file.
- Make the smallest change needed to satisfy the reported validation or type
  error.
- Do not redesign the module, rename public APIs, or rewrite unrelated logic.
- For type-check failures, prefer targeted import, annotation, cast, or local
  container typing fixes over structural rewrites.
"""

_AIDER_RETRY_EDIT_APPLY = """## Retry Strategy

Failure kind: edit_apply

- A previous diff/search-replace style edit failed to apply cleanly.
- Preserve correct code from the existing target file when possible.
- Rewrite the target file directly instead of relying on exact SEARCH/REPLACE
  matches from the previous attempt.
- Do not re-emit unchanged failed edit blocks.
"""

_AIDER_RETRY_STRUCTURAL = """## Retry Strategy

Failure kind: structural_repair

- Use the previous candidate as a starting point, but you may reorganize the
  file if required to satisfy missing definitions, syntax errors, or larger
  contract violations.
- Preserve correct code where possible and avoid unnecessary redesign.
"""

_AIDER_EDITOR_REASONING_POLICY = """Aider architect retries use a lower-effort
editor by default and can fall back to whole-file editing for reliability."""


def aider_contract_addendum(kind: Literal["build", "test"]) -> str:
    if kind == "build":
        return _AIDER_BUILD_GUIDANCE
    if kind == "test":
        return _AIDER_TEST_COVERAGE_GUIDANCE
    return ""


def aider_retry_strategy_addendum(kind: Literal["build", "test"], strategy: str | None) -> str:
    if not strategy:
        return ""
    if strategy == "minimal_repair":
        return _AIDER_RETRY_MINIMAL
    if strategy == "edit_apply":
        return _AIDER_RETRY_EDIT_APPLY
    if strategy == "structural_repair":
        return _AIDER_RETRY_STRUCTURAL
    return ""


def aider_runtime_policy() -> str:
    return _AIDER_RUNTIME_POLICY


def aider_generation_fingerprint_parts(kind: Literal["build", "test"]) -> list[str]:
    addendum = aider_contract_addendum(kind)
    parts = [
        part for part in (addendum, _AIDER_RUNTIME_POLICY, _AIDER_EDITOR_REASONING_POLICY) if part
    ]
    return parts

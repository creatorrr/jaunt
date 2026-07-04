from __future__ import annotations

import ast
import textwrap

from jaunt.validation import (
    validate_build_generated_source,
    validate_no_import_fallbacks,
)


def test_import_fallback_around_spec_module_rejected() -> None:
    src = textwrap.dedent(
        """
        try:
            from timing import MOCK_TIMING_CALLS
        except ImportError:
            MOCK_TIMING_CALLS = []
        """
    )
    errs = validate_no_import_fallbacks(ast.parse(src), {"timing"})
    assert errs and "fallback" in errs[0]


def test_import_fallback_third_party_allowed() -> None:
    src = "try:\n    import ujson\nexcept ImportError:\n    ujson = None\n"
    assert validate_no_import_fallbacks(ast.parse(src), {"timing"}) == []


def test_import_fallback_module_not_found_error_rejected() -> None:
    src = textwrap.dedent(
        """
        try:
            import timing
        except ModuleNotFoundError:
            timing = None
        """
    )
    errs = validate_no_import_fallbacks(ast.parse(src), {"timing"})
    assert errs and "fallback" in errs[0]


def test_import_fallback_relative_import_rejected() -> None:
    # A relative import guarded by ImportError is always a fallback hazard,
    # regardless of the protected-modules set.
    src = textwrap.dedent(
        """
        try:
            from . import helpers
        except ImportError:
            helpers = None
        """
    )
    errs = validate_no_import_fallbacks(ast.parse(src), set())
    assert errs and "fallback" in errs[0]


def test_import_fallback_bare_except_counts() -> None:
    src = textwrap.dedent(
        """
        try:
            from timing import MockTimer
        except Exception:
            MockTimer = None
        """
    )
    errs = validate_no_import_fallbacks(ast.parse(src), {"timing"})
    assert errs and "fallback" in errs[0]


def test_import_fallback_no_try_is_clean() -> None:
    src = "from timing import MockTimer\n"
    assert validate_no_import_fallbacks(ast.parse(src), {"timing"}) == []


def test_self_import_of_spec_symbol_rejected() -> None:
    # Generated source defines MockTimer itself, then re-imports it from the spec
    # module — a circular-forwarding hazard flagged via the build entrypoint.
    src = textwrap.dedent(
        """
        class MockTimer:
            pass

        from timing import MockTimer as MockTimer
        """
    )
    errs = validate_build_generated_source(
        src,
        ["MockTimer"],
        spec_module="timing",
        handwritten_names=(),
    )
    assert any("MockTimer" in e and "timing" in e for e in errs)


def test_self_import_of_handwritten_symbol_allowed() -> None:
    # Reusing a genuinely handwritten symbol from the spec module is fine.
    src = textwrap.dedent(
        """
        from timing import HELPER as HELPER

        def build() -> int:
            return HELPER()
        """
    )
    errs = validate_build_generated_source(
        src,
        ["build"],
        spec_module="timing",
        handwritten_names=("HELPER",),
    )
    assert not any("re-imports its own spec symbol" in e for e in errs)

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


def test_import_fallback_true_bare_except_counts() -> None:
    # A truly bare `except:` (no exception type) catches everything, so a guarded
    # protected import under it is still a fallback hazard.
    src = "try:\n    from timing import MockTimer\nexcept:\n    MockTimer = None\n"
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


def test_self_import_of_spec_symbol_via_relative_rejected() -> None:
    # Generated source lives in `pkg.__generated__.mod`; a relative `from ..mod import Foo`
    # resolves to the spec module `pkg.mod` and re-imports its own spec symbol.
    src = textwrap.dedent(
        """
        class Foo:
            pass

        from ..mod import Foo as Foo
        """
    )
    errs = validate_build_generated_source(
        src,
        ["Foo"],
        spec_module="pkg.mod",
        handwritten_names=(),
        generated_module="pkg.__generated__.mod",
    )
    assert any("Foo" in e and "pkg.mod" in e for e in errs)


def test_self_import_star_from_spec_module_rejected() -> None:
    # `from <spec_module> import *` pulls the spec symbols back into the generated
    # module (absolute form) and must be flagged.
    src = textwrap.dedent(
        """
        def build() -> int:
            return 1

        from timing import *
        """
    )
    errs = validate_build_generated_source(
        src,
        ["build"],
        spec_module="timing",
        handwritten_names=(),
    )
    assert any("import *" in e and "timing" in e for e in errs)


def test_self_import_star_from_spec_module_via_relative_rejected() -> None:
    src = textwrap.dedent(
        """
        def build() -> int:
            return 1

        from ..mod import *
        """
    )
    errs = validate_build_generated_source(
        src,
        ["build"],
        spec_module="pkg.mod",
        handwritten_names=(),
        generated_module="pkg.__generated__.mod",
    )
    assert any("import *" in e and "pkg.mod" in e for e in errs)


def test_spec_symbol_rebind_via_plain_import_rejected() -> None:
    # `import <spec_module>` (aliased) + a module-level rebind `X = m.X` re-pulls the
    # spec module's wrapped stub — the plain-import twin of the `from ... import X`
    # hazard, and must be flagged too (finding 1, PR #63).
    src = textwrap.dedent(
        """
        import timing as _t

        class MockTimer:
            pass

        MockTimer = _t.MockTimer
        """
    )
    errs = validate_build_generated_source(
        src,
        ["MockTimer"],
        spec_module="timing",
        handwritten_names=(),
    )
    assert any("MockTimer" in e and "timing" in e and "rebind" in e for e in errs)


def test_spec_symbol_rebind_via_dotted_plain_import_rejected() -> None:
    # The un-aliased dotted form `import pkg.mod` + `Foo = pkg.mod.Foo`.
    src = textwrap.dedent(
        """
        import pkg.mod

        class Foo:
            pass

        Foo = pkg.mod.Foo
        """
    )
    errs = validate_build_generated_source(
        src,
        ["Foo"],
        spec_module="pkg.mod",
        handwritten_names=(),
        generated_module="pkg.__generated__.mod",
    )
    assert any("Foo" in e and "pkg.mod" in e and "rebind" in e for e in errs)


def test_rebind_from_unrelated_module_allowed() -> None:
    # Rebinding from a genuinely unrelated module is not a self-import; this check
    # must not flag it.
    src = textwrap.dedent(
        """
        import other

        def build() -> int:
            return 1

        build = other.build
        """
    )
    errs = validate_build_generated_source(
        src,
        ["build"],
        spec_module="timing",
        handwritten_names=(),
    )
    assert not any("rebind" in e for e in errs)


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

import ast
import textwrap

from jaunt.module_magic import ModuleSpecCandidate, scan_module_source


def _scan(src: str):
    return scan_module_source(ast.parse(textwrap.dedent(src)), module="m")


def test_ellipsis_and_docstring_and_pass_and_nie_bodies_are_specs():
    scan = _scan("""
        def a(x: int) -> int:
            ...
        def b():
            "doc only"
        def c():
            pass
        async def d():
            raise NotImplementedError
    """)
    assert {c.name for c in scan.candidates} == {"a", "b", "c", "d"}
    assert all(not c.is_class for c in scan.candidates)


def test_runtime_error_body_is_not_a_stub():
    scan = _scan("""
        def f():
            raise RuntimeError("spec stub")
    """)
    assert scan.candidates == ()


def test_real_body_is_handwritten():
    scan = _scan("""
        def f(x):
            return x + 1
    """)
    assert scan.candidates == ()


def test_docstring_only_class_is_whole_class_spec():
    scan = _scan("""
        class Email:
            \"\"\"Email object.\"\"\"
    """)
    assert scan.candidates == (ModuleSpecCandidate(name="Email", is_class=True),)


def test_class_with_one_stub_method_is_spec_and_all_real_is_not():
    scan = _scan("""
        class Mixed:
            def done(self):
                return 1
            def todo(self):
                \"\"\"stub\"\"\"
        class Done:
            def done(self):
                return 1
    """)
    assert {c.name for c in scan.candidates} == {"Mixed"}


def test_jaunt_decorated_defs_are_skipped_plain_and_aliased():
    scan = _scan("""
        import jaunt
        import jaunt as j
        from jaunt import magic as m

        @jaunt.magic()
        def a(): ...
        @j.magic
        def b(): ...
        @m
        def c(): ...
        @jaunt.preserve
        def intentionally_empty(): ...
    """)
    assert scan.candidates == ()


def test_non_jaunt_decorated_defs_are_never_governed():
    scan = _scan("""
        import typing
        import functools
        from dataclasses import dataclass

        @typing.overload
        def f(x: int) -> int: ...
        @typing.overload
        def f(x: str) -> str: ...
        def f(x): return x

        @property
        def broken(self): ...
        @functools.cache
        def cached(): ...

        @dataclass
        class Config:
            \"\"\"fields\"\"\"
    """)
    assert scan.candidates == ()


def test_conditional_defs_are_not_governed():
    scan = _scan("""
        if True:
            def f(): ...
    """)
    assert scan.candidates == ()


def test_capture_warnings_for_toplevel_subclass_and_call():
    scan = _scan("""
        class Email:
            \"\"\"spec\"\"\"
        def parse(raw: str) -> Email:
            ...
        class Signed(Email):
            def sign(self): return 1
        DEFAULT = parse("x")
    """)
    assert len(scan.warnings) == 2
    assert "Signed" in scan.warnings[0] and "Email" in scan.warnings[0]
    assert "parse" in scan.warnings[1]

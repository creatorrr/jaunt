from __future__ import annotations

from typing import Any

from jaunt.validation import class_build_warnings, validate_build_class_source


BASE_KW: dict[str, Any] = dict(
    class_name="C",
    stub_methods=["do"],
    preserved_segments={},
    declared_bases=[],
    class_decorators=[],
    required_abstractmethods=[],
    spec_docstring="A class.",
)


def _kw(**overrides: Any) -> dict[str, Any]:
    return {**BASE_KW, **overrides}


def test_passes_when_structure_matches() -> None:
    src = 'class C:\n    "A class. Extra notes."\n    def do(self):\n        return 1\n'
    assert validate_build_class_source(src, **BASE_KW) == []


def test_fails_when_stub_method_missing() -> None:
    src = 'class C:\n    "A class."\n    def other(self):\n        return 1\n'
    errs = validate_build_class_source(src, **BASE_KW)
    assert any("do" in e for e in errs)


def test_fails_when_base_dropped() -> None:
    src = 'class C:\n    "A class."\n    def do(self): return 1\n'
    kw = _kw(declared_bases=["Base"])
    errs = validate_build_class_source(src, **kw)
    assert any("Base" in e for e in errs)


def test_fails_when_abstractmethod_unimplemented() -> None:
    src = 'class C(Base):\n    "A class."\n    def do(self): return 1\n'
    kw = _kw(declared_bases=["Base"], required_abstractmethods=["needed"])
    errs = validate_build_class_source(src, **kw)
    assert any("needed" in e for e in errs)


def test_fails_when_preserved_method_drifts() -> None:
    spec_seg = "def kept(self):\n    return 42"
    src = (
        'class C:\n    "A class."\n'
        "    def do(self): return 1\n"
        "    def kept(self):\n        return 99\n"
    )
    kw = _kw(preserved_segments={"kept": spec_seg})
    errs = validate_build_class_source(src, **kw)
    assert any("kept" in e for e in errs)


def test_passes_when_preserved_method_intact_modulo_formatting() -> None:
    spec_seg = "def kept(self):\n    return 42"
    src = (
        'class C:\n    "A class."\n'
        "    def do(self): return 1\n"
        "    def kept(self):\n        return 42\n"
    )
    kw = _kw(preserved_segments={"kept": spec_seg})
    assert validate_build_class_source(src, **kw) == []


def test_fails_when_docstring_dropped() -> None:
    src = 'class C:\n    "Totally different."\n    def do(self): return 1\n'
    errs = validate_build_class_source(src, **BASE_KW)
    assert any("docstring" in e.lower() for e in errs)


def test_extra_private_methods_allowed() -> None:
    src = (
        'class C:\n    "A class."\n'
        "    def do(self): return self._helper()\n"
        "    def _helper(self): return 1\n"
    )
    assert validate_build_class_source(src, **BASE_KW) == []


def test_dropped_param_warns_not_fails() -> None:
    src = 'class C:\n    "A class."\n    def do(self): return 1\n'
    # validation passes (warn-only)
    assert validate_build_class_source(src, **BASE_KW) == []
    warns = class_build_warnings(src, class_name="C", stub_signatures={"do": ["self", "x"]})
    assert any("x" in w for w in warns)

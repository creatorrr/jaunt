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


def test_multiline_docstring_retained_modulo_whitespace_reflow() -> None:
    # docstring-only spec docstrings are multi-line; the LLM commonly reflows the
    # internal whitespace. Retention must be whitespace-tolerant (see Inventory in
    # examples/06_whole_class surfacing this end-to-end).
    spec_doc = "An item store. Supports add(item, qty),\n    remove(item, qty), and total()."
    generated = (
        "class C:\n"
        '    """An item store. Supports add(item, qty), remove(item, qty), and total().\n\n'
        "    Quantities never go below zero.\n"
        '    """\n'
        "    def do(self):\n        return 1\n"
    )
    kw = _kw(spec_docstring=spec_doc)
    assert validate_build_class_source(generated, **kw) == []


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


def test_fails_when_stub_left_unfilled_even_if_sentinel_stripped() -> None:
    src = 'class C:\n    "A class."\n    def do(self):\n        raise NotImplementedError\n'
    errs = validate_build_class_source(src, **BASE_KW)
    assert any("stub" in e for e in errs)


def test_passes_when_stub_filled() -> None:
    src = 'class C:\n    "A class."\n    def do(self):\n        return 1\n'
    assert validate_build_class_source(src, **BASE_KW) == []


def test_docstring_only_empty_class_fails() -> None:
    src = 'class C:\n    "A class."\n    pass\n'
    kw = _kw(stub_methods=[], require_public_method=True)
    errs = validate_build_class_source(src, **kw)
    assert any("public method" in e for e in errs)


def test_docstring_only_with_public_method_passes() -> None:
    src = 'class C:\n    "A class."\n    def total(self):\n        return 0\n'
    kw = _kw(stub_methods=[], require_public_method=True)
    assert validate_build_class_source(src, **kw) == []


def test_fails_when_class_attribute_dropped() -> None:
    src = 'class C:\n    "A class."\n    def do(self):\n        return 1\n'
    kw = _kw(class_attributes={"CAPACITY": "CAPACITY: int = 10"})
    errs = validate_build_class_source(src, **kw)
    assert any("CAPACITY" in e for e in errs)


def test_fails_when_class_attribute_value_changed() -> None:
    src = 'class C:\n    "A class."\n    CAPACITY = None\n    def do(self):\n        return 1\n'
    kw = _kw(class_attributes={"CAPACITY": "CAPACITY: int = 10"})
    errs = validate_build_class_source(src, **kw)
    assert any("CAPACITY" in e for e in errs)


def test_passes_when_class_attribute_retained() -> None:
    src = 'class C:\n    "A class."\n    CAPACITY: int = 10\n    def do(self):\n        return 1\n'
    kw = _kw(class_attributes={"CAPACITY": "CAPACITY: int = 10"})
    assert validate_build_class_source(src, **kw) == []

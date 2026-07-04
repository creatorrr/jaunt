from __future__ import annotations

import abc
import ast

import pytest

from jaunt.class_analysis import (
    BaseContract,
    MemberSplit,
    build_class_scaffold,
    classify_class_mode,
    is_magic_decorator,
    is_preserve_decorator,
    is_stub_body,
    render_whole_class_contract,
    resolve_base_contract,
    split_class_members,
)
from jaunt.errors import JauntError


def _cls(src: str) -> ast.ClassDef:
    node = ast.parse(src).body[0]
    assert isinstance(node, ast.ClassDef)
    return node


def test_is_stub_body_recognizes_emptyish_bodies() -> None:
    for body in ("...", "pass", "raise NotImplementedError", "raise NotImplementedError('x')"):
        cls = _cls(f"class C:\n    def m(self):\n        {body}\n")
        fn = cls.body[0]
        assert isinstance(fn, ast.FunctionDef)
        assert is_stub_body(fn) is True


def test_is_stub_body_recognizes_docstring_plus_ellipsis() -> None:
    cls = _cls('class C:\n    def m(self):\n        "doc"\n        ...\n')
    fn = cls.body[0]
    assert isinstance(fn, ast.FunctionDef)
    assert is_stub_body(fn) is True


def test_is_stub_body_rejects_real_body() -> None:
    cls = _cls("class C:\n    def m(self):\n        return 1\n")
    fn = cls.body[0]
    assert isinstance(fn, ast.FunctionDef)
    assert is_stub_body(fn) is False


def test_preserve_decorator_detected_both_forms() -> None:
    cls = _cls(
        "class C:\n"
        "    @jaunt.preserve\n"
        "    def a(self): ...\n"
        "    @preserve()\n"
        "    def b(self): ...\n"
        "    @other\n"
        "    def c(self): ...\n"
    )
    decs = {fn.name: fn.decorator_list for fn in cls.body if isinstance(fn, ast.FunctionDef)}
    assert any(is_preserve_decorator(d) for d in decs["a"])
    assert any(is_preserve_decorator(d) for d in decs["b"])
    assert not any(is_preserve_decorator(d) for d in decs["c"])


def test_is_magic_decorator_detected_both_forms() -> None:
    cls = _cls(
        "class C:\n"
        "    @jaunt.magic\n"
        "    def a(self): ...\n"
        "    @magic()\n"
        "    def b(self): ...\n"
        "    @other\n"
        "    def c(self): ...\n"
    )
    decs = {fn.name: fn.decorator_list for fn in cls.body if isinstance(fn, ast.FunctionDef)}
    assert any(is_magic_decorator(d) for d in decs["a"])
    assert any(is_magic_decorator(d) for d in decs["b"])
    assert not any(is_magic_decorator(d) for d in decs["c"])


def test_split_class_members_uses_heuristic_and_preserve() -> None:
    cls = _cls(
        "class C:\n"
        '    """spec"""\n'
        "    X = 1\n"
        "    def stub(self): ...\n"
        "    def real(self):\n        return 2\n"
        "    @jaunt.preserve\n"
        "    def kept_stub(self): ...\n"
    )
    split = split_class_members(cls)
    assert split == MemberSplit(
        stubs=("stub",),
        sealed=(),
        preserved=("kept_stub", "real"),
        preserve_marked=("kept_stub",),
    )


def test_classify_class_mode() -> None:
    docstring_only = _cls('class C:\n    """just a spec"""\n')
    stubs = _cls("class C:\n    def a(self): ...\n    def b(self): ...\n")
    mix = _cls("class C:\n    def a(self): ...\n    def b(self):\n        return 1\n")
    assert classify_class_mode(docstring_only) == "docstring_only"
    assert classify_class_mode(stubs) == "stubs"
    assert classify_class_mode(mix) == "mix"


def test_resolve_base_contract_collects_abstractmethods_and_signatures() -> None:
    class Base(abc.ABC):
        @abc.abstractmethod
        def required(self, x: int) -> int: ...

        def helper(self) -> str:
            return "h"

    class Child(Base):
        """spec"""

    contract = resolve_base_contract(Child)
    assert isinstance(contract, BaseContract)
    assert "required" in contract.required_abstractmethods
    assert "required" in contract.block
    assert "helper" in contract.block  # inherited public method is offered as context


def test_resolve_base_contract_no_bases() -> None:
    class Plain:
        """spec"""

    contract = resolve_base_contract(Plain)
    assert contract.required_abstractmethods == ()
    # object has no public spec-relevant members worth surfacing
    assert contract.project_base_refs == ()


def test_split_sealed_subset_of_stubs() -> None:
    cls = _cls(
        "class C:\n"
        "    @jaunt.magic\n"
        "    def locked(self, x: int) -> int: ...\n"
        "    def sketch(self): ...\n"
        "    def real(self):\n        return 1\n"
    )
    split = split_class_members(cls)
    assert split.sealed == ("locked",)
    assert set(split.sealed) <= set(split.stubs)
    assert split.stubs == ("locked", "sketch")
    assert split.preserved == ("real",)


def test_magic_plus_preserve_raises() -> None:
    cls = _cls("class C:\n    @jaunt.magic\n    @jaunt.preserve\n    def m(self): ...\n")
    with pytest.raises(JauntError, match="preserve"):
        split_class_members(cls)


def test_magic_on_non_stub_body_raises() -> None:
    cls = _cls("class C:\n    @jaunt.magic\n    def m(self):\n        return 1\n")
    with pytest.raises(JauntError, match="preserve"):
        split_class_members(cls)


def test_magic_on_property_raises() -> None:
    cls = _cls("class C:\n    @property\n    @jaunt.magic\n    def m(self) -> int: ...\n")
    with pytest.raises(JauntError, match="property"):
        split_class_members(cls)


def test_classify_counts_sealed_as_stubs() -> None:
    cls = _cls("class C:\n    @jaunt.magic\n    def m(self): ...\n")
    assert classify_class_mode(cls) == "stubs"


def test_scaffold_strips_inner_magic() -> None:
    seg = (
        "@jaunt.magic()\n"
        "class C:\n"
        '    """doc"""\n'
        "    @jaunt.magic\n"
        "    def locked(self, x: int) -> int: ...\n"
    )
    out = build_class_scaffold(seg)
    assert "@jaunt.magic" not in out
    assert "def locked(self, x: int) -> int:" in out
    assert "# jaunt:implement" in out


def test_sig_decorator_seals_method_both_forms() -> None:
    cls = _cls(
        "class C:\n"
        "    @jaunt.sig\n"
        "    def locked(self, x: int) -> int: ...\n"
        "    @sig()\n"
        "    def locked2(self, y: int) -> int: ...\n"
        "    def sketch(self): ...\n"
        "    def real(self):\n        return 1\n"
    )
    split = split_class_members(cls)
    assert split.sealed == ("locked", "locked2")
    assert set(split.sealed) <= set(split.stubs)
    assert split.preserved == ("real",)


def test_sig_and_inner_magic_seal_equivalently() -> None:
    via_sig = split_class_members(
        _cls("class C:\n    @jaunt.sig\n    def m(self, x: int) -> int: ...\n")
    )
    via_magic = split_class_members(
        _cls("class C:\n    @jaunt.magic\n    def m(self, x: int) -> int: ...\n")
    )
    assert via_sig == via_magic
    assert via_sig.sealed == ("m",)


def test_sig_on_non_stub_body_raises() -> None:
    cls = _cls("class C:\n    @jaunt.sig\n    def m(self):\n        return 1\n")
    with pytest.raises(JauntError):
        split_class_members(cls)


def test_sig_on_property_raises() -> None:
    cls = _cls("class C:\n    @property\n    @jaunt.sig\n    def m(self) -> int: ...\n")
    with pytest.raises(JauntError, match="property"):
        split_class_members(cls)


def test_scaffold_strips_sig() -> None:
    seg = (
        "@jaunt.magic()\n"
        "class C:\n"
        '    """doc"""\n'
        "    @jaunt.sig\n"
        "    def locked(self, x: int) -> int: ...\n"
    )
    out = build_class_scaffold(seg)
    assert "@jaunt.sig" not in out
    assert "@sig" not in out
    assert "def locked(self, x: int) -> int:" in out
    assert "# jaunt:implement" in out


def test_contract_renders_three_tiers_and_composition() -> None:
    seg = (
        "@jaunt.magic()\n"
        "class C:\n"
        '    """doc"""\n'
        "    @jaunt.magic\n"
        "    def locked(self, x: int) -> int: ...\n"
        "    def sketch(self): ...\n"
        "    @jaunt.preserve\n"
        "    def keep(self):\n        return 1\n"
    )
    out = render_whole_class_contract(class_segment=seg, base_contract_block="")
    assert "exactly" in out and "locked(self, x: int) -> int" in out  # sealed w/ signature
    assert "sketches of intent" in out and "C.sketch" in out  # guidepost
    assert "EXACTLY as written" in out and "C.keep" in out  # preserved
    assert "small, single-purpose methods" in out  # composition, always on


def test_contract_renders_inherited_api_block() -> None:
    out = render_whole_class_contract(
        class_segment="@jaunt.magic()\nclass C:\n    def m(self): ...\n",
        base_contract_block="",
        inherited_api_block="Base.run(self) -> None\n  doc: run it",
    )
    assert "Inherited generated API" in out
    assert "Base.run(self) -> None" in out

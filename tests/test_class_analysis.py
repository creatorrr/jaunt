from __future__ import annotations

import ast

from jaunt.class_analysis import (
    MemberSplit,
    classify_class_mode,
    is_preserve_decorator,
    is_stub_body,
    split_class_members,
)


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

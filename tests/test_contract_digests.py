"""Contract digest widening: async + class nodes, byte-compat for sync functions."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from jaunt.digest import ContractDigests, contract_digests, load_contract_node


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(tmp_path: Path, source: str) -> str:
    p = tmp_path / "m.py"
    p.write_text(source, encoding="utf-8")
    return str(p)


SYNC_FN = '''
def f(a: int, b: int = 2) -> int:
    """Add things.

    Examples:
        - f(1) -> 3
    """
    return a + b
'''

STUB_FN = '''
def g(a: int) -> int:
    """Stubby."""
    raise NotImplementedError
'''


class TestSyncByteCompat:
    def test_sync_function_digests_are_golden(self, tmp_path: Path) -> None:
        src_file = _write(tmp_path, SYNC_FN)
        digs = contract_digests(src_file, "f")
        node = ast.parse(SYNC_FN).body[0]
        assert isinstance(node, ast.FunctionDef)
        assert node.returns is not None
        prose = ast.get_docstring(node, clean=True) or ""
        sig = ast.unparse(node.args) + " -> " + ast.unparse(node.returns)
        body = "\n".join(ast.unparse(s) for s in node.body[1:])
        assert digs == ContractDigests(prose=_sha(prose), signature=_sha(sig), body=_sha(body))

    def test_stub_body_is_hashed_not_elided(self, tmp_path: Path) -> None:
        src_file = _write(tmp_path, STUB_FN)
        digs = contract_digests(src_file, "g")
        assert digs.body == _sha("raise NotImplementedError")
        assert digs.body != _sha("")


class TestAsync:
    def test_async_signature_has_prefix_and_flip_changes_digest(self, tmp_path: Path) -> None:
        sync_file = _write(tmp_path, "def f(a: int) -> int:\n    return a\n")
        sync_digs = contract_digests(sync_file, "f")
        (tmp_path / "m.py").write_text(
            "async def f(a: int) -> int:\n    return a\n", encoding="utf-8"
        )
        async_digs = contract_digests(str(tmp_path / "m.py"), "f")
        assert sync_digs.signature != async_digs.signature
        assert sync_digs.body == async_digs.body

    def test_loader_returns_async_node(self, tmp_path: Path) -> None:
        src_file = _write(tmp_path, "async def f() -> None:\n    pass\n")
        assert isinstance(load_contract_node(src_file, "f"), ast.AsyncFunctionDef)


CLASS_SRC = '''
class Counter:
    """Counts things."""

    start = 0

    def __init__(self, start: int = 0) -> None:
        self.n = start

    def increment(self, by: int) -> int:
        """Bump.

        Examples:
            - Counter().increment(1) == 1
        """
        self.n += by
        return self.n

    async def aincrement(self, by: int) -> int:
        """Async bump."""
        return self.n + by

    def _private(self) -> None:
        pass
'''


class TestClassDigests:
    def test_loader_returns_class_node(self, tmp_path: Path) -> None:
        src_file = _write(tmp_path, CLASS_SRC)
        assert isinstance(load_contract_node(src_file, "Counter"), ast.ClassDef)

    def test_dotted_qualname_rejected(self, tmp_path: Path) -> None:
        src_file = _write(tmp_path, CLASS_SRC)
        with pytest.raises(ValueError, match="whole class"):
            load_contract_node(src_file, "Counter.increment")

    def test_method_docstring_edit_changes_prose_not_signature(self, tmp_path: Path) -> None:
        f1 = _write(tmp_path, CLASS_SRC)
        d1 = contract_digests(f1, "Counter")
        (tmp_path / "m.py").write_text(CLASS_SRC.replace("Bump.", "Bump twice."), "utf-8")
        d2 = contract_digests(str(tmp_path / "m.py"), "Counter")
        assert d1.prose != d2.prose
        assert d1.signature == d2.signature
        assert d1.body == d2.body

    def test_private_method_docstring_not_in_prose(self, tmp_path: Path) -> None:
        f1 = _write(tmp_path, CLASS_SRC)
        d1 = contract_digests(f1, "Counter")
        edited = CLASS_SRC.replace(
            "def _private(self) -> None:\n        pass",
            'def _private(self) -> None:\n        """Doc."""\n        pass',
        )
        (tmp_path / "m.py").write_text(edited, "utf-8")
        d2 = contract_digests(str(tmp_path / "m.py"), "Counter")
        assert d1.prose == d2.prose  # private docstring invisible to prose
        assert d1.body != d2.body  # but the body changed

    def test_method_add_changes_signature(self, tmp_path: Path) -> None:
        f1 = _write(tmp_path, CLASS_SRC)
        d1 = contract_digests(f1, "Counter")
        (tmp_path / "m.py").write_text(
            CLASS_SRC + "\n    def reset(self) -> None:\n        self.n = 0\n"
            if False
            else CLASS_SRC.rstrip() + "\n\n    def reset(self) -> None:\n        self.n = 0\n",
            "utf-8",
        )
        d2 = contract_digests(str(tmp_path / "m.py"), "Counter")
        assert d1.signature != d2.signature

    def test_body_only_edit_changes_body_only(self, tmp_path: Path) -> None:
        f1 = _write(tmp_path, CLASS_SRC)
        d1 = contract_digests(f1, "Counter")
        (tmp_path / "m.py").write_text(
            CLASS_SRC.replace("self.n += by", "self.n = self.n + by"), "utf-8"
        )
        d2 = contract_digests(str(tmp_path / "m.py"), "Counter")
        assert d1.body != d2.body
        assert d1.signature == d2.signature
        assert d1.prose == d2.prose

    def test_class_attribute_value_edit_changes_body_only(self, tmp_path: Path) -> None:
        f1 = _write(tmp_path, CLASS_SRC)
        d1 = contract_digests(f1, "Counter")
        (tmp_path / "m.py").write_text(CLASS_SRC.replace("start = 0", "start = 1"), "utf-8")
        d2 = contract_digests(str(tmp_path / "m.py"), "Counter")
        assert d1.body != d2.body
        assert d1.signature == d2.signature
        assert d1.prose == d2.prose

    def test_class_decorator_edit_changes_signature(self, tmp_path: Path) -> None:
        src1 = "@final\n" + CLASS_SRC
        src2 = "@sealed\n" + CLASS_SRC
        f1 = _write(tmp_path, src1)
        d1 = contract_digests(f1, "Counter")
        (tmp_path / "m.py").write_text(src2, "utf-8")
        d2 = contract_digests(str(tmp_path / "m.py"), "Counter")
        assert d1.signature != d2.signature

    def test_method_decorator_edit_changes_signature_only(self, tmp_path: Path) -> None:
        f1 = _write(tmp_path, CLASS_SRC)
        d1 = contract_digests(f1, "Counter")
        edited = CLASS_SRC.replace(
            "    def increment(self, by: int) -> int:",
            "    @staticmethod\n    def increment(self, by: int) -> int:",
        )
        (tmp_path / "m.py").write_text(edited, "utf-8")
        d2 = contract_digests(str(tmp_path / "m.py"), "Counter")
        assert d1.signature != d2.signature
        assert d1.prose == d2.prose
        assert d1.body == d2.body

    def test_method_decorator_order_changes_signature(self, tmp_path: Path) -> None:
        src1 = """
class C:
    @outer
    @inner
    def f(self) -> int:
        return 1
"""
        src2 = src1.replace("    @outer\n    @inner", "    @inner\n    @outer")
        f1 = _write(tmp_path, src1)
        d1 = contract_digests(f1, "C")
        (tmp_path / "m.py").write_text(src2, "utf-8")
        d2 = contract_digests(str(tmp_path / "m.py"), "C")
        assert d1.signature != d2.signature

from __future__ import annotations

import ast

from jaunt.class_analysis import (
    build_class_scaffold,
    collect_spec_module_imports,
    render_whole_class_contract,
)

STUB_CLASS = (
    "@jaunt.magic()\n"
    "class Stack(Base):\n"
    '    """A stack. LIFO."""\n'
    "    CAPACITY: int = 10\n"
    "    def push(self, x: int) -> None:\n"
    '        """Push x."""\n'
    "        ...\n"
    "    @jaunt.preserve\n"
    "    def is_empty(self) -> bool:\n"
    "        return self._n == 0\n"
)

DOCSTRING_ONLY = '@jaunt.magic()\nclass Inv:\n    """An inventory. add/remove/total."""\n'


def test_collect_imports_includes_all_top_level_imports() -> None:
    src = (
        "import os\n"
        "from typing import Any\n"
        "import jaunt\n\n"
        "@jaunt.magic()\n"
        "class C:\n"
        "    import sys  # not top-level\n"
        "    def f(self): ...\n"
    )
    imports = collect_spec_module_imports(src)
    assert "import os" in imports
    assert "from typing import Any" in imports
    assert "import jaunt" in imports
    assert all("sys" not in imp for imp in imports)


def test_scaffold_renders_header_attrs_docstring_preserved_and_sentinel_stub() -> None:
    scaffold = build_class_scaffold(STUB_CLASS)
    tree = ast.parse(scaffold)  # must be valid Python
    cls = tree.body[0]
    assert isinstance(cls, ast.ClassDef)
    # base + class attribute + docstring retained
    assert "Base" in {ast.unparse(b) for b in cls.bases}
    assert "CAPACITY" in scaffold and "= 10" in scaffold
    assert "A stack. LIFO." in scaffold
    # @magic stripped from the header
    assert "@jaunt.magic" not in scaffold
    # preserved method body kept, @jaunt.preserve stripped
    assert "self._n == 0" in scaffold
    assert "@jaunt.preserve" not in scaffold
    # stub becomes a sentinel body
    assert "# jaunt:implement" in scaffold
    assert "jaunt: implement Stack.push per the spec" in scaffold


def test_scaffold_docstring_only_is_header_docstring_pass() -> None:
    scaffold = build_class_scaffold(DOCSTRING_ONLY)
    ast.parse(scaffold)
    assert "An inventory" in scaffold
    assert scaffold.rstrip().endswith("pass")


def test_contract_lists_fill_preserve_and_docstring_only_directive() -> None:
    c1 = render_whole_class_contract(
        class_segment=STUB_CLASS, base_contract_block="(no base classes)"
    )
    assert "Stack.push" in c1
    assert "Stack.is_empty" in c1
    assert "jaunt:implement" in c1
    c2 = render_whole_class_contract(
        class_segment=DOCSTRING_ONLY, base_contract_block="(no base classes)"
    )
    assert "public method" in c2.lower()

"""Tests for `.pyi` stub emission (jaunt 1.3.0, finding 3 part 2)."""

from __future__ import annotations

import textwrap
from pathlib import Path

from jaunt.header import format_stub_header, parse_stub_header
from jaunt.stub_emitter import (
    build_stub_source,
    generated_content_digest,
    is_jaunt_stub,
    stub_inputs_digest,
    stub_path_for_source,
    stub_staleness,
)


def _header() -> str:
    return format_stub_header(
        tool_version="0",
        source_module="timing",
        generated_digest=generated_content_digest("x = 1\n"),
        inputs_digest=stub_inputs_digest("x = 1\n", "x = 1\n"),
    )


def test_docstring_only_class_gains_designed_init() -> None:
    """A docstring-only whole-class spec exposes the generated __init__/methods."""
    spec_source = textwrap.dedent(
        '''
        import jaunt


        @jaunt.magic()
        class Timer:
            """A stopwatch timer. Design __init__ taking a name and an elapsed() reader."""
        '''
    )
    generated_source = textwrap.dedent(
        """
        class Timer:
            def __init__(self, name: str) -> None:
                self.name = name
                self._started = False

            def elapsed(self) -> float:
                return 0.0
        """
    )
    stub = build_stub_source(spec_source, generated_source, {"Timer"}, _header())
    assert "class Timer:" in stub
    assert "def __init__(self, name: str) -> None:" in stub
    assert "def elapsed(self) -> float:" in stub
    # Bodies are elided; no real implementation leaks into the stub.
    assert "self._started" not in stub
    assert "return 0.0" not in stub
    # The class-level @jaunt.magic decorator does not appear in the stub.
    assert "@jaunt.magic" not in stub


def test_handwritten_function_keeps_annotations() -> None:
    """A handwritten (non-spec) function keeps its exact spec-module signature."""
    spec_source = textwrap.dedent(
        """
        def helper(x: int, *, flag: bool = False) -> str:
            return str(x)
        """
    )
    stub = build_stub_source(spec_source, "", set(), _header())
    normalized = stub.replace(" ", "")
    assert "defhelper(x:int,*,flag:bool=False)->str:" in normalized
    assert "return str(x)" not in stub


def test_decorated_spec_function_appears_undecorated() -> None:
    """A decorated spec function appears with the generated signature and no decorator."""
    spec_source = textwrap.dedent(
        '''
        import jaunt


        @jaunt.magic()
        def slugify(text: str) -> str:
            """Slugify."""
            ...
        '''
    )
    generated_source = textwrap.dedent(
        """
        def slugify(text: str) -> str:
            return text.lower()
        """
    )
    stub = build_stub_source(spec_source, generated_source, {"slugify"}, _header())
    assert "def slugify(text: str) -> str:" in stub
    assert "@jaunt.magic" not in stub
    assert "return text.lower()" not in stub


def test_module_all_and_constants_preserved() -> None:
    spec_source = textwrap.dedent(
        """
        __all__ = ["slugify"]
        MAX_LEN: int = 80
        """
    )
    stub = build_stub_source(spec_source, "", set(), _header())
    assert '__all__ = ["slugify"]' in stub or "__all__ = ['slugify']" in stub
    assert "MAX_LEN: int = 80" in stub


def test_build_stub_source_deterministic() -> None:
    spec_source = textwrap.dedent(
        '''
        import jaunt


        @jaunt.magic()
        def a(x: int) -> int:
            """A."""
            ...


        @jaunt.magic()
        def b(y: int) -> int:
            """B."""
            ...
        '''
    )
    generated_source = textwrap.dedent(
        """
        def a(x: int) -> int:
            return x


        def b(y: int) -> int:
            return y
        """
    )
    first = build_stub_source(spec_source, generated_source, {"a", "b"}, _header())
    second = build_stub_source(spec_source, generated_source, {"a", "b"}, _header())
    assert first == second
    # Source order preserved: a before b.
    assert first.index("def a(") < first.index("def b(")


def test_stub_begins_with_provenance_header() -> None:
    header = _header()
    stub = build_stub_source("x: int = 1\n", "", set(), header)
    assert stub.startswith(header)
    assert parse_stub_header(stub) is not None


def test_is_jaunt_stub_header_sniff(tmp_path: Path) -> None:
    jaunt_stub = tmp_path / "timing.pyi"
    jaunt_stub.write_text(build_stub_source("x: int = 1\n", "", set(), _header()), encoding="utf-8")
    assert is_jaunt_stub(jaunt_stub) is True

    hand = tmp_path / "hand.pyi"
    hand.write_text("def f() -> int: ...\n", encoding="utf-8")
    assert is_jaunt_stub(hand) is False

    assert is_jaunt_stub(tmp_path / "missing.pyi") is False


def test_stub_path_is_pyi_sibling(tmp_path: Path) -> None:
    src = tmp_path / "pkg" / "timing.py"
    assert stub_path_for_source(src) == tmp_path / "pkg" / "timing.pyi"


def test_stub_staleness_missing_and_stale_and_fresh(tmp_path: Path) -> None:
    source_file = tmp_path / "timing.py"
    source_file.write_text("import jaunt\n", encoding="utf-8")
    generated = "def greet() -> str:\n    return 'hi'\n"

    # No stub yet -> missing.
    assert stub_staleness(source_file=source_file, generated_source=generated) == "missing"

    # Write a fresh, matching jaunt stub.
    header = format_stub_header(
        tool_version="0",
        source_module="timing",
        generated_digest=generated_content_digest(generated),
        inputs_digest=stub_inputs_digest("import jaunt\n", generated),
    )
    stub_path = stub_path_for_source(source_file)
    stub_path.write_text(build_stub_source("import jaunt\n", generated, set(), header), "utf-8")
    assert stub_staleness(source_file=source_file, generated_source=generated) is None

    # Generated content changes -> recorded inputs digest no longer matches -> stale.
    changed = "def greet() -> str:\n    return 'hello'\n"
    assert stub_staleness(source_file=source_file, generated_source=changed) == "stale"


def test_emit_stubs_config_defaults_true(tmp_path: Path) -> None:
    from jaunt.config import load_config

    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n', encoding="utf-8"
    )
    cfg = load_config(root=tmp_path)
    assert cfg.build.emit_stubs is True


def test_emit_stubs_config_opt_out(tmp_path: Path) -> None:
    from jaunt.config import load_config

    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n\n[build]\nemit_stubs = false\n',
        encoding="utf-8",
    )
    cfg = load_config(root=tmp_path)
    assert cfg.build.emit_stubs is False


def _undefined_load_names(stub_body: str) -> set[str]:
    """Names read in the stub body that nothing in the stub (or builtins) binds."""
    import ast
    import builtins

    tree = ast.parse(stub_body)
    bound: set[str] = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    bound.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return used - bound


def test_generated_only_import_used_in_signature_is_included() -> None:
    """An import that lives only in the generated module but is referenced by an
    emitted signature is pulled into the stub (finding 2, PR #63)."""
    spec_source = textwrap.dedent(
        '''
        import jaunt


        @jaunt.magic()
        def load():
            """Load a frame."""
            ...
        '''
    )
    generated_source = textwrap.dedent(
        """
        import pandas as pd


        def load() -> pd.DataFrame:
            return pd.DataFrame()
        """
    )
    stub = build_stub_source(spec_source, generated_source, {"load"}, _header())
    assert "import pandas as pd" in stub
    assert "def load() -> pd.DataFrame:" in stub
    # No implementation leaks and no dangling reference.
    assert "return pd.DataFrame()" not in stub
    body = stub[len(_header()) :]
    assert _undefined_load_names(body) == set()


def test_unused_generated_imports_are_not_included() -> None:
    """A generated import not referenced by any emitted signature is omitted."""
    spec_source = textwrap.dedent(
        '''
        import jaunt


        @jaunt.magic()
        def load() -> int:
            """Return a count."""
            ...
        '''
    )
    generated_source = textwrap.dedent(
        """
        import os
        import pandas as pd


        def load() -> int:
            return len(os.listdir())
        """
    )
    stub = build_stub_source(spec_source, generated_source, {"load"}, _header())
    assert "import pandas" not in stub
    assert "import os" not in stub  # only in the elided body, not the signature


def test_generated_only_helper_type_is_supported() -> None:
    """A return type defined (not imported) in the generated module is emitted so the
    stub never references an undefined name."""
    spec_source = textwrap.dedent(
        '''
        import jaunt


        @jaunt.magic()
        def make():
            """Make a result."""
            ...
        '''
    )
    generated_source = textwrap.dedent(
        """
        class _Result:
            value: int

        def make() -> _Result:
            return _Result()
        """
    )
    stub = build_stub_source(spec_source, generated_source, {"make"}, _header())
    assert "def make() -> _Result:" in stub
    assert "class _Result:" in stub
    body = stub[len(_header()) :]
    assert _undefined_load_names(body) == set()


def test_stub_staleness_reacts_to_spec_source_change(tmp_path: Path) -> None:
    """Editing the spec module's handwritten source (generated unchanged) still marks
    the stub stale, because stub content derives from the spec too (finding 6, PR #63)."""
    source_file = tmp_path / "timing.py"
    spec_v1 = (
        "import jaunt\n\n\n"
        "def _helper() -> int:\n    return 1\n\n\n"
        "@jaunt.magic()\n"
        "def greet() -> str:\n"
        '    """Greet."""\n'
        "    ...\n"
    )
    source_file.write_text(spec_v1, encoding="utf-8")
    generated = "def greet() -> str:\n    return 'hi'\n"

    header = format_stub_header(
        tool_version="0",
        source_module="timing",
        generated_digest=generated_content_digest(generated),
        inputs_digest=stub_inputs_digest(spec_v1, generated),
    )
    stub_path_for_source(source_file).write_text(
        build_stub_source(spec_v1, generated, {"greet"}, header), encoding="utf-8"
    )
    # Fresh: both spec and generated match what the stub recorded.
    assert stub_staleness(source_file=source_file, generated_source=generated) is None

    # Change only the spec's handwritten helper; the generated source is identical.
    spec_v2 = spec_v1.replace("return 1", "return 2")
    source_file.write_text(spec_v2, encoding="utf-8")
    assert stub_staleness(source_file=source_file, generated_source=generated) == "stale"


def test_stub_staleness_ignores_hand_authored_stub(tmp_path: Path) -> None:
    """A pre-existing non-jaunt .pyi is never our concern (never overwritten/flagged)."""
    source_file = tmp_path / "timing.py"
    source_file.write_text("import jaunt\n", encoding="utf-8")
    stub_path_for_source(source_file).write_text("def greet() -> str: ...\n", encoding="utf-8")
    generated = "def greet() -> str:\n    return 'hi'\n"
    assert stub_staleness(source_file=source_file, generated_source=generated) is None

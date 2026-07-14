"""Tests for `.pyi` stub emission (jaunt 1.3.0, finding 3 part 2)."""

from __future__ import annotations

import textwrap
from pathlib import Path

from jaunt.header import format_stub_header, parse_stub_header
from jaunt.stub_emitter import (
    build_stub_source,
    generated_content_digest,
    is_jaunt_stub,
    normalize_python_source,
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


def test_transitive_generated_reference_is_resolved() -> None:
    """A two-level chain (make() -> _Result whose attribute references _Inner) resolves
    to a fixpoint so no transitive name is left dangling (finding 2, PR #63)."""
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
        class _Inner:
            x: int

        class _Result:
            inner: _Inner

        def make() -> _Result:
            return _Result()
        """
    )
    stub = build_stub_source(spec_source, generated_source, {"make"}, _header())
    assert "def make() -> _Result:" in stub
    assert "class _Result:" in stub
    assert "class _Inner:" in stub
    body = stub[len(_header()) :]
    assert _undefined_load_names(body) == set()


def test_generated_relative_import_is_rewritten_absolute() -> None:
    """A relative import in the generated module used by a signature is emitted in its
    absolute form, since the stub sits at the spec module's location (finding 3)."""
    spec_source = textwrap.dedent(
        '''
        import jaunt


        @jaunt.magic()
        def load():
            """Load a result."""
            ...
        '''
    )
    generated_source = textwrap.dedent(
        """
        from .types import Result


        def load() -> Result:
            return Result()
        """
    )
    stub = build_stub_source(
        spec_source,
        generated_source,
        {"load"},
        _header(),
        generated_module="pkg.__generated__.svc",
    )
    assert "from pkg.__generated__.types import Result" in stub
    assert "from .types" not in stub
    assert "def load() -> Result:" in stub


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


def test_future_imports_never_emitted_in_stub() -> None:
    """`from __future__ import annotations` must not ride into the stub.

    Future imports are meaningless in .pyi files and, worse, land after the
    generated-import prelude — mid-file, where they are a syntax error (ruff
    F404; ty rejects the file). Both harvest paths must filter them: imports
    copied from the spec module and imports pulled from the generated module
    by referenced name. (mem-mcp-b adoption feedback, finding 18.)
    """
    spec_source = textwrap.dedent(
        '''
        from __future__ import annotations

        import jaunt


        @jaunt.magic()
        def load(path: str) -> "Frame":
            """Load a frame from disk."""
            raise RuntimeError("spec stub")
        '''
    )
    generated_source = textwrap.dedent(
        """
        from __future__ import annotations

        from frames.core import Frame


        def load(path: str) -> Frame:
            return Frame()
        """
    )
    stub = build_stub_source(spec_source, generated_source, {"load"}, _header())
    assert "__future__" not in stub
    # The generated-only import needed by the signature still arrives.
    assert "from frames.core import Frame" in stub
    # The stub parses (a mid-file future import would be a SyntaxError).
    import ast

    ast.parse(stub)


def test_future_import_referenced_by_name_is_filtered() -> None:
    """Even a stub signature that references the name `annotations` must not
    drag the future import in through the by-referenced-name harvest path."""
    spec_source = textwrap.dedent(
        '''
        import jaunt


        @jaunt.magic()
        def dump(x: object) -> dict:
            """Dump annotations."""
            raise RuntimeError("spec stub")
        '''
    )
    generated_source = textwrap.dedent(
        """
        from __future__ import annotations


        def dump(x: object) -> dict:
            return dict(annotations={})


        def helper(mapping: annotations) -> None:
            return None
        """
    )
    stub = build_stub_source(spec_source, generated_source, {"dump"}, _header())
    assert "__future__" not in stub
    import ast

    ast.parse(stub)


def test_jaunt_imports_never_emitted_in_stub() -> None:
    """`import jaunt` / `from jaunt import ...` are decorator plumbing: every
    jaunt marker is stripped from stub clones, so copying the import is a
    guaranteed unused-import (F401). (mem-mcp-b feedback, wave 4.)"""
    spec_source = textwrap.dedent(
        '''
        import jaunt
        import os
        from jaunt import magic

        jaunt.magic_module(__name__)


        def load(path: str) -> str:
            """Load."""
            raise NotImplementedError
        '''
    )
    generated_source = "def load(path: str) -> str:\n    return path\n"
    stub = build_stub_source(spec_source, generated_source, {"load"}, _header())
    assert "import jaunt" not in stub
    assert "from jaunt" not in stub
    assert "import os" not in stub


def test_source_only_imports_are_not_emitted_in_stub() -> None:
    """Imports used only by source bodies must not leak into declaration-only stubs."""
    spec_source = textwrap.dedent(
        '''
        import json
        import logging
        from typing import Optional, Sequence

        def load(value: Sequence[str]) -> Optional[str]:
            """Load one value."""
            logging.info("loading")
            json.dumps(value)
            return value[0] if value else None
        '''
    )
    generated_source = textwrap.dedent(
        """
        from typing import Optional, Sequence

        def load(value: Sequence[str]) -> Optional[str]:
            return value[0] if value else None
        """
    )

    stub = build_stub_source(spec_source, generated_source, set(), _header())

    assert "import json" not in stub
    assert "import logging" not in stub
    assert "from typing import Optional, Sequence" in stub


def test_string_annotation_names_resolve_or_any_bind() -> None:
    """Names inside quoted annotations ("X | None") must resolve like plain
    ones: from the generated module's imports (incl. TYPE_CHECKING blocks) or
    the Any fallback — never left undefined (F821). (wave 4 feedback.)"""
    spec_source = textwrap.dedent(
        '''
        import jaunt

        jaunt.magic_module(__name__)


        def chunker(name: str) -> "RecursiveChunker | None":
            """Get a chunker."""
            raise NotImplementedError


        def frame() -> "Frame":
            """Get a frame."""
            raise NotImplementedError
        '''
    )
    generated_source = textwrap.dedent(
        """
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from frames.core import Frame

        try:
            from chonkie import RecursiveChunker
        except ImportError:
            RecursiveChunker = None


        def chunker(name: str) -> "RecursiveChunker | None":
            return None


        def frame() -> "Frame":
            ...
        """
    )
    stub = build_stub_source(spec_source, generated_source, {"chunker", "frame"}, _header())
    # TYPE_CHECKING-guarded import in the generated module resolves for real.
    assert "from frames.core import Frame" in stub
    # try/except-guarded optional dep falls back to a safe Any binding.
    assert "RecursiveChunker = Any" in stub
    import ast as _ast

    _ast.parse(stub)


def test_format_stub_best_effort_formats_when_ruff_available() -> None:
    from jaunt.stub_emitter import format_stub_best_effort

    ugly = "__all__ = ['a',   'b']\n\n\n\n\ndef a() -> int: ...\n"
    formatted = format_stub_best_effort(ugly)
    # jaunt's own dev env has ruff; formatted output uses double quotes and
    # stub-file blank-line rules. In a ruff-less env this degrades to identity.
    import shutil

    if shutil.which("ruff"):
        assert '"a"' in formatted
    else:
        assert formatted == ugly


def test_normalize_python_source_formats_and_applies_unsafe_ruff_fixes() -> None:
    source = textwrap.dedent(
        """
        from typing import Any
        from typing import Any, Optional

        def identity( value: Optional[Any] )->Optional[Any]:
          return value
        """
    )

    normalized, errors = normalize_python_source(source, filename="generated.py")

    assert errors == []
    assert normalized.count("Any") == 3
    assert normalized.count("from typing import") == 1
    assert "def identity(value: Any | None) -> Any | None:" in normalized


def test_normalize_python_source_preserves_sealed_annotation_syntax() -> None:
    source = textwrap.dedent(
        """
        from typing import List, Optional

        class Legacy:
            def convert(self, values: Optional[List[str]]) -> Optional[int]:
                return None
        """
    )

    normalized, errors = normalize_python_source(
        source,
        filename="generated.py",
        preserve_annotation_syntax=True,
    )

    assert errors == []
    assert "values: Optional[List[str]]" in normalized
    assert "-> Optional[int]" in normalized


def test_jaunt_public_names_in_annotations_still_resolve() -> None:
    """Stripping jaunt imports must not orphan a legitimate annotation that
    references a jaunt public name — it resolves from the generated module's
    imports or Any-binds, never F821. (1.4.2 codex review.)"""
    spec_source = textwrap.dedent(
        '''
        import jaunt
        from jaunt import JauntError

        jaunt.magic_module(__name__)


        def failing(path: str) -> JauntError:
            """Return the error a load would raise."""
            raise NotImplementedError
        '''
    )
    generated_source = textwrap.dedent(
        """
        from jaunt import JauntError


        def failing(path: str) -> JauntError:
            return JauntError("nope")
        """
    )
    stub = build_stub_source(spec_source, generated_source, {"failing"}, _header())
    assert "from jaunt import JauntError" in stub
    import ast as _ast

    _ast.parse(stub)


def test_any_fallback_assignments_emitted_after_imports() -> None:
    """`X = Any` optional-dependency fallbacks must follow the FULL import block,
    so ruff E402 never fires on a spec-copied import that trails them. (wave-5
    emitter-hygiene feedback.)"""
    spec_source = textwrap.dedent(
        '''
        import jaunt
        from collections.abc import Mapping

        jaunt.magic_module(__name__)


        def chunker(m: Mapping) -> "RecursiveChunker | None":
            """Get a chunker."""
            raise NotImplementedError
        '''
    )
    generated_source = textwrap.dedent(
        """
        from collections.abc import Mapping

        try:
            from chonkie import RecursiveChunker
        except ImportError:
            RecursiveChunker = None


        def chunker(m: Mapping) -> "RecursiveChunker | None":
            return None
        """
    )
    stub = build_stub_source(spec_source, generated_source, {"chunker"}, _header())
    lines = stub.splitlines()
    import_idxs = [
        i for i, ln in enumerate(lines) if ln.startswith("import ") or ln.startswith("from ")
    ]
    any_idxs = [
        i for i, ln in enumerate(lines) if "= Any" in ln and not ln.startswith("from typing")
    ]
    assert import_idxs and any_idxs
    assert max(import_idxs) < min(any_idxs)
    assert "RecursiveChunker = Any" in stub
    import ast as _ast

    _ast.parse(stub)


def test_async_contextmanager_stub_passes_available_type_checkers(tmp_path: Path) -> None:
    import shutil
    import subprocess

    import pytest

    spec_source = textwrap.dedent(
        '''
        import contextlib
        from collections.abc import AsyncGenerator, AsyncIterator

        import jaunt


        @contextlib.asynccontextmanager
        @jaunt.magic()
        async def get_connection() -> AsyncGenerator[str, None]:
            """Yield one connection."""
            ...


        @contextlib.asynccontextmanager
        @jaunt.magic()
        async def trace_span() -> AsyncIterator[int]:
            """Yield one span identifier."""
            ...
        '''
    )
    generated_source = textwrap.dedent(
        """
        import contextlib
        from collections.abc import AsyncGenerator, AsyncIterator


        @contextlib.asynccontextmanager
        async def get_connection() -> AsyncGenerator[str, None]:
            yield "connection"


        @contextlib.asynccontextmanager
        async def trace_span() -> AsyncIterator[int]:
            yield 1
        """
    )
    stub = build_stub_source(
        spec_source,
        generated_source,
        {"get_connection", "trace_span"},
        _header(),
    )
    stub, errors = normalize_python_source(stub, filename="contexts.pyi")
    assert errors == []
    assert "async def get_connection" not in stub
    assert "async def trace_span" not in stub
    assert stub.count("@contextlib.asynccontextmanager\ndef ") == 2

    stub_path = tmp_path / "contexts.pyi"
    stub_path.write_text(stub, encoding="utf-8")
    commands = (
        ("ty", ["ty", "check", str(stub_path)]),
        ("mypy", ["mypy", str(stub_path)]),
        ("pyright", ["pyright", str(stub_path)]),
    )
    checked = 0
    for executable, argv in commands:
        resolved = shutil.which(executable)
        if resolved is None:
            continue
        checked += 1
        proc = subprocess.run(
            [resolved, *argv[1:]],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"{executable}:\n{proc.stdout}\n{proc.stderr}"
    if checked == 0:
        pytest.skip("ty, mypy, and pyright are unavailable")

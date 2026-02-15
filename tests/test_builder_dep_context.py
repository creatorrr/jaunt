"""Tests that builder passes dependency context to the LLM backend."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef


class _CapturingBackend(GeneratorBackend):
    """Backend that records the ModuleSpecContext it receives."""

    def __init__(self) -> None:
        self.contexts: list[ModuleSpecContext] = []

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None]:
        self.contexts.append(ctx)
        names = ctx.expected_names
        lines = [f"def {n}(): pass" for n in names]
        return "\n".join(lines) + "\n", None


def _make_spec_file(tmp_path: Path, module: str, qualname: str) -> str:
    """Create a minimal spec Python file and return its path."""
    code = textwrap.dedent(f"""\
        def {qualname}():
            '''Stub.'''
            pass
    """)
    parts = module.split(".")
    dir_path = tmp_path
    for p in parts[:-1]:
        dir_path = dir_path / p
        dir_path.mkdir(exist_ok=True)
        init = dir_path / "__init__.py"
        if not init.exists():
            init.write_text("")

    file_path = dir_path / f"{parts[-1]}.py"
    file_path.write_text(code)
    return str(file_path)


def test_dependency_apis_populated_for_downstream_modules(tmp_path) -> None:
    """When module B depends on module A, B's context should include A's API signatures."""
    from jaunt.builder import run_build

    # Create spec files.
    file_a = _make_spec_file(tmp_path, "pkg.alpha", "helper")
    file_b = _make_spec_file(tmp_path, "pkg.beta", "main_fn")

    ref_a = SpecRef("pkg.alpha:helper")
    ref_b = SpecRef("pkg.beta:main_fn")

    entry_a = SpecEntry(
        kind="magic",
        spec_ref=ref_a,
        module="pkg.alpha",
        qualname="helper",
        source_file=file_a,
        obj=lambda: None,
        decorator_kwargs={},
    )
    entry_b = SpecEntry(
        kind="magic",
        spec_ref=ref_b,
        module="pkg.beta",
        qualname="main_fn",
        source_file=file_b,
        obj=lambda: None,
        decorator_kwargs={},
    )

    specs = {ref_a: entry_a, ref_b: entry_b}
    module_specs = {
        "pkg.alpha": [entry_a],
        "pkg.beta": [entry_b],
    }
    spec_graph = {ref_a: set(), ref_b: {ref_a}}
    module_dag = {"pkg.alpha": set(), "pkg.beta": {"pkg.alpha"}}

    backend = _CapturingBackend()

    # Create output dir.
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir(exist_ok=True)
    (pkg_dir / "__init__.py").write_text("")

    report = asyncio.run(
        run_build(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.alpha", "pkg.beta"},
            backend=backend,
            jobs=1,
        )
    )

    assert not report.failed
    assert len(backend.contexts) == 2

    # Find the context for pkg.beta (depends on pkg.alpha).
    beta_ctx = next(c for c in backend.contexts if c.spec_module == "pkg.beta")

    # dependency_apis should contain the spec for pkg.alpha:helper.
    assert ref_a in beta_ctx.dependency_apis
    assert "helper" in beta_ctx.dependency_apis[ref_a]

    # dependency_generated_modules should contain generated source for pkg.alpha.
    assert "pkg.alpha" in beta_ctx.dependency_generated_modules

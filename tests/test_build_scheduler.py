from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jaunt.builder import run_build
from jaunt.deps import build_spec_graph
from jaunt.errors import JauntDependencyCycleError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entry(
    *, module: str, qualname: str, source_file: str, deps: list[str] | None = None
) -> SpecEntry:
    kw: dict[str, object] = {}
    if deps is not None:
        kw["deps"] = deps
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs=kw,
    )


class FakeBackend(GeneratorBackend):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.contexts: list[ModuleSpecContext] = []

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None]:
        self.calls.append(ctx.spec_module)
        self.contexts.append(ctx)
        lines: list[str] = []
        for name in ctx.expected_names:
            lines.append(f"def {name}():\n    return {name!r}\n")
        return "\n".join(lines).rstrip() + "\n", None


def test_scheduler_respects_dependency_order_jobs_1(tmp_path: Path) -> None:
    src = tmp_path / "src"

    # Two modules: a depends on nothing; b depends on a.
    a_path = tmp_path / "a.py"
    b_path = tmp_path / "b.py"
    _write(a_path, "def A():\n    return 1\n")
    _write(b_path, "def B():\n    return 2\n")

    a = _entry(module="pkg.a", qualname="A", source_file=str(a_path))
    b = _entry(module="pkg.b", qualname="B", source_file=str(b_path), deps=["pkg.a:A"])

    specs = {a.spec_ref: a, b.spec_ref: b}
    spec_graph = build_spec_graph(specs, infer_default=False)

    module_specs = {"pkg.a": [a], "pkg.b": [b]}
    module_dag = {"pkg.a": set(), "pkg.b": {"pkg.a"}}

    backend = FakeBackend()
    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=set(module_specs.keys()),
            backend=backend,
            jobs=1,
        )
    )

    assert report.failed == {}
    assert report.generated == {"pkg.a", "pkg.b"}
    assert backend.calls == ["pkg.a", "pkg.b"]


def test_non_stale_modules_are_skipped(tmp_path: Path) -> None:
    src = tmp_path / "src"
    a_path = tmp_path / "a.py"
    _write(a_path, "def A():\n    return 1\n")
    a = _entry(module="pkg.a", qualname="A", source_file=str(a_path))

    specs = {a.spec_ref: a}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.a": [a]}
    module_dag = {"pkg.a": set()}

    backend = FakeBackend()
    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=set(),
            backend=backend,
            jobs=1,
        )
    )
    assert report.generated == set()
    assert report.failed == {}
    assert report.skipped == {"pkg.a"}
    assert backend.calls == []


def test_dependency_context_passed_to_backend(tmp_path: Path) -> None:
    """When module b depends on module a, the backend should receive a's spec source
    as dependency_apis and a's generated source as dependency_generated_modules."""
    src = tmp_path / "src"

    a_path = tmp_path / "a.py"
    b_path = tmp_path / "b.py"
    _write(a_path, "def A():\n    return 1\n")
    _write(b_path, "def B():\n    return A()\n")

    a = _entry(module="pkg.a", qualname="A", source_file=str(a_path))
    b = _entry(module="pkg.b", qualname="B", source_file=str(b_path), deps=["pkg.a:A"])

    specs = {a.spec_ref: a, b.spec_ref: b}
    spec_graph = build_spec_graph(specs, infer_default=False)

    module_specs = {"pkg.a": [a], "pkg.b": [b]}
    module_dag = {"pkg.a": set(), "pkg.b": {"pkg.a"}}

    backend = FakeBackend()
    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=set(module_specs.keys()),
            backend=backend,
            jobs=1,
        )
    )

    assert report.failed == {}
    assert len(backend.contexts) == 2

    # The second call should be for pkg.b (depends on pkg.a).
    b_ctx = next(c for c in backend.contexts if c.spec_module == "pkg.b")

    # dependency_apis should contain the spec source for A
    assert a.spec_ref in b_ctx.dependency_apis
    assert "def A():" in b_ctx.dependency_apis[a.spec_ref]

    # dependency_generated_modules should contain the generated source for pkg.a
    assert "pkg.a" in b_ctx.dependency_generated_modules
    assert "def A():" in b_ctx.dependency_generated_modules["pkg.a"]


def test_scheduler_cycle_raises(tmp_path: Path) -> None:
    src = tmp_path / "src"

    a_path = tmp_path / "a.py"
    b_path = tmp_path / "b.py"
    _write(a_path, "def A():\n    return 1\n")
    _write(b_path, "def B():\n    return 2\n")

    a = _entry(module="pkg.a", qualname="A", source_file=str(a_path), deps=["pkg.b:B"])
    b = _entry(module="pkg.b", qualname="B", source_file=str(b_path), deps=["pkg.a:A"])

    specs = {a.spec_ref: a, b.spec_ref: b}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.a": [a], "pkg.b": [b]}
    module_dag = {"pkg.a": {"pkg.b"}, "pkg.b": {"pkg.a"}}

    backend = FakeBackend()
    with pytest.raises(JauntDependencyCycleError):
        asyncio.run(
            run_build(
                package_dir=src,
                generated_dir="__generated__",
                module_specs=module_specs,
                specs=specs,
                spec_graph=spec_graph,
                module_dag=module_dag,
                stale_modules=set(module_specs.keys()),
                backend=backend,
                jobs=1,
            )
        )

"""Tests for builder handling of method specs grouped by class."""

from __future__ import annotations

import asyncio
from pathlib import Path

from jaunt.builder import run_build
from jaunt.deps import build_spec_graph
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entry(
    *,
    module: str,
    qualname: str,
    source_file: str,
    class_name: str | None = None,
    decorator_kwargs: dict[str, object] | None = None,
) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs=decorator_kwargs or {},
        class_name=class_name,
    )


class _CapturingBackend(GeneratorBackend):
    """Backend that records contexts and generates valid output for classes."""

    def __init__(self) -> None:
        self.contexts: list[ModuleSpecContext] = []

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None]:
        self.contexts.append(ctx)
        lines: list[str] = []
        for name in ctx.expected_names:
            # Heuristic: if any spec_source contains "class <name>:", generate a class.
            is_class = any(f"class {name}" in src for src in ctx.spec_sources.values())
            if is_class:
                lines.append(f"class {name}:")
                lines.append("    def stub(self): pass")
            else:
                lines.append(f"def {name}():")
                lines.append(f"    return {name!r}")
        return "\n".join(lines) + "\n", None


def test_method_specs_grouped_by_class_in_expected_names(tmp_path: Path) -> None:
    """Method specs from the same class should produce a class-level expected_name."""
    spec_path = tmp_path / "mod.py"
    _write(
        spec_path,
        "class MyService:\n"
        "    def get_user(self, uid: int) -> dict:\n"
        "        ...\n"
        "\n"
        "    def delete_user(self, uid: int) -> bool:\n"
        "        ...\n",
    )

    e1 = _entry(
        module="pkg.mod",
        qualname="MyService.get_user",
        source_file=str(spec_path),
        class_name="MyService",
    )
    e2 = _entry(
        module="pkg.mod",
        qualname="MyService.delete_user",
        source_file=str(spec_path),
        class_name="MyService",
    )
    specs = {e1.spec_ref: e1, e2.spec_ref: e2}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.mod": [e1, e2]}
    module_dag: dict[str, set[str]] = {"pkg.mod": set()}

    backend = _CapturingBackend()
    report = asyncio.run(
        run_build(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.mod"},
            backend=backend,
            jobs=1,
        )
    )
    assert not report.failed, report.failed
    assert len(backend.contexts) == 1
    ctx = backend.contexts[0]
    # The expected_names should contain the class name, not method qualnames
    assert "MyService" in ctx.expected_names
    assert "MyService.get_user" not in ctx.expected_names
    assert "MyService.delete_user" not in ctx.expected_names


def test_mixed_functions_and_methods_in_same_module(tmp_path: Path) -> None:
    """A module with both top-level functions and method specs should work."""
    spec_path = tmp_path / "mod.py"
    _write(
        spec_path,
        "def helper() -> int:\n"
        "    ...\n"
        "\n"
        "class MyService:\n"
        "    def get_user(self, uid: int) -> dict:\n"
        "        ...\n",
    )

    e_fn = _entry(module="pkg.mod", qualname="helper", source_file=str(spec_path))
    e_method = _entry(
        module="pkg.mod",
        qualname="MyService.get_user",
        source_file=str(spec_path),
        class_name="MyService",
    )
    specs = {e_fn.spec_ref: e_fn, e_method.spec_ref: e_method}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.mod": [e_fn, e_method]}
    module_dag: dict[str, set[str]] = {"pkg.mod": set()}

    backend = _CapturingBackend()
    report = asyncio.run(
        run_build(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.mod"},
            backend=backend,
            jobs=1,
        )
    )
    assert not report.failed, report.failed
    ctx = backend.contexts[0]
    assert "helper" in ctx.expected_names
    assert "MyService" in ctx.expected_names
    assert len(ctx.expected_names) == 2


def test_multiple_classes_with_methods_in_one_module(tmp_path: Path) -> None:
    """Two classes with method specs in the same module should produce both class names."""
    spec_path = tmp_path / "mod.py"
    _write(
        spec_path,
        "class ServiceA:\n"
        "    def do_a(self) -> str:\n"
        "        ...\n"
        "\n"
        "class ServiceB:\n"
        "    def do_b(self) -> str:\n"
        "        ...\n",
    )

    e1 = _entry(
        module="pkg.mod",
        qualname="ServiceA.do_a",
        source_file=str(spec_path),
        class_name="ServiceA",
    )
    e2 = _entry(
        module="pkg.mod",
        qualname="ServiceB.do_b",
        source_file=str(spec_path),
        class_name="ServiceB",
    )
    specs = {e1.spec_ref: e1, e2.spec_ref: e2}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.mod": [e1, e2]}
    module_dag: dict[str, set[str]] = {"pkg.mod": set()}

    backend = _CapturingBackend()
    report = asyncio.run(
        run_build(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.mod"},
            backend=backend,
            jobs=1,
        )
    )
    assert not report.failed, report.failed
    ctx = backend.contexts[0]
    assert "ServiceA" in ctx.expected_names
    assert "ServiceB" in ctx.expected_names


def test_class_source_included_in_spec_sources(tmp_path: Path) -> None:
    """spec_sources for method entries should include the full class source."""
    spec_path = tmp_path / "mod.py"
    _write(
        spec_path,
        "class MyService:\n"
        "    x: int = 0\n"
        "\n"
        "    def get_user(self, uid: int) -> dict:\n"
        "        ...\n"
        "\n"
        "    def helper(self) -> None:\n"
        "        pass\n",
    )

    e = _entry(
        module="pkg.mod",
        qualname="MyService.get_user",
        source_file=str(spec_path),
        class_name="MyService",
    )
    specs = {e.spec_ref: e}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.mod": [e]}
    module_dag: dict[str, set[str]] = {"pkg.mod": set()}

    backend = _CapturingBackend()
    report = asyncio.run(
        run_build(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.mod"},
            backend=backend,
            jobs=1,
        )
    )
    assert not report.failed, report.failed
    ctx = backend.contexts[0]
    sources = list(ctx.spec_sources.values())
    assert any("class MyService:" in s for s in sources)
    assert any("def helper" in s for s in sources)


def test_rejects_class_and_method_magic_on_same_class(tmp_path: Path) -> None:
    """Cannot combine whole-class @magic with individual method @magic."""
    spec_path = tmp_path / "mod.py"
    _write(
        spec_path,
        "class MyService:\n    def get_user(self, uid: int) -> dict:\n        ...\n",
    )

    # Whole-class spec (class_name=None, qualname="MyService")
    e_class = _entry(
        module="pkg.mod",
        qualname="MyService",
        source_file=str(spec_path),
        class_name=None,
    )
    # Method spec (class_name="MyService")
    e_method = _entry(
        module="pkg.mod",
        qualname="MyService.get_user",
        source_file=str(spec_path),
        class_name="MyService",
    )
    specs = {e_class.spec_ref: e_class, e_method.spec_ref: e_method}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.mod": [e_class, e_method]}
    module_dag: dict[str, set[str]] = {"pkg.mod": set()}

    backend = _CapturingBackend()
    report = asyncio.run(
        run_build(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.mod"},
            backend=backend,
            jobs=1,
        )
    )
    # Should fail with an error about conflicting class/method magic
    assert "pkg.mod" in report.failed
    errs = report.failed["pkg.mod"]
    assert any("conflict" in e.lower() or "both" in e.lower() for e in errs)

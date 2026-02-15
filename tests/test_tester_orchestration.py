from __future__ import annotations

import asyncio
from pathlib import Path

from jaunt.deps import build_spec_graph
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref
from jaunt.tester import run_pytest, run_test_generation


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entry(*, module: str, qualname: str, source_file: str) -> SpecEntry:
    return SpecEntry(
        kind="test",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs={},
    )


class FakeBackend(GeneratorBackend):
    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None]:
        # Generate a minimal pytest module that defines all expected test functions.
        lines: list[str] = []
        for name in ctx.expected_names:
            lines.append(f"def {name}():\n    assert True\n")
        return "\n".join(lines).rstrip() + "\n", None


def test_tester_generates_into_tests_tree_and_runs_pytest(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "tests").mkdir(parents=True, exist_ok=True)

    # A "test spec" module under tests/, but not named test_*.py to avoid accidental collection.
    spec_path = project / "tests" / "specs_mod.py"
    _write(
        spec_path,
        """
def test_generated():
    # stub; only used for digest/source extraction
    raise AssertionError("should not run")
""".lstrip(),
    )

    e = _entry(module="tests.specs_mod", qualname="test_generated", source_file=str(spec_path))
    specs = {e.spec_ref: e}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"tests.specs_mod": [e]}
    module_dag = {"tests.specs_mod": set()}

    backend = FakeBackend()
    report = asyncio.run(
        run_test_generation(
            project_dir=project,
            tests_package="tests",
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"tests.specs_mod"},
            backend=backend,
            jobs=1,
        )
    )

    assert report.failed == {}
    assert report.generated == {"tests.specs_mod"}
    assert report.generated_files

    gen_file = report.generated_files[0]
    assert str(gen_file).startswith(str(project / "tests" / "__generated__"))
    assert gen_file.exists()

    # Safety: no writes under src/__generated__.
    assert not (project / "src" / "__generated__").exists()

    code = gen_file.read_text(encoding="utf-8")
    assert "def test_generated():" in code

    # Run pytest only on the generated file (should pass).
    assert run_pytest([gen_file], pytest_args=["-q"]) == 0


def test_run_test_generation_threads_dependency_apis_into_backend_ctx(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "tests").mkdir(parents=True, exist_ok=True)

    spec_path = project / "tests" / "specs_mod.py"
    _write(
        spec_path,
        """
def test_generated():
    # stub; only used for digest/source extraction
    raise AssertionError("should not run")
""".lstrip(),
    )

    e = _entry(module="tests.specs_mod", qualname="test_generated", source_file=str(spec_path))
    specs = {e.spec_ref: e}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"tests.specs_mod": [e]}
    module_dag = {"tests.specs_mod": set()}

    sentinel = {normalize_spec_ref("api_mod:foo"): "def foo() -> int: ...\n"}

    class AssertingBackend(GeneratorBackend):
        async def generate_module(
            self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
        ) -> tuple[str, None]:
            assert ctx.dependency_apis == sentinel
            return "def test_generated():\n    assert True\n", None

    backend = AssertingBackend()
    report = asyncio.run(
        run_test_generation(
            project_dir=project,
            tests_package="tests",
            generated_dir="__generated__",
            dependency_apis=sentinel,
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"tests.specs_mod"},
            backend=backend,
            jobs=1,
        )
    )

    assert report.failed == {}

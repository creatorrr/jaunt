from __future__ import annotations

import asyncio
from pathlib import Path

from jaunt.deps import build_spec_graph
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref
from jaunt.tester import run_tests


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


def _project_with_test_spec(tmp_path: Path) -> tuple[Path, dict, dict, dict, dict, set[str]]:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "tests").mkdir(parents=True, exist_ok=True)

    spec_path = project / "tests" / "specs_mod.py"
    _write(
        spec_path,
        """
def test_derived_01():
    raise AssertionError("should not run")
""".lstrip(),
    )

    entry = _entry(
        module="tests.specs_mod",
        qualname="test_derived_01",
        source_file=str(spec_path),
    )
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"tests.specs_mod": [entry]}
    module_dag = {"tests.specs_mod": set()}
    stale_modules = {"tests.specs_mod"}
    return project, module_specs, specs, spec_graph, module_dag, stale_modules


class RepairingHeldoutBackend(GeneratorBackend):
    def __init__(self) -> None:
        self.extra_contexts: list[list[str] | None] = []

    async def generate_module(
        self,
        ctx: ModuleSpecContext,
        *,
        extra_error_context: list[str] | None = None,
    ) -> tuple[str, None]:
        del ctx
        self.extra_contexts.append(extra_error_context)
        if extra_error_context is not None:
            return (
                """
import pytest


@pytest.mark.jaunt_tier("derived")
def test_derived_01():
    assert True
""".lstrip(),
                None,
            )
        return (
            """
import pytest


@pytest.mark.jaunt_tier("derived")
def test_derived_01():
    assert 41 == 42
""".lstrip(),
            None,
        )


def test_run_tests_repairs_derived_failure_with_redacted_feedback(tmp_path: Path) -> None:
    project, module_specs, specs, spec_graph, module_dag, stale_modules = _project_with_test_spec(
        tmp_path
    )
    backend = RepairingHeldoutBackend()

    result = asyncio.run(
        run_tests(
            project_dir=project,
            tests_package="tests",
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=stale_modules,
            backend=backend,
            jobs=1,
            pythonpath=[project / "tests"],
            cwd=project,
        )
    )

    assert result.exit_code == 0
    assert backend.extra_contexts[0] is None
    repair_context = backend.extra_contexts[1]
    assert repair_context is not None
    assert any("derived#" in line for line in repair_context)
    assert any("AssertionError" in line for line in repair_context)
    for leaked in ("41", "42", "== 42", "assert 41"):
        assert all(leaked not in line for line in repair_context)


def test_run_tests_no_redact_derived_escape_hatch_threads_full_feedback(
    tmp_path: Path,
) -> None:
    project, module_specs, specs, spec_graph, module_dag, stale_modules = _project_with_test_spec(
        tmp_path
    )
    backend = RepairingHeldoutBackend()

    result = asyncio.run(
        run_tests(
            project_dir=project,
            tests_package="tests",
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=stale_modules,
            backend=backend,
            jobs=1,
            pythonpath=[project / "tests"],
            cwd=project,
            no_redact_derived=True,
        )
    )

    assert result.exit_code == 0
    repair_context = backend.extra_contexts[1]
    assert repair_context is not None
    assert any("assert 41 == 42" in line for line in repair_context)

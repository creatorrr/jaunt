from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from jaunt.builder import (
    BuildReport,
    _build_expected_names,
    _generated_relpath,
    _whole_class_context,
    build_module_context_artifacts,
    detect_stale_modules,
    run_build,
)
from jaunt.deps import build_spec_graph, collapse_to_module_dag
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext, TokenUsage
from jaunt.header import extract_base_api_digest
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


class CrossA:
    """Runtime stand-in for pkg.base:A."""

    def run(self) -> None: ...


class CrossB(CrossA):
    """Runtime stand-in for pkg.child:B."""

    def go(self) -> None: ...


class SameA:
    """Runtime stand-in for pkg.mod:A."""

    def run(self) -> None: ...


class SameB(SameA):
    """Runtime stand-in for pkg.mod:B."""

    def compute(self, x: int) -> int: ...

    def go(self) -> None: ...


BASE_SOURCE = (
    "class A:\n"
    '    """Base A."""\n'
    "    def run(self) -> None:\n"
    '        """does the run"""\n'
    "        return None\n"
)

CHILD_SOURCE = 'class B(A):\n    """Child B."""\n    def go(self) -> None:\n        return None\n'


class _RecordingByModule(GeneratorBackend):
    def __init__(self, sources: dict[str, str]) -> None:
        self._sources = sources
        self.order: list[str] = []
        self.by_module: dict[str, ModuleSpecContext] = {}

    @property
    def model_name(self) -> str:
        return "rec"

    @property
    def provider_name(self) -> str:
        return "rec"

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, TokenUsage | None]:
        self.order.append(ctx.spec_module)
        self.by_module[ctx.spec_module] = ctx
        return self._sources[ctx.spec_module], None


def _write_generated_module_source(tmp_path: Path, module: str, source: str) -> Path:
    from jaunt import paths

    gen_mod = paths.spec_module_to_generated_module(module, generated_dir="__generated__")
    relpath = paths.generated_module_to_relpath(gen_mod, generated_dir="__generated__")
    out = tmp_path / relpath
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(source, encoding="utf-8")
    return out


def _write_spec_file(tmp_path: Path, module: str, source: str) -> Path:
    module_parts = module.split(".")
    package_dir = tmp_path / "src" / Path(*module_parts[:-1])
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    spec_file = package_dir / f"{module_parts[-1]}.py"
    spec_file.write_text(source, encoding="utf-8")
    return spec_file


def _class_entry(
    *,
    module: str,
    qualname: str,
    obj: type,
    source_file: Path,
    base_deps: tuple = (),
    sealed_members: tuple[str, ...] = (),
) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=str(source_file),
        obj=obj,
        decorator_kwargs={},
        class_name=None,
        base_deps=tuple(base_deps),
        sealed_members=sealed_members,
    )


@dataclass(frozen=True)
class _CrossModuleBuild:
    report: BuildReport
    specs: dict
    spec_graph: dict
    module_dag: dict[str, set[str]]
    module_specs: dict[str, list[SpecEntry]]
    child_entry: SpecEntry
    backend: _RecordingByModule


def _run_cross_module_build(tmp_path: Path) -> _CrossModuleBuild:
    base_spec = _write_spec_file(
        tmp_path,
        "pkg.base",
        "import jaunt\n\n"
        "@jaunt.magic()\n"
        "class A:\n"
        '    """Base A."""\n'
        "    def run(self) -> None: ...\n",
    )
    child_spec = _write_spec_file(
        tmp_path,
        "pkg.child",
        "import jaunt\n\n"
        "@jaunt.magic()\n"
        "class B(A):\n"
        '    """Child B."""\n'
        "    def go(self) -> None: ...\n",
    )
    a_entry = _class_entry(
        module="pkg.base",
        qualname="A",
        obj=CrossA,
        source_file=base_spec,
    )
    child_entry = _class_entry(
        module="pkg.child",
        qualname="B",
        obj=CrossB,
        source_file=child_spec,
        base_deps=(normalize_spec_ref("pkg.base:A"),),
    )
    specs = {a_entry.spec_ref: a_entry, child_entry.spec_ref: child_entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_dag = collapse_to_module_dag(spec_graph)
    module_specs = {"pkg.base": [a_entry], "pkg.child": [child_entry]}
    backend = _RecordingByModule({"pkg.base": BASE_SOURCE, "pkg.child": CHILD_SOURCE})

    report = asyncio.run(
        run_build(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.base", "pkg.child"},
            backend=backend,
            jobs=1,
        )
    )
    return _CrossModuleBuild(
        report=report,
        specs=specs,
        spec_graph=spec_graph,
        module_dag=module_dag,
        module_specs=module_specs,
        child_entry=child_entry,
        backend=backend,
    )


def _child_stale(build: _CrossModuleBuild, tmp_path: Path) -> set[str]:
    child_entry = build.child_entry
    wcc = _whole_class_context(
        [child_entry],
        specs=build.specs,
        package_dir=tmp_path,
        generated_dir="__generated__",
    )
    expected, _ = _build_expected_names([child_entry])
    ctx_d = build_module_context_artifacts(
        module_name="pkg.child",
        entries=[child_entry],
        expected_names=expected,
        module_specs=build.module_specs,
        module_dag=build.module_dag,
        package_dir=tmp_path,
        generated_dir="__generated__",
        base_contract_block=wcc.base_contract_block,
        whole_class_contract_block=wcc.whole_class_contract_block,
        inherited_api_block=wcc.inherited_api_block,
    ).digest
    return detect_stale_modules(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_specs={"pkg.child": [child_entry]},
        specs=build.specs,
        spec_graph=build.spec_graph,
        generation_fingerprint="",
        module_context_digests={"pkg.child": ctx_d},
        module_base_api_digests={"pkg.child": wcc.base_api_digest},
    )


def test_cross_module_base_builds_first_and_child_sees_inherited_api(tmp_path: Path) -> None:
    build = _run_cross_module_build(tmp_path)

    assert not build.report.failed, build.report.failed
    assert {"pkg.base", "pkg.child"} <= build.report.generated
    assert build.backend.order.index("pkg.base") < build.backend.order.index("pkg.child")

    child_ctx = build.backend.by_module["pkg.child"]
    assert "Inherited generated API" in child_ctx.whole_class_contract_block
    assert "A.run(" in child_ctx.whole_class_contract_block

    for module in ("pkg.base", "pkg.child"):
        assert (tmp_path / _generated_relpath(module, generated_dir="__generated__")).exists()

    child_path = tmp_path / _generated_relpath("pkg.child", generated_dir="__generated__")
    assert extract_base_api_digest(child_path.read_text(encoding="utf-8")) not in (None, "")


def test_same_module_base_cogenerated_without_conflict(tmp_path: Path) -> None:
    spec_file = _write_spec_file(
        tmp_path,
        "pkg.mod",
        "import jaunt\n\n"
        "@jaunt.magic()\n"
        "class A:\n"
        '    """Base A."""\n'
        "    def run(self) -> None: ...\n"
        "\n\n"
        "@jaunt.magic()\n"
        "class B(A):\n"
        '    """Child B."""\n'
        "\n"
        "    @jaunt.magic\n"
        "    def compute(self, x: int) -> int: ...\n"
        "\n"
        "    def go(self) -> None: ...\n",
    )
    a_entry = _class_entry(module="pkg.mod", qualname="A", obj=SameA, source_file=spec_file)
    b_entry = _class_entry(
        module="pkg.mod",
        qualname="B",
        obj=SameB,
        source_file=spec_file,
        base_deps=(normalize_spec_ref("pkg.mod:A"),),
        sealed_members=("compute",),
    )
    specs = {a_entry.spec_ref: a_entry, b_entry.spec_ref: b_entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_dag = collapse_to_module_dag(spec_graph)
    assert module_dag == {"pkg.mod": set()}
    module_specs = {"pkg.mod": [a_entry, b_entry]}
    backend = _RecordingByModule(
        {
            "pkg.mod": (
                "class A:\n"
                '    """Base A."""\n'
                "    def run(self) -> None:\n"
                "        return None\n"
                "\n\n"
                "class B(A):\n"
                '    """Child B."""\n'
                "    def compute(self, x: int) -> int:\n"
                "        return x + 1\n"
                "    def go(self) -> None:\n"
                "        return None\n"
            )
        }
    )

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
    assert not any("Conflicting @magic" in err for errs in report.failed.values() for err in errs)
    assert "pkg.mod" in report.generated

    text = (tmp_path / _generated_relpath("pkg.mod", generated_dir="__generated__")).read_text(
        encoding="utf-8"
    )
    assert "class A" in text
    assert "class B(A)" in text


def test_child_restaled_by_base_api_change_not_body_change(tmp_path: Path) -> None:
    build = _run_cross_module_build(tmp_path)
    assert not build.report.failed, build.report.failed
    assert {"pkg.base", "pkg.child"} <= build.report.generated

    child_path = tmp_path / _generated_relpath("pkg.child", generated_dir="__generated__")
    assert extract_base_api_digest(child_path.read_text(encoding="utf-8")) not in (None, "")

    _write_generated_module_source(
        tmp_path,
        "pkg.base",
        "class A:\n"
        '    """Base A."""\n'
        "    def run(self) -> None:\n"
        '        """does the run"""\n'
        "        return None\n"
        "    def stop(self) -> None:\n"
        '        """stop it"""\n'
        "        return None\n",
    )
    assert "pkg.child" in _child_stale(build, tmp_path)

    _write_generated_module_source(
        tmp_path,
        "pkg.base",
        "class A:\n"
        '    """Base A."""\n'
        "    def run(self) -> None:\n"
        '        """does the run"""\n'
        "        x = 1\n"
        "        return None\n",
    )
    assert "pkg.child" not in _child_stale(build, tmp_path)

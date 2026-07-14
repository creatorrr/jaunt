from __future__ import annotations

import asyncio
import io
import subprocess
from pathlib import Path

import pytest

from jaunt import paths
from jaunt.builder import expand_stale_modules, run_build
from jaunt.cost import CostTracker
from jaunt.deps import build_spec_graph
from jaunt.errors import JauntDependencyCycleError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext, TokenUsage
from jaunt.progress import ProgressBar
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


class SourceBackend(GeneratorBackend):
    def __init__(self, source: str) -> None:
        self.source = source
        self.calls: list[str] = []

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None]:
        self.calls.append(ctx.spec_module)
        return self.source, None


def test_expand_stale_modules_respects_allowed_modules() -> None:
    module_dag = {
        "pkg.dep": set(),
        "pkg.mid": {"pkg.dep"},
        "pkg.target": {"pkg.mid"},
        "pkg.other": {"pkg.dep"},
        "pkg.already_stale": set(),
    }

    expanded = expand_stale_modules(
        module_dag,
        {"pkg.dep", "pkg.already_stale"},
        changed_modules={"pkg.dep"},
        allowed_modules={"pkg.dep", "pkg.mid", "pkg.target"},
    )

    assert expanded == {"pkg.dep", "pkg.mid", "pkg.target", "pkg.already_stale"}


def test_expand_stale_modules_without_allowed_modules_keeps_existing_behavior() -> None:
    module_dag = {
        "pkg.dep": set(),
        "pkg.mid": {"pkg.dep"},
        "pkg.target": {"pkg.mid"},
        "pkg.other": {"pkg.dep"},
    }

    expanded = expand_stale_modules(
        module_dag,
        {"pkg.dep"},
        changed_modules={"pkg.dep"},
        allowed_modules=None,
    )

    assert expanded == {"pkg.dep", "pkg.mid", "pkg.target", "pkg.other"}


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


def test_dependents_rebuild_only_when_changed_module_api_changes(tmp_path: Path) -> None:
    src = tmp_path / "src"

    a_path = tmp_path / "a.py"
    b_path = tmp_path / "b.py"
    _write(a_path, "def A() -> int:\n    return 1\n")
    _write(b_path, "def B() -> int:\n    return A()\n")

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
            stale_modules={"pkg.a"},
            changed_modules=set(),
            backend=backend,
            jobs=2,
        )
    )

    assert report.failed == {}
    assert report.generated == {"pkg.a"}
    assert backend.calls == ["pkg.a"]


def test_run_build_allowed_modules_skips_out_of_closure_dependents(tmp_path: Path) -> None:
    src = tmp_path / "src"

    dep_path = tmp_path / "dep.py"
    target_path = tmp_path / "target.py"
    other_path = tmp_path / "other.py"
    _write(dep_path, "def Dep() -> str:\n    return 'dep'\n")
    _write(target_path, "def Target() -> str:\n    return Dep()\n")
    _write(other_path, "def Other() -> str:\n    return Dep()\n")

    dep = _entry(module="pkg.dep", qualname="Dep", source_file=str(dep_path))
    target = _entry(
        module="pkg.target",
        qualname="Target",
        source_file=str(target_path),
        deps=["pkg.dep:Dep"],
    )
    other = _entry(
        module="pkg.other",
        qualname="Other",
        source_file=str(other_path),
        deps=["pkg.dep:Dep"],
    )

    specs = {dep.spec_ref: dep, target.spec_ref: target, other.spec_ref: other}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.dep": [dep], "pkg.target": [target], "pkg.other": [other]}
    module_dag = {"pkg.dep": set(), "pkg.target": {"pkg.dep"}, "pkg.other": {"pkg.dep"}}

    backend = FakeBackend()
    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.dep"},
            changed_modules={"pkg.dep"},
            allowed_modules={"pkg.dep", "pkg.target"},
            backend=backend,
            jobs=2,
        )
    )

    assert report.failed == {}
    assert report.generated == {"pkg.dep", "pkg.target"}
    assert report.skipped == {"pkg.other"}
    assert "pkg.other" not in backend.calls


def test_run_build_rejects_undeclared_generated_import(tmp_path: Path) -> None:
    src = tmp_path / "src"
    spec_path = tmp_path / "specs.py"
    _write(spec_path, "def Play():\n    return 1\n")
    entry = _entry(module="pkg.specs", qualname="Play", source_file=str(spec_path))
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.specs": [entry]}
    module_dag = {"pkg.specs": set()}

    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.specs"},
            backend=SourceBackend(
                "import hallucinated_pkg\n\ndef Play():\n    return hallucinated_pkg.VALUE\n"
            ),
            jobs=1,
        )
    )

    assert report.generated == set()
    assert "pkg.specs" in report.failed
    joined = "\n".join(report.failed["pkg.specs"])
    assert "hallucinated_pkg" in joined
    assert "pkg.__generated__.specs" in joined


def test_run_build_accepts_first_party_import_from_second_source_root(tmp_path: Path) -> None:
    src = tmp_path / "src"
    helpers = tmp_path / "helpers.py"
    spec_path = tmp_path / "specs.py"
    _write(helpers, "def helper() -> int:\n    return 1\n")
    _write(spec_path, "def Play():\n    return 1\n")
    entry = _entry(module="pkg.specs", qualname="Play", source_file=str(spec_path))
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.specs": [entry]}
    module_dag = {"pkg.specs": set()}

    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.specs"},
            backend=SourceBackend("import helpers\n\ndef Play():\n    return helpers.helper()\n"),
            source_roots=[src, tmp_path],
            jobs=1,
        )
    )

    assert report.failed == {}
    assert report.generated == {"pkg.specs"}


def test_needs_dep_marker_surfaces_as_build_warning(tmp_path: Path) -> None:
    src = tmp_path / "src"
    spec_path = tmp_path / "specs.py"
    _write(spec_path, "def Play():\n    return 1\n")
    entry = _entry(module="pkg.specs", qualname="Play", source_file=str(spec_path))
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.specs": [entry]}
    module_dag = {"pkg.specs": set()}

    source = (
        "def Play():\n"
        "    # JAUNT-NEEDS-DEP: util.hashing:stable_hash — inlined a copy\n"
        "    return 1\n"
    )
    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.specs"},
            backend=SourceBackend(source),
            jobs=1,
        )
    )

    assert report.generated == {"pkg.specs"}
    assert "pkg.specs" in report.needs_deps
    markers = report.needs_deps["pkg.specs"]
    assert any("util.hashing:stable_hash" in m for m in markers)


def test_no_needs_dep_marker_leaves_needs_deps_empty(tmp_path: Path) -> None:
    src = tmp_path / "src"
    spec_path = tmp_path / "specs.py"
    _write(spec_path, "def Play():\n    return 1\n")
    entry = _entry(module="pkg.specs", qualname="Play", source_file=str(spec_path))
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.specs": [entry]}
    module_dag = {"pkg.specs": set()}

    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.specs"},
            backend=SourceBackend("def Play():\n    return 1\n"),
            jobs=1,
        )
    )

    assert report.generated == {"pkg.specs"}
    assert report.needs_deps == {}


_CONTEXT_BLOCKS = (
    "preamble",
    "system",
    "module_contract",
    "deps",
    "package_context",
    "repo_map",
    "blueprint",
    "skills_workspace",
)


def test_context_stats_populated_for_built_module(tmp_path: Path) -> None:
    src = tmp_path / "src"
    spec_path = tmp_path / "specs.py"
    _write(spec_path, "def Play():\n    return 1\n")
    entry = _entry(module="pkg.specs", qualname="Play", source_file=str(spec_path))
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.specs": [entry]}
    module_dag = {"pkg.specs": set()}

    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.specs"},
            backend=SourceBackend("def Play():\n    return 1\n"),
            repo_map_block="R" * 40,
            jobs=1,
        )
    )

    assert report.generated == {"pkg.specs"}
    stats = report.context_stats
    assert "pkg.specs" in stats
    blocks = stats["pkg.specs"]
    for name in _CONTEXT_BLOCKS:
        assert name in blocks, name
        assert set(blocks[name]) == {"chars", "est_tokens"}
        assert blocks[name]["chars"] >= 0
        assert blocks[name]["est_tokens"] == blocks[name]["chars"] // 4
    # The injected repo map (40 chars, no project overview) is accounted verbatim.
    assert blocks["repo_map"]["chars"] == 40
    # The preamble is a non-empty static block.
    assert blocks["preamble"]["chars"] > 0


def test_context_stats_only_for_generated_modules(tmp_path: Path) -> None:
    src = tmp_path / "src"
    a_path = tmp_path / "a.py"
    b_path = tmp_path / "b.py"
    _write(a_path, "def A():\n    return 1\n")
    _write(b_path, "def B():\n    return 1\n")
    ea = _entry(module="pkg.a", qualname="A", source_file=str(a_path))
    eb = _entry(module="pkg.b", qualname="B", source_file=str(b_path))
    specs = {ea.spec_ref: ea, eb.spec_ref: eb}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.a": [ea], "pkg.b": [eb]}
    module_dag = {"pkg.a": set(), "pkg.b": set()}

    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.a"},
            backend=SourceBackend("def A():\n    return 1\n"),
            jobs=1,
        )
    )

    assert report.generated == {"pkg.a"}
    assert "pkg.b" in report.skipped
    # Skipped (non-rebuilt) modules get no context accounting.
    assert set(report.context_stats) == {"pkg.a"}


def _single_spec_project(tmp_path: Path):
    src = tmp_path / "src"
    spec_path = tmp_path / "specs.py"
    _write(spec_path, "import jaunt\n\n\n@jaunt.magic()\ndef Play(x: int) -> int:\n    ...\n")
    entry = _entry(module="pkg.specs", qualname="Play", source_file=str(spec_path))
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.specs": [entry]}
    module_dag = {"pkg.specs": set()}
    return src, spec_path, specs, spec_graph, module_specs, module_dag


def test_run_build_emits_pyi_stub(tmp_path: Path) -> None:
    from jaunt.stub_emitter import is_jaunt_stub

    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.specs"},
            backend=SourceBackend("def Play(x: int) -> int:\n    return x * 2\n"),
            jobs=1,
            emit_stubs=True,
        )
    )
    assert report.generated == {"pkg.specs"}
    stub_path = spec_path.with_suffix(".pyi")
    assert stub_path.exists()
    assert is_jaunt_stub(stub_path)
    text = stub_path.read_text(encoding="utf-8")
    assert "def Play(x: int) -> int:" in text
    assert "return x * 2" not in text
    assert report.emitted_stubs.get("pkg.specs") == str(stub_path)


def test_run_build_no_stub_when_emit_stubs_disabled(tmp_path: Path) -> None:
    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.specs"},
            backend=SourceBackend("def Play(x: int) -> int:\n    return x\n"),
            jobs=1,
        )
    )
    assert not spec_path.with_suffix(".pyi").exists()


def test_run_build_never_overwrites_hand_authored_stub(tmp_path: Path) -> None:
    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    stub_path = spec_path.with_suffix(".pyi")
    stub_path.write_text("# hand written\ndef Play(x: int) -> int: ...\n", encoding="utf-8")

    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.specs"},
            backend=SourceBackend("def Play(x: int) -> int:\n    return x\n"),
            jobs=1,
            emit_stubs=True,
        )
    )
    # The hand-authored stub is preserved verbatim.
    assert stub_path.read_text(encoding="utf-8") == "# hand written\ndef Play(x: int) -> int: ...\n"
    assert "pkg.specs" not in report.emitted_stubs
    assert any("pkg.specs" in w for w in report.stub_warnings)


def test_run_build_revalidates_fresh_generated_import_policy(tmp_path: Path) -> None:
    src = tmp_path / "src"
    spec_path = tmp_path / "specs.py"
    _write(spec_path, "def Play():\n    return 1\n")
    entry = _entry(module="pkg.specs", qualname="Play", source_file=str(spec_path))
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.specs": [entry]}
    module_dag = {"pkg.specs": set()}

    first_backend = SourceBackend(
        "import hallucinated_pkg\n\ndef Play():\n    return hallucinated_pkg.VALUE\n"
    )
    first = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules={"pkg.specs"},
            backend=first_backend,
            check_generated_imports=False,
            jobs=1,
        )
    )
    assert first.failed == {}
    assert first.generated == {"pkg.specs"}

    second_backend = SourceBackend("def Play():\n    return 2\n")
    second = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=set(),
            backend=second_backend,
            check_generated_imports=True,
            jobs=1,
        )
    )

    assert second.generated == set()
    assert second.skipped == set()
    assert second_backend.calls == []
    assert "pkg.specs" in second.failed
    assert "hallucinated_pkg" in "\n".join(second.failed["pkg.specs"])


def test_run_build_targeted_skips_out_of_scope_import_validation(tmp_path: Path) -> None:
    src = tmp_path / "src"
    bad_path = tmp_path / "bad.py"
    target_path = tmp_path / "target.py"
    _write(bad_path, "def Bad():\n    return 1\n")
    _write(target_path, "def Target():\n    return 1\n")
    bad = _entry(module="pkg.bad", qualname="Bad", source_file=str(bad_path))
    target = _entry(module="pkg.target", qualname="Target", source_file=str(target_path))

    # First, generate pkg.bad with an undeclared import (gate off) so the file lands on disk.
    asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs={"pkg.bad": [bad]},
            specs={bad.spec_ref: bad},
            spec_graph=build_spec_graph({bad.spec_ref: bad}, infer_default=False),
            module_dag={"pkg.bad": set()},
            stale_modules={"pkg.bad"},
            backend=SourceBackend("import hallucinated_pkg\n\ndef Bad():\n    return 1\n"),
            check_generated_imports=False,
            jobs=1,
        )
    )

    # Targeted build at pkg.target with the gate ON. pkg.bad is out of the requested
    # closure (skipped, not in allowed_modules) and must NOT be import-validated.
    specs = {bad.spec_ref: bad, target.spec_ref: target}
    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs={"pkg.bad": [bad], "pkg.target": [target]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"pkg.bad": set(), "pkg.target": set()},
            stale_modules={"pkg.target"},
            allowed_modules={"pkg.target"},
            backend=SourceBackend("def Target():\n    return 1\n"),
            check_generated_imports=True,
            jobs=1,
        )
    )

    assert report.failed == {}
    assert "pkg.bad" not in report.failed
    assert "pkg.target" in report.generated


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
    assert "signature: def A()" in b_ctx.dependency_apis[a.spec_ref]

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


def test_scheduler_splits_disconnected_specs_within_module(tmp_path: Path) -> None:
    spec_path = tmp_path / "mod.py"
    _write(
        spec_path,
        "def A() -> int:\n    return 1\n\ndef B() -> int:\n    return 2\n",
    )

    a = _entry(module="pkg.mod", qualname="A", source_file=str(spec_path))
    b = _entry(module="pkg.mod", qualname="B", source_file=str(spec_path))
    specs = {a.spec_ref: a, b.spec_ref: b}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.mod": [a, b]}
    module_dag = {"pkg.mod": set()}

    backend = FakeBackend()
    progress_output = io.StringIO()
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
            jobs=2,
            progress=ProgressBar(
                label="build",
                total=1,
                mode="plain",
                stream=progress_output,
            ),
        )
    )

    assert report.failed == {}
    assert backend.calls == ["pkg.mod", "pkg.mod"]
    assert sorted(ctx.expected_names for ctx in backend.contexts) == [["A"], ["B"]]
    assert report.work_items == {
        "pkg.mod": [
            {
                "id": "pkg.mod:component:1",
                "label": "component 1/2 [A]",
                "symbols": ["A"],
                "attempts": 1,
                "cache_hit": False,
            },
            {
                "id": "pkg.mod:component:2",
                "label": "component 2/2 [B]",
                "symbols": ["B"],
                "attempts": 1,
                "cache_hit": False,
            },
        ]
    }
    progress_text = progress_output.getvalue()
    assert "component 1/2 [A]" in progress_text
    assert "component 2/2 [B]" in progress_text

    relpath = paths.generated_module_to_relpath(
        paths.spec_module_to_generated_module("pkg.mod", generated_dir="__generated__"),
        generated_dir="__generated__",
    )
    generated = (tmp_path / relpath).read_text(encoding="utf-8")
    assert "def A():" in generated
    assert "def B():" in generated


def test_scheduler_attributes_retries_and_cost_to_one_module(tmp_path: Path) -> None:
    class RetryBackend(GeneratorBackend):
        def __init__(self) -> None:
            self.calls = 0

        @property
        def model_name(self) -> str:
            return "gpt-5.6-sol"

        @property
        def provider_name(self) -> str:
            return "openai"

        async def generate_module(
            self,
            ctx: ModuleSpecContext,
            *,
            extra_error_context: list[str] | None = None,
        ) -> tuple[str, TokenUsage]:
            del extra_error_context
            self.calls += 1
            name = "Wrong" if self.calls == 1 else ctx.expected_names[0]
            return (
                f"def {name}():\n    return 1\n",
                TokenUsage(10, 5, self.model_name, self.provider_name),
            )

    spec_path = tmp_path / "mod.py"
    _write(spec_path, "def A():\n    return None\n")
    entry = _entry(module="pkg.mod", qualname="A", source_file=str(spec_path))
    specs = {entry.spec_ref: entry}
    backend = RetryBackend()
    tracker = CostTracker()
    progress_output = io.StringIO()

    report = asyncio.run(
        run_build(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"pkg.mod": [entry]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"pkg.mod": set()},
            stale_modules={"pkg.mod"},
            backend=backend,
            jobs=1,
            cost_tracker=tracker,
            progress=ProgressBar(
                label="build",
                total=1,
                mode="plain",
                stream=progress_output,
            ),
        )
    )

    assert report.failed == {}
    assert report.work_items["pkg.mod"][0]["attempts"] == 2
    assert tracker.api_calls == 2
    assert tracker.total_prompt_tokens == 20
    assert tracker.total_completion_tokens == 10
    progress_text = progress_output.getvalue()
    assert "pkg.mod: attempt (1/2)" in progress_text
    assert "pkg.mod: retry (attempt 1)" in progress_text


def test_component_merge_coalesces_overlapping_from_imports() -> None:
    from jaunt.builder import _GeneratedComponent, _merge_generated_components

    merged, errors = _merge_generated_components(
        [
            _GeneratedComponent(
                expected_names=("A",),
                source=(
                    "from typing import Any, Dict\n\n"
                    "def A(value: Any) -> Dict[str, Any]:\n"
                    "    return {'value': value}\n"
                ),
            ),
            _GeneratedComponent(
                expected_names=("B",),
                source=(
                    "from typing import Any, Optional\n\n"
                    "def B(value: Any) -> Optional[Any]:\n"
                    "    return value\n"
                ),
            ),
        ]
    )

    assert errors == []
    assert merged.count("from typing import") == 1
    assert merged.count("Any") == 5
    assert "from typing import Any, Dict, Optional" in merged


def test_attached_test_context_is_neutral_to_equivalent_path_move(tmp_path: Path) -> None:
    from jaunt.builder import _build_attached_test_specs_block

    source = (
        "import jaunt\n\n"
        "@jaunt.test(targets=('pkg.mod:A',))\n"
        "def test_a():\n"
        '    """A returns one."""\n'
        "    ...\n"
    )
    old_path = tmp_path / "owner/tests/spec.py"
    new_path = tmp_path / "owner_unique_tests/spec.py"
    _write(old_path, source)
    _write(new_path, source)
    entries = []
    for module, path in (("tests.spec", old_path), ("owner_unique_tests.spec", new_path)):
        entries.append(
            SpecEntry(
                kind="test",
                spec_ref=normalize_spec_ref(f"{module}:test_a"),
                module=module,
                qualname="test_a",
                source_file=str(path),
                obj=object(),
                decorator_kwargs={"targets": ("pkg.mod:A",)},
            )
        )

    assert _build_attached_test_specs_block([entries[0]]) == _build_attached_test_specs_block(
        [entries[1]]
    )


def test_run_build_normalizes_generated_python_with_ruff(tmp_path: Path) -> None:
    spec_path = tmp_path / "mod.py"
    _write(spec_path, "def A():\n    return None\n")
    entry = _entry(module="pkg.mod", qualname="A", source_file=str(spec_path))
    specs = {entry.spec_ref: entry}
    report = asyncio.run(
        run_build(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"pkg.mod": [entry]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"pkg.mod": set()},
            stale_modules={"pkg.mod"},
            backend=SourceBackend(
                "from typing import Any\n"
                "from typing import Any, Optional\n\n"
                "def A( value: Optional[Any]=None )->Optional[Any]:\n"
                " return value\n"
            ),
            jobs=1,
        )
    )

    assert report.failed == {}
    generated = next(tmp_path.rglob("__generated__/mod.py"))
    source = generated.read_text(encoding="utf-8")
    assert source.count("from typing import") == 1
    assert "def A(value: Any | None = None) -> Any | None:" in source
    subprocess.run(
        ["ruff", "format", "--isolated", "--check", str(generated)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "ruff",
            "check",
            "--isolated",
            "--select",
            "E,F,I,UP,B",
            "--ignore",
            "E501",
            str(generated),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_scheduler_keeps_connected_specs_in_same_module_together(tmp_path: Path) -> None:
    spec_path = tmp_path / "mod.py"
    _write(
        spec_path,
        "def A() -> int:\n    return B()\n\ndef B() -> int:\n    return 2\n",
    )

    a = _entry(module="pkg.mod", qualname="A", source_file=str(spec_path), deps=["pkg.mod:B"])
    b = _entry(module="pkg.mod", qualname="B", source_file=str(spec_path))
    specs = {a.spec_ref: a, b.spec_ref: b}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.mod": [a, b]}
    module_dag = {"pkg.mod": set()}

    backend = FakeBackend()
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
            jobs=2,
        )
    )

    assert report.failed == {}
    assert backend.calls == ["pkg.mod"]
    assert backend.contexts[0].expected_names == ["A", "B"]


def test_ty_error_context_timeout_returns_error(monkeypatch, tmp_path: Path) -> None:
    from jaunt import builder

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["ty"], timeout=1.0, stderr="hung")

    monkeypatch.setattr(builder.subprocess, "run", _timeout)

    errs = builder._ty_error_context(  # noqa: SLF001 - direct helper coverage
        source="def foo() -> int:\n    return 1\n",
        module_name="pkg.mod",
        package_dir=tmp_path,
        generated_dir="__generated__",
        ty_cmd=["ty"],
    )

    assert errs
    assert "timed out" in errs[0]


def test_ty_error_context_mirrors_package_sources(monkeypatch, tmp_path: Path) -> None:
    from jaunt import builder

    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "specs.py", "class Claims:\n    pass\n")
    _write(tmp_path / "pkg" / "__generated__" / "other.py", "OTHER = 1\n")
    _write(tmp_path / "pkg" / "__generated__" / "specs.py", "STALE = True\n")
    _write(tmp_path / "pkg" / "__pycache__" / "junk.py", "")

    seen: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):
        target = Path(cmd[-1])
        sandbox = target.parents[2]
        seen["candidate"] = target.read_text(encoding="utf-8")
        seen["has_specs"] = (sandbox / "pkg" / "specs.py").exists()
        seen["has_init"] = (sandbox / "pkg" / "__init__.py").exists()
        seen["has_sibling"] = (sandbox / "pkg" / "__generated__" / "other.py").exists()
        seen["has_pycache"] = (sandbox / "pkg" / "__pycache__").exists()

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(builder.subprocess, "run", _fake_run)

    errs = builder._ty_error_context(  # noqa: SLF001 - direct helper coverage
        source="from ..specs import Claims\n",
        module_name="pkg.specs",
        package_dir=tmp_path,
        generated_dir="__generated__",
        ty_cmd=["ty"],
    )

    assert errs == []
    assert seen["has_specs"] is True
    assert seen["has_init"] is True
    assert seen["has_sibling"] is True
    assert seen["has_pycache"] is False
    # The candidate slot holds the in-flight source, not the stale on-disk file.
    assert seen["candidate"] == "from ..specs import Claims\n"


def _fake_ty_proc(stdout: str):
    class _Proc:
        returncode = 1
        stderr = ""

        def __init__(self, out: str) -> None:
            self.stdout = out

    return _Proc(stdout)


def test_ty_error_context_orders_real_errors_before_unresolved_import(
    monkeypatch, tmp_path: Path
) -> None:
    from jaunt import builder

    raw = (
        "error[unresolved-import]: Cannot resolve imported module `..specs`\n"
        " --> specs.py:1:8\n"
        "help: Did you mean `.specs`?\n"
        "error[invalid-return-type]: Return type does not match returned value\n"
        " --> specs.py:9:5\n"
    )
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: _fake_ty_proc(raw))

    errs = builder._ty_error_context(  # noqa: SLF001 - direct helper coverage
        source="def foo() -> int:\n    return 1\n",
        module_name="pkg.mod",
        package_dir=tmp_path,
        generated_dir="__generated__",
        ty_cmd=["ty"],
    )

    assert len(errs) == 1
    assert errs[0].index("invalid-return-type") < errs[0].index("unresolved-import")
    assert "Did you mean" in errs[0]


def test_ty_error_context_pure_unresolved_import_is_ignored(monkeypatch, tmp_path: Path) -> None:
    from jaunt import builder

    raw = "error[unresolved-import]: Cannot resolve imported module `..specs`\n --> specs.py:1:8\n"
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: _fake_ty_proc(raw))

    errs = builder._ty_error_context(  # noqa: SLF001 - direct helper coverage
        source="from ..specs import Claims\n",
        module_name="pkg.mod",
        package_dir=tmp_path,
        generated_dir="__generated__",
        ty_cmd=["ty"],
    )

    assert errs == []

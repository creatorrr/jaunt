from __future__ import annotations

import asyncio
import errno
import io
import os
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


def _project_with_stale_managed_stub(tmp_path: Path):
    project = _single_spec_project(tmp_path)
    src, spec_path, specs, spec_graph, module_specs, module_dag = project
    first = asyncio.run(
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
    assert first.failed == {}
    stub_path = spec_path.with_suffix(".pyi")
    stale_bytes = stub_path.read_bytes() + b"\n"
    stub_path.write_bytes(stale_bytes)
    return project, stub_path, stale_bytes


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


def test_run_build_fails_when_emitted_stub_does_not_converge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    monkeypatch.setattr("jaunt.stub_emitter.stub_staleness", lambda **_kwargs: "stale")

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

    assert report.generated == set()
    assert "pkg.specs" in report.failed
    assert "emitted stub did not converge" in report.failed["pkg.specs"][0]
    assert "pkg.specs" not in report.emitted_stubs


def test_run_build_reemits_stub_when_generated_module_changes_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaunt import builder as builder_module
    from jaunt.stub_emitter import stub_staleness

    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    stub_path = spec_path.with_suffix(".pyi")
    generated_path = src / "pkg" / "__generated__" / "specs.py"
    real_link = builder_module.os.link
    replaced_generated = False

    def link_with_concurrent_generated_update(source, destination) -> None:
        nonlocal replaced_generated
        real_link(source, destination)
        if Path(destination) != stub_path or replaced_generated:
            return
        replaced_generated = True
        generated = generated_path.read_text(encoding="utf-8")
        replacement = generated.replace(
            "def Play(x: int) -> int:\n    return x * 2",
            "def Play(x: int) -> str:\n    return str(x)",
        )
        assert replacement != generated
        replacement_path = generated_path.with_name(".specs-race.py")
        replacement_path.write_text(replacement, encoding="utf-8")
        builder_module.os.replace(replacement_path, generated_path)

    monkeypatch.setattr(builder_module.os, "link", link_with_concurrent_generated_update)

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

    assert replaced_generated is True
    assert report.generated == {"pkg.specs"}
    assert report.failed == {}
    assert report.emitted_stubs["pkg.specs"] == str(stub_path)
    assert "def Play(x: int) -> str:" in stub_path.read_text(encoding="utf-8")
    assert (
        stub_staleness(
            source_file=spec_path,
            generated_source=generated_path.read_text(encoding="utf-8"),
        )
        is None
    )


def test_run_build_preserves_hand_authored_stub_created_during_formatting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaunt import stub_emitter

    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    stub_path = spec_path.with_suffix(".pyi")
    hand_authored = b"# created concurrently\ndef Play(x: int) -> int: ...\n"
    real_format = stub_emitter.format_stub_best_effort
    created_stub = False

    def format_after_user_write(stub_source: str, *, filename=None) -> str:
        nonlocal created_stub
        formatted = real_format(stub_source, filename=filename)
        if not created_stub:
            stub_path.write_bytes(hand_authored)
            created_stub = True
        return formatted

    monkeypatch.setattr(stub_emitter, "format_stub_best_effort", format_after_user_write)

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

    assert created_stub is True
    assert stub_path.read_bytes() == hand_authored
    assert report.generated == {"pkg.specs"}
    assert report.failed == {}
    assert "pkg.specs" not in report.emitted_stubs
    assert any(
        "hand-authored specs.pyi not overwritten" in warning for warning in report.stub_warnings
    )


def test_run_build_does_not_clobber_absent_stub_created_at_publish_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaunt import builder as builder_module

    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    stub_path = spec_path.with_suffix(".pyi")
    hand_authored = b"# won create race\ndef Play(x: int) -> int: ...\n"
    real_link = builder_module.os.link
    created_stub = False

    def link_after_external_create(source, destination) -> None:
        nonlocal created_stub
        if Path(destination) == stub_path and not created_stub:
            stub_path.write_bytes(hand_authored)
            created_stub = True
        real_link(source, destination)

    monkeypatch.setattr(builder_module.os, "link", link_after_external_create)

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

    assert created_stub is True
    assert stub_path.read_bytes() == hand_authored
    assert report.generated == {"pkg.specs"}
    assert report.failed == {}
    assert "pkg.specs" not in report.emitted_stubs
    assert any("hand-authored specs.pyi not overwritten" in item for item in report.stub_warnings)


def test_run_build_publishes_new_stub_when_hardlinks_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaunt import builder as builder_module
    from jaunt.stub_emitter import stub_staleness

    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    stub_path = spec_path.with_suffix(".pyi")
    real_link = builder_module.os.link

    def reject_stub_hardlinks(source, destination) -> None:
        if Path(destination) == stub_path:
            raise OSError(errno.EOPNOTSUPP, "simulated unsupported hardlink")
        real_link(source, destination)

    monkeypatch.setattr(builder_module.os, "link", reject_stub_hardlinks)

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
    assert report.failed == {}
    assert report.emitted_stubs == {"pkg.specs": str(stub_path)}
    assert (
        stub_staleness(
            source_file=spec_path,
            generated_source=(src / "pkg" / "__generated__" / "specs.py").read_text(
                encoding="utf-8"
            ),
        )
        is None
    )
    assert list(stub_path.parent.glob(".jaunt-stub-candidate-*")) == []


def test_run_build_hardlink_fallback_preserves_absent_stub_create_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaunt import builder as builder_module

    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    stub_path = spec_path.with_suffix(".pyi")
    hand_authored = b"# won fallback create race\ndef Play(x: int) -> int: ...\n"
    real_link = builder_module.os.link
    real_open = builder_module.os.open
    created_stub = False

    def reject_stub_hardlinks(source, destination) -> None:
        if Path(destination) == stub_path:
            raise OSError(errno.EOPNOTSUPP, "simulated unsupported hardlink")
        real_link(source, destination)

    def create_before_exclusive_open(path, flags, mode=0o777):
        nonlocal created_stub
        if Path(path) == stub_path and flags & os.O_EXCL and not created_stub:
            stub_path.write_bytes(hand_authored)
            created_stub = True
        return real_open(path, flags, mode)

    monkeypatch.setattr(builder_module.os, "link", reject_stub_hardlinks)
    monkeypatch.setattr(builder_module.os, "open", create_before_exclusive_open)

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

    assert created_stub is True
    assert stub_path.read_bytes() == hand_authored
    assert report.generated == {"pkg.specs"}
    assert report.failed == {}
    assert "pkg.specs" not in report.emitted_stubs
    assert any("hand-authored specs.pyi not overwritten" in item for item in report.stub_warnings)
    assert list(stub_path.parent.glob(".jaunt-stub-recovery-*")) == []
    assert list(stub_path.parent.glob(".jaunt-stub-candidate-*")) == []


@pytest.mark.parametrize("fault", ["write", "fsync", "close"])
def test_run_build_hardlink_fallback_removes_failed_public_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from jaunt import builder as builder_module

    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    stub_path = spec_path.with_suffix(".pyi")
    real_link = builder_module.os.link
    real_open = builder_module.os.open
    real_write = builder_module.os.write
    real_fsync = builder_module.os.fsync
    real_close = builder_module.os.close
    public_descriptor: int | None = None
    public_write_calls = 0
    close_faulted = False

    def reject_stub_hardlinks(source, destination) -> None:
        if Path(destination) == stub_path:
            raise OSError(errno.EOPNOTSUPP, "simulated unsupported hardlink")
        real_link(source, destination)

    def track_public_open(path, flags, mode=0o777):
        nonlocal public_descriptor
        descriptor = real_open(path, flags, mode)
        if Path(path) == stub_path and flags & os.O_EXCL:
            public_descriptor = descriptor
        return descriptor

    def fail_public_write(descriptor, data) -> int:
        nonlocal public_write_calls
        if fault != "write" or descriptor != public_descriptor:
            return real_write(descriptor, data)
        public_write_calls += 1
        if public_write_calls == 1:
            return real_write(descriptor, data[: max(1, len(data) // 3)])
        raise OSError(errno.EIO, "simulated public stub write failure")

    def fail_public_fsync(descriptor) -> None:
        if fault == "fsync" and descriptor == public_descriptor:
            raise OSError(errno.EIO, "simulated public stub fsync failure")
        real_fsync(descriptor)

    def fail_public_close(descriptor) -> None:
        nonlocal close_faulted
        if fault == "close" and descriptor == public_descriptor and not close_faulted:
            close_faulted = True
            real_close(descriptor)
            raise OSError(errno.EIO, "simulated public stub close failure")
        real_close(descriptor)

    monkeypatch.setattr(builder_module.os, "link", reject_stub_hardlinks)
    monkeypatch.setattr(builder_module.os, "open", track_public_open)
    monkeypatch.setattr(builder_module.os, "write", fail_public_write)
    monkeypatch.setattr(builder_module.os, "fsync", fail_public_fsync)
    monkeypatch.setattr(builder_module.os, "close", fail_public_close)

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

    assert "pkg.specs" in report.failed
    assert not stub_path.exists()
    assert list(stub_path.parent.glob(".jaunt-stub-quarantine-*")) == []
    assert list(stub_path.parent.glob(".jaunt-stub-recovery-*")) == []
    assert list(stub_path.parent.glob(".jaunt-stub-candidate-*")) == []


def test_run_build_hardlink_fallback_accepts_positive_short_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaunt import builder as builder_module
    from jaunt.stub_emitter import stub_staleness

    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    stub_path = spec_path.with_suffix(".pyi")
    real_link = builder_module.os.link
    real_open = builder_module.os.open
    real_write = builder_module.os.write
    public_descriptor: int | None = None
    shortened = False

    def reject_stub_hardlinks(source, destination) -> None:
        if Path(destination) == stub_path:
            raise OSError(errno.EOPNOTSUPP, "simulated unsupported hardlink")
        real_link(source, destination)

    def track_public_open(path, flags, mode=0o777):
        nonlocal public_descriptor
        descriptor = real_open(path, flags, mode)
        if Path(path) == stub_path and flags & os.O_EXCL:
            public_descriptor = descriptor
        return descriptor

    def short_public_write(descriptor, data) -> int:
        nonlocal shortened
        if descriptor == public_descriptor and not shortened:
            shortened = True
            return real_write(descriptor, data[: max(1, len(data) // 3)])
        return real_write(descriptor, data)

    monkeypatch.setattr(builder_module.os, "link", reject_stub_hardlinks)
    monkeypatch.setattr(builder_module.os, "open", track_public_open)
    monkeypatch.setattr(builder_module.os, "write", short_public_write)

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

    assert shortened is True
    assert report.failed == {}
    assert report.emitted_stubs == {"pkg.specs": str(stub_path)}
    assert (
        stub_staleness(
            source_file=spec_path,
            generated_source=(src / "pkg" / "__generated__" / "specs.py").read_text(
                encoding="utf-8"
            ),
        )
        is None
    )


def test_run_build_hardlink_fallback_restores_managed_stub_after_copy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaunt import builder as builder_module

    project, stub_path, stale_bytes = _project_with_stale_managed_stub(tmp_path)
    src, _spec_path, specs, spec_graph, module_specs, module_dag = project
    real_link = builder_module.os.link
    real_open = builder_module.os.open
    real_write = builder_module.os.write
    public_descriptor: int | None = None
    failed_once = False

    def reject_stub_hardlinks(source, destination) -> None:
        if Path(destination) == stub_path:
            raise OSError(errno.EOPNOTSUPP, "simulated unsupported hardlink")
        real_link(source, destination)

    def track_public_open(path, flags, mode=0o777):
        nonlocal public_descriptor
        descriptor = real_open(path, flags, mode)
        if Path(path) == stub_path and flags & os.O_EXCL:
            public_descriptor = descriptor
        return descriptor

    def fail_first_public_copy(descriptor, data) -> int:
        nonlocal failed_once
        if descriptor == public_descriptor and not failed_once:
            failed_once = True
            real_write(descriptor, data[: max(1, len(data) // 3)])
            raise OSError(errno.EIO, "simulated candidate copy failure")
        return real_write(descriptor, data)

    monkeypatch.setattr(builder_module.os, "link", reject_stub_hardlinks)
    monkeypatch.setattr(builder_module.os, "open", track_public_open)
    monkeypatch.setattr(builder_module.os, "write", fail_first_public_copy)

    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=set(),
            backend=SourceBackend("unused"),
            jobs=1,
            emit_stubs=True,
        )
    )

    assert failed_once is True
    assert "pkg.specs" in report.failed
    assert stub_path.read_bytes() == stale_bytes
    assert list(stub_path.parent.glob(".jaunt-stub-quarantine-*")) == []
    assert list(stub_path.parent.glob(".jaunt-stub-recovery-*")) == []
    assert list(stub_path.parent.glob(".jaunt-stub-candidate-*")) == []


def test_run_build_hardlink_fallback_quarantines_concurrent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaunt import builder as builder_module

    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    stub_path = spec_path.with_suffix(".pyi")
    hand_authored = b"# concurrent replacement\ndef Play(x: int) -> int: ...\n"
    external_path = stub_path.parent / ".external-specs.pyi"
    external_path.write_bytes(hand_authored)
    real_link = builder_module.os.link
    real_open = builder_module.os.open
    real_write = builder_module.os.write
    real_replace = builder_module.os.replace
    public_descriptor: int | None = None
    public_write_calls = 0
    replacement_moved = False

    def reject_stub_hardlinks(source, destination) -> None:
        if Path(destination) == stub_path:
            raise OSError(errno.EOPNOTSUPP, "simulated unsupported hardlink")
        real_link(source, destination)

    def track_public_open(path, flags, mode=0o777):
        nonlocal public_descriptor
        descriptor = real_open(path, flags, mode)
        if Path(path) == stub_path and flags & os.O_EXCL:
            public_descriptor = descriptor
        return descriptor

    def fail_public_write(descriptor, data) -> int:
        nonlocal public_write_calls
        if descriptor != public_descriptor:
            return real_write(descriptor, data)
        public_write_calls += 1
        if public_write_calls == 1:
            return real_write(descriptor, data[: max(1, len(data) // 3)])
        raise OSError(errno.EIO, "simulated public stub write failure")

    def replace_before_quarantine(source, destination) -> None:
        nonlocal replacement_moved
        if (
            Path(source) == stub_path
            and Path(destination).name.startswith(".jaunt-stub-quarantine-")
            and not replacement_moved
        ):
            real_replace(external_path, stub_path)
            replacement_moved = True
        real_replace(source, destination)

    monkeypatch.setattr(builder_module.os, "link", reject_stub_hardlinks)
    monkeypatch.setattr(builder_module.os, "open", track_public_open)
    monkeypatch.setattr(builder_module.os, "write", fail_public_write)
    monkeypatch.setattr(builder_module.os, "replace", replace_before_quarantine)

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

    quarantines = list(stub_path.parent.glob(".jaunt-stub-quarantine-*"))
    assert replacement_moved is True
    assert "pkg.specs" in report.failed
    assert not stub_path.exists()
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == hand_authored
    assert str(quarantines[0]) in report.failed["pkg.specs"][0]
    assert list(stub_path.parent.glob(".jaunt-stub-recovery-*")) == []
    assert list(stub_path.parent.glob(".jaunt-stub-candidate-*")) == []


def test_run_build_recovers_hand_authored_write_at_managed_publish_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaunt import builder as builder_module

    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    stub_path = spec_path.with_suffix(".pyi")
    first = asyncio.run(
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
    assert first.failed == {}
    stub_path.write_bytes(stub_path.read_bytes() + b"\n")

    hand_authored = b"# won replace race\ndef Play(x: int) -> int: ...\n"
    real_replace = builder_module.os.replace
    replaced_stub = False

    def replace_after_external_write(source, destination) -> None:
        nonlocal replaced_stub
        if (
            Path(source) == stub_path
            and Path(destination).name.startswith(".jaunt-stub-recovery-")
            and not replaced_stub
        ):
            stub_path.write_bytes(hand_authored)
            replaced_stub = True
        real_replace(source, destination)

    monkeypatch.setattr(builder_module.os, "replace", replace_after_external_write)

    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=set(),
            backend=SourceBackend("unused"),
            jobs=1,
            emit_stubs=True,
        )
    )

    assert replaced_stub is True
    assert stub_path.read_bytes() == hand_authored
    assert report.generated == set()
    assert report.failed == {}
    assert report.skipped == {"pkg.specs"}
    assert "pkg.specs" not in report.emitted_stubs
    assert any("hand-authored specs.pyi not overwritten" in item for item in report.stub_warnings)
    assert list(stub_path.parent.glob(".jaunt-stub-recovery-*")) == []
    assert list(stub_path.parent.glob(".jaunt-stub-candidate-*")) == []


@pytest.mark.parametrize("link_errno", [errno.EOPNOTSUPP, errno.ENOSYS, errno.EACCES])
def test_run_build_publishes_managed_stub_when_hardlinks_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link_errno: int
) -> None:
    from jaunt import builder as builder_module
    from jaunt.stub_emitter import stub_staleness

    project, stub_path, stale_bytes = _project_with_stale_managed_stub(tmp_path)
    src, spec_path, specs, spec_graph, module_specs, module_dag = project
    real_link = builder_module.os.link

    def fail_stub_links(source, destination) -> None:
        if Path(destination) == stub_path:
            raise OSError(link_errno, "simulated hardlink failure")
        real_link(source, destination)

    monkeypatch.setattr(builder_module.os, "link", fail_stub_links)

    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=set(),
            backend=SourceBackend("unused"),
            jobs=1,
            emit_stubs=True,
        )
    )

    assert report.generated == set()
    assert report.failed == {}
    assert report.emitted_stubs == {"pkg.specs": str(stub_path)}
    assert stub_path.read_bytes() != stale_bytes
    assert (
        stub_staleness(
            source_file=spec_path,
            generated_source=(src / "pkg" / "__generated__" / "specs.py").read_text(
                encoding="utf-8"
            ),
        )
        is None
    )
    assert list(stub_path.parent.glob(".jaunt-stub-recovery-*")) == []
    assert list(stub_path.parent.glob(".jaunt-stub-candidate-*")) == []


def test_run_build_retains_recovery_when_hardlink_and_copy_restore_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaunt import builder as builder_module

    project, stub_path, stale_bytes = _project_with_stale_managed_stub(tmp_path)
    src, _spec_path, specs, spec_graph, module_specs, module_dag = project
    real_link = builder_module.os.link
    real_open = builder_module.os.open

    def fail_stub_links(source, destination) -> None:
        if Path(destination) == stub_path:
            raise OSError(errno.EOPNOTSUPP, "simulated unsupported hardlink")
        real_link(source, destination)

    def fail_exclusive_restore(path, flags, mode=0o777):
        if Path(path) == stub_path and flags & os.O_EXCL:
            raise PermissionError(errno.EACCES, "simulated restore permission failure")
        return real_open(path, flags, mode)

    monkeypatch.setattr(builder_module.os, "link", fail_stub_links)
    monkeypatch.setattr(builder_module.os, "open", fail_exclusive_restore)

    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=set(),
            backend=SourceBackend("unused"),
            jobs=1,
            emit_stubs=True,
        )
    )

    recoveries = list(stub_path.parent.glob(".jaunt-stub-recovery-*"))
    assert "pkg.specs" in report.failed
    assert "prior bytes remain at" in report.failed["pkg.specs"][0]
    assert not stub_path.exists()
    assert len(recoveries) == 1
    assert recoveries[0].read_bytes() == stale_bytes
    assert list(stub_path.parent.glob(".jaunt-stub-candidate-*")) == []


def test_run_build_preserves_hand_authored_race_after_managed_stub_displacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaunt import builder as builder_module

    project, stub_path, stale_bytes = _project_with_stale_managed_stub(tmp_path)
    src, _spec_path, specs, spec_graph, module_specs, module_dag = project
    hand_authored = b"# arrived after displacement\ndef Play(x: int) -> int: ...\n"
    real_link = builder_module.os.link
    raced = False

    def fail_candidate_after_user_write(source, destination) -> None:
        nonlocal raced
        if (
            Path(destination) == stub_path
            and Path(source).name.startswith(".jaunt-stub-candidate-")
            and not raced
        ):
            stub_path.write_bytes(hand_authored)
            raced = True
            raise PermissionError(errno.EACCES, "simulated publication race")
        real_link(source, destination)

    monkeypatch.setattr(builder_module.os, "link", fail_candidate_after_user_write)

    report = asyncio.run(
        run_build(
            package_dir=src,
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=set(),
            backend=SourceBackend("unused"),
            jobs=1,
            emit_stubs=True,
        )
    )

    recoveries = list(stub_path.parent.glob(".jaunt-stub-recovery-*"))
    assert raced is True
    assert report.failed == {}
    assert stub_path.read_bytes() == hand_authored
    assert "pkg.specs" not in report.emitted_stubs
    assert len(recoveries) == 1
    assert recoveries[0].read_bytes() == stale_bytes
    assert any(str(recoveries[0]) in warning for warning in report.stub_warnings)
    assert list(stub_path.parent.glob(".jaunt-stub-candidate-*")) == []


def test_run_build_reemits_stub_deleted_after_freshness_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaunt import stub_emitter

    src, spec_path, specs, spec_graph, module_specs, module_dag = _single_spec_project(tmp_path)
    stub_path = spec_path.with_suffix(".pyi")
    real_staleness = stub_emitter.stub_staleness
    deleted_stub = False

    def delete_after_freshness_check(**kwargs) -> str | None:
        nonlocal deleted_stub
        state = real_staleness(**kwargs)
        if state is None and not deleted_stub:
            stub_path.unlink()
            deleted_stub = True
        return state

    monkeypatch.setattr(stub_emitter, "stub_staleness", delete_after_freshness_check)

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

    assert deleted_stub is True
    assert report.generated == {"pkg.specs"}
    assert report.failed == {}
    assert report.emitted_stubs["pkg.specs"] == str(stub_path)
    assert stub_path.exists()
    assert (
        real_staleness(
            source_file=spec_path,
            generated_source=(src / "pkg" / "__generated__" / "specs.py").read_text(
                encoding="utf-8"
            ),
        )
        is None
    )


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

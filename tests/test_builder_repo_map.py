"""Tests that run_build threads repo_map_block onto every ModuleSpecContext."""

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


def _entry(*, module: str, qualname: str, source_file: str) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs={},
        class_name=None,
    )


class _CapturingBackend(GeneratorBackend):
    """Backend that records the repo_map_block of each context it sees."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None]:
        self.seen.append(ctx.repo_map_block)
        lines = [f"def {name}():\n    return {name!r}" for name in ctx.expected_names]
        return "\n".join(lines) + "\n", None


def test_run_build_propagates_repo_map_block(tmp_path: Path) -> None:
    spec_path = tmp_path / "mod.py"
    _write(
        spec_path,
        "def do_thing() -> str:\n    ...\n",
    )

    e = _entry(module="pkg.mod", qualname="do_thing", source_file=str(spec_path))
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
            repo_map_block="MAP",
            jobs=1,
        )
    )
    assert not report.failed, report.failed
    assert backend.seen[0] == "MAP"

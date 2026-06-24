"""Tests for whole-class @magic build validation."""

from __future__ import annotations

import asyncio
from pathlib import Path

from jaunt.builder import BuildReport, run_build
from jaunt.deps import build_spec_graph
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext, TokenUsage
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


class Counter:
    """A counter. Starts at zero."""

    def incr(self) -> int:
        raise NotImplementedError


class _StubBackend(GeneratorBackend):
    def __init__(self, source: str) -> None:
        self._source = source

    @property
    def model_name(self) -> str:
        return "stub"

    @property
    def provider_name(self) -> str:
        return "stub"

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, TokenUsage | None]:
        return self._source, None


class _RecordingBackend(GeneratorBackend):
    """Captures the ctx passed to generate_with_retry and returns a fixed source."""

    def __init__(self, source: str) -> None:
        self._source = source
        self.seen_ctx: ModuleSpecContext | None = None

    @property
    def model_name(self) -> str:
        return "rec"

    @property
    def provider_name(self) -> str:
        return "rec"

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, TokenUsage | None]:
        return self._source, None

    async def generate_with_retry(
        self,
        ctx,
        *,
        max_attempts=2,
        extra_validator=None,
        initial_error_context=None,
        progress=None,
    ):
        from jaunt.generate.base import GenerationResult
        from jaunt.validation import validate_generated_source

        self.seen_ctx = ctx
        errs = validate_generated_source(self._source, list(ctx.expected_names))
        if not errs and extra_validator is not None:
            errs = extra_validator(self._source)
        return GenerationResult(attempts=1, source=self._source, errors=errs, usage=None)


def _write_spec(tmp_path: Path) -> Path:
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    spec = pkg / "mod.py"
    spec.write_text(
        "import jaunt\n\n"
        "@jaunt.magic()\n"
        "class Counter:\n"
        '    """A counter. Starts at zero."""\n'
        "    def incr(self) -> int: ...\n",
        encoding="utf-8",
    )
    return spec


def _entry(spec_path: Path) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref("pkg.mod:Counter"),
        module="pkg.mod",
        qualname="Counter",
        source_file=str(spec_path),
        obj=Counter,
        decorator_kwargs={},
        class_name=None,
    )


def _run_build(tmp_path: Path, backend: GeneratorBackend) -> BuildReport:
    spec_path = _write_spec(tmp_path)
    entry = _entry(spec_path)
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.mod": [entry]}
    module_dag: dict[str, set[str]] = {"pkg.mod": set()}
    return asyncio.run(
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


def test_whole_class_build_uses_class_validator(tmp_path: Path) -> None:
    bad_report = _run_build(
        tmp_path,
        _StubBackend('class Counter:\n    """A counter. Starts at zero."""\n    pass\n'),
    )
    bad_errors = bad_report.failed.get("pkg.mod", [])
    assert bad_errors
    assert any("incr" in err for err in bad_errors)

    good_report = _run_build(
        tmp_path,
        _StubBackend(
            "class Counter:\n"
            '    """A counter. Starts at zero."""\n'
            "    def __init__(self) -> None:\n"
            "        self._value = 0\n"
            "    def incr(self) -> int:\n"
            "        self._value += 1\n"
            "        return self._value\n"
        ),
    )
    assert not good_report.failed, good_report.failed


def test_whole_class_component_seeds_scaffold_and_flag(tmp_path: Path) -> None:
    good = (
        "class Counter:\n"
        '    """A counter. Starts at zero."""\n'
        "    def __init__(self) -> None:\n        self._value = 0\n"
        "    def incr(self) -> int:\n        self._value += 1\n        return self._value\n"
    )
    be = _RecordingBackend(good)
    report = _run_build(tmp_path, be)
    assert "pkg.mod" in report.generated, report.failed
    assert be.seen_ctx is not None
    assert be.seen_ctx.whole_class is True
    assert "class Counter" in be.seen_ctx.seed_target_content
    assert "Counter.incr" in be.seen_ctx.whole_class_contract_block


def test_in_loop_validator_rejects_unfilled_stub(tmp_path: Path) -> None:
    stub_out = (
        "class Counter:\n"
        '    """A counter. Starts at zero."""\n'
        "    def incr(self) -> int:\n        raise NotImplementedError\n"
    )
    report = _run_build(tmp_path, _RecordingBackend(stub_out))
    assert "pkg.mod" in report.failed
    assert any("stub" in e for e in report.failed["pkg.mod"])

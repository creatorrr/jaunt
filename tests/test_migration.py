from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from jaunt.builder import plan_refreeze_or_rebuild, write_generated_module
from jaunt.config import SemanticGateConfig
from jaunt.deps import build_spec_graph
from jaunt.digest import module_digest
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
    )


class _FakeExec:
    def __init__(self, reply: str = "MEANINGFUL") -> None:
        self.calls: list[dict[str, object]] = []
        self._reply = reply

    async def __call__(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(final_message=self._reply)


def _scheme1_case(tmp_path: Path) -> SimpleNamespace:
    spec_path = tmp_path / "pkg" / "specs.py"
    _write(
        spec_path,
        '''
def Foo():
    """Return one."""
    raise NotImplementedError
'''.lstrip(),
    )
    entry = _entry(module="pkg.specs", qualname="Foo", source_file=str(spec_path))
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"pkg.specs": [entry]}
    module_dag = {"pkg.specs": set()}
    digest = module_digest("pkg.specs", [entry], specs, spec_graph)
    src = tmp_path / "src"
    header_fields = {
        "tool_version": "0",
        "kind": "build",
        "source_module": "pkg.specs",
        "module_digest": digest,
        "spec_refs": [str(entry.spec_ref)],
    }
    out_path = write_generated_module(
        package_dir=src,
        generated_dir="__generated__",
        module_name="pkg.specs",
        source="def Foo():\n    return 1\n",
        header_fields=header_fields,
    )

    return SimpleNamespace(
        src=src,
        out_path=out_path,
        digest=digest,
        module_specs=module_specs,
        specs=specs,
        spec_graph=spec_graph,
        module_dag=module_dag,
        header_fields=header_fields,
    )


def test_scheme1_fresh_file_refreezes_silently_without_gate_call(tmp_path: Path) -> None:
    """Scheme-1 fresh files silently migrate to scheme 2 without calling the gate."""
    case = _scheme1_case(tmp_path)
    fake = _FakeExec()

    plan = asyncio.run(
        plan_refreeze_or_rebuild(
            package_dir=case.src,
            generated_dir="__generated__",
            module_specs=case.module_specs,
            specs=case.specs,
            spec_graph=case.spec_graph,
            module_dag=case.module_dag,
            stale_modules={"pkg.specs"},
            header_fields_by_module={"pkg.specs": case.header_fields},
            cfg=SemanticGateConfig(),
            gate_enabled=True,
            run_exec=fake,
        )
    )

    assert "pkg.specs" in plan.refrozen
    assert "pkg.specs" not in plan.rebuild
    assert plan.failed_refreeze == set()
    assert fake.calls == []

    text = case.out_path.read_text(encoding="utf-8")
    assert "# jaunt:digest_scheme=2" in text
    assert "# jaunt:spec_digests=" in text
    assert "def Foo():" in text


def test_scheme1_stale_file_routes_to_gate_rebuild_not_silent_refreeze(
    tmp_path: Path,
) -> None:
    """Scheme-1 stale files bypass migration refreeze and route to gate rebuild."""
    case = _scheme1_case(tmp_path)
    fake = _FakeExec(reply="MEANINGFUL")
    stale_header_fields = dict(case.header_fields)
    stale_header_fields["module_digest"] = "sha256:" + "0" * 64

    plan = asyncio.run(
        plan_refreeze_or_rebuild(
            package_dir=case.src,
            generated_dir="__generated__",
            module_specs=case.module_specs,
            specs=case.specs,
            spec_graph=case.spec_graph,
            module_dag=case.module_dag,
            stale_modules={"pkg.specs"},
            header_fields_by_module={"pkg.specs": stale_header_fields},
            cfg=SemanticGateConfig(),
            gate_enabled=True,
            run_exec=fake,
        )
    )

    assert "pkg.specs" not in plan.refrozen
    assert "pkg.specs" in plan.rebuild

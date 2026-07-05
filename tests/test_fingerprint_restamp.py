"""Finding 27: fingerprint-only build restales are re-stamped for free.

Editing a build-prompt template (or `[build] instructions`) changes the
generation fingerprint and restales every already-built module. When the
spec snapshots themselves are all unchanged, those modules must re-freeze
deterministically -- the new fingerprint is stamped over the untouched body
with ZERO semantic-gate/model calls and no backend rebuild.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from jaunt.builder import (
    RefreezePlan,
    _compute_snapshots,
    _compute_spec_digests,
    plan_refreeze_or_rebuild,
    write_generated_module,
)
from jaunt.config import SemanticGateConfig
from jaunt.deps import build_spec_graph
from jaunt.digest import module_digest
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref

OLD_FP = "sha256:" + "a" * 64
NEW_FP = "sha256:" + "b" * 64
GEN_BODY = "def Foo():\n    return 1\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entry(*, source_file: str) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref("pkg.specs:Foo"),
        module="pkg.specs",
        qualname="Foo",
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


def _foo_source(docstring: str) -> str:
    return f'def Foo():\n    """{docstring}"""\n    raise NotImplementedError\n'


def _build_scheme2_module(
    tmp_path: Path,
    *,
    disk_entry: SpecEntry,
    disk_fingerprint: str,
) -> Path:
    """Write a scheme-2 generated module + sidecar for ``disk_entry``."""
    src = tmp_path / "src"
    header_fields = {
        "tool_version": "0",
        "kind": "build",
        "source_module": "pkg.specs",
        "module_digest": module_digest(
            "pkg.specs",
            [disk_entry],
            {disk_entry.spec_ref: disk_entry},
            build_spec_graph({disk_entry.spec_ref: disk_entry}, infer_default=False),
        ),
        "spec_refs": [str(disk_entry.spec_ref)],
        "generation_fingerprint": disk_fingerprint,
    }
    return write_generated_module(
        package_dir=src,
        generated_dir="__generated__",
        module_name="pkg.specs",
        source=GEN_BODY,
        header_fields=header_fields,
        spec_digests=_compute_spec_digests([disk_entry]),
        snapshots=_compute_snapshots([disk_entry]),
    )


def _run_plan(
    src: Path,
    entry: SpecEntry,
    *,
    fingerprint: str,
    fake: _FakeExec,
) -> RefreezePlan:
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    header_fields = {
        "tool_version": "0",
        "kind": "build",
        "source_module": "pkg.specs",
        "module_digest": module_digest("pkg.specs", [entry], specs, spec_graph),
        "spec_refs": [str(entry.spec_ref)],
        "generation_fingerprint": fingerprint,
    }
    plan = asyncio.run(
        plan_refreeze_or_rebuild(
            package_dir=src,
            generated_dir="__generated__",
            module_specs={"pkg.specs": [entry]},
            specs=specs,
            spec_graph=spec_graph,
            module_dag={"pkg.specs": set()},
            stale_modules={"pkg.specs"},
            header_fields_by_module={"pkg.specs": header_fields},
            cfg=SemanticGateConfig(),
            gate_enabled=True,
            run_exec=fake,
        )
    )
    return plan


def test_fingerprint_only_drift_refreezes_for_free(tmp_path: Path) -> None:
    """A stale module with unchanged specs re-stamps the new fingerprint free."""
    spec_path = tmp_path / "pkg" / "specs.py"
    _write(spec_path, _foo_source("Return one."))
    entry = _entry(source_file=str(spec_path))

    out_path = _build_scheme2_module(tmp_path, disk_entry=entry, disk_fingerprint=OLD_FP)
    before = out_path.read_text(encoding="utf-8")
    assert OLD_FP in before

    fake = _FakeExec()
    plan = _run_plan(tmp_path / "src", entry, fingerprint=NEW_FP, fake=fake)

    # Re-stamped, not rebuilt; no gate/model call and no backend invocation.
    assert "pkg.specs" in plan.refrozen
    assert "pkg.specs" not in plan.rebuild
    assert plan.failed_refreeze == set()
    assert fake.calls == []

    after = out_path.read_text(encoding="utf-8")
    # New fingerprint stamped, old one gone, body byte-identical.
    assert NEW_FP in after
    assert OLD_FP not in after
    assert "# jaunt:digest_scheme=2" in after
    assert after.rstrip().endswith(GEN_BODY.rstrip())


def test_real_spec_edit_still_routes_to_gate_rebuild(tmp_path: Path) -> None:
    """A genuine docstring change is not swallowed by the fingerprint short-circuit."""
    old_path = tmp_path / "pkg" / "specs_old.py"
    new_path = tmp_path / "pkg" / "specs_new.py"
    _write(old_path, _foo_source("Return one."))
    _write(new_path, _foo_source("Return the doubled value instead."))
    old_entry = _entry(source_file=str(old_path))
    new_entry = _entry(source_file=str(new_path))

    # On-disk sidecar reflects the OLD docstring; the planner sees the NEW one.
    _build_scheme2_module(tmp_path, disk_entry=old_entry, disk_fingerprint=OLD_FP)

    fake = _FakeExec(reply="MEANINGFUL")
    plan = _run_plan(tmp_path / "src", new_entry, fingerprint=OLD_FP, fake=fake)

    # Prose-only change routes through the semantic gate to a rebuild.
    assert "pkg.specs" in plan.rebuild
    assert "pkg.specs" not in plan.refrozen
    assert fake.calls, "expected the semantic gate to be consulted for a real edit"

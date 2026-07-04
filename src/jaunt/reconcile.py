"""Pure reconciliation helpers for generated Jaunt artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jaunt.builder import _read_generated
from jaunt.header import parse_contract_battery_header, parse_header, parse_stub_header
from jaunt.registry import SpecEntry
from jaunt.stub_emitter import is_jaunt_stub


@dataclass(frozen=True, slots=True)
class OrphanArtifact:
    path: Path
    kind: Literal["generated", "stub", "contract_battery", "sidecar"]
    source_module: str


def find_orphans(
    *,
    package_dir: Path,
    generated_dir: str,
    governed_modules: set[str],
    source_dirs: Sequence[Path],
    battery_dir: Path | None,
    contract_refs: set[str],
) -> list[OrphanArtifact]:
    """Find generated artifacts whose owning spec or contract is no longer governed."""

    orphans: list[OrphanArtifact] = []
    orphaned_generated: list[OrphanArtifact] = []

    generated_root = package_dir / generated_dir
    for path in _rglob_files(generated_root, "*.py"):
        text = _read_text(path)
        if text is None:
            continue
        header = parse_header(text)
        if header is None:
            continue
        source_module = header.get("source_module")
        if source_module is None or source_module in governed_modules:
            continue
        orphan = OrphanArtifact(path=path, kind="generated", source_module=source_module)
        orphans.append(orphan)
        orphaned_generated.append(orphan)

    for source_dir in source_dirs:
        for path in _rglob_files(source_dir, "*.pyi"):
            if not is_jaunt_stub(path):
                continue
            text = _read_text(path)
            if text is None:
                continue
            header = parse_stub_header(text)
            if header is None:
                continue
            source_module = header.get("source_module")
            if source_module is None or source_module in governed_modules:
                continue
            orphans.append(OrphanArtifact(path=path, kind="stub", source_module=source_module))

    if battery_dir is not None and battery_dir.exists():
        for path in _rglob_files(battery_dir, "test_*.py"):
            text = _read_text(path)
            if text is None:
                continue
            header = parse_contract_battery_header(text)
            if header is None:
                continue
            source_module = header.get("derived-from")
            if source_module is None or source_module in contract_refs:
                continue
            orphans.append(
                OrphanArtifact(path=path, kind="contract_battery", source_module=source_module)
            )

    for generated in orphaned_generated:
        sidecar = generated.path.with_name(generated.path.name + ".contract.json")
        if sidecar.exists():
            orphans.append(
                OrphanArtifact(
                    path=sidecar,
                    kind="sidecar",
                    source_module=generated.source_module,
                )
            )

    return sorted(orphans, key=lambda orphan: str(orphan.path))


def newly_governed_specs(
    entries: Sequence[SpecEntry], *, package_dir: Path | None, generated_dir: str
) -> dict[str, list[str]]:
    """Group module-origin specs that do not yet have a generated module."""

    grouped: dict[str, list[str]] = {}
    generated_cache: dict[str, bool] = {}

    for entry in entries:
        if entry.origin != "module":
            continue
        if package_dir is None:
            absent = True
        else:
            absent = generated_cache.get(entry.module)
            if absent is None:
                absent = _read_generated(package_dir, generated_dir, entry.module) is None
                generated_cache[entry.module] = absent
        if not absent:
            continue
        grouped.setdefault(entry.module, []).append(entry.qualname)

    return {module: sorted(symbols) for module, symbols in grouped.items()}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _rglob_files(root: Path, pattern: str) -> Iterable[Path]:
    try:
        yield from (path for path in root.rglob(pattern) if path.is_file())
    except OSError:
        return

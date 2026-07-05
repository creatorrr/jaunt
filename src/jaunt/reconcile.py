"""Pure reconciliation helpers for generated Jaunt artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import jaunt
from jaunt.builder import _read_generated
from jaunt.header import parse_contract_battery_header, parse_header, parse_stub_header
from jaunt.registry import SpecEntry
from jaunt.stub_emitter import is_jaunt_stub


@dataclass(frozen=True, slots=True)
class OrphanArtifact:
    path: Path
    kind: Literal["generated", "stub", "contract_battery", "sidecar"]
    source_module: str


@jaunt.contract
def find_orphans(
    *,
    package_dir: Path,
    generated_dir: str,
    governed_modules: set[str],
    source_dirs: Sequence[Path],
    battery_dir: Path | None,
    contract_refs: set[str],
    classify_test_orphans: bool = True,
) -> list[OrphanArtifact]:
    """Find generated artifacts whose owning spec or contract is no longer governed.

    Scans three artifact families and returns the ones whose owning source is
    gone: generated ``*.py`` under ``package_dir / generated_dir`` whose header
    ``source_module`` is not in ``governed_modules``; jaunt-owned ``*.pyi`` stubs
    under ``source_dirs`` whose header ``source_module`` is not governed; and
    committed contract batteries (``test_*.py``) under ``battery_dir`` whose
    header ``derived-from`` is not in ``contract_refs``. Each orphaned generated
    module also contributes its ``<name>.py.contract.json`` sidecar when present.

    Missing directories are tolerated: a ``package_dir`` / ``battery_dir`` /
    ``source_dir`` that does not exist yields no matches rather than an error, so
    an empty project (no such directories on disk) returns an empty list. The
    result is sorted by path.

    `classify_test_orphans=False` disables orphan classification for generated
    TEST modules (header ``kind=test``) — used as a fail-safe when the governed
    test-module set could not be enumerated completely, so a partial set never
    removes a valid generated test.
    """

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
        if not classify_test_orphans and header.get("kind") == "test":
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


@jaunt.contract
def newly_governed_specs(
    entries: Sequence[SpecEntry], *, package_dir: Path | None, generated_dir: str
) -> dict[str, list[str]]:
    """Group module-origin specs that do not yet have a generated module.

    Considers only entries whose ``origin`` is ``"module"`` (a
    ``jaunt.magic_module`` spec); decorator-origin entries are ignored. A module
    counts as newly governed when it has no generated output yet: when
    ``package_dir`` is ``None`` every module-origin entry is treated as absent,
    otherwise absence is resolved by looking up the generated module on disk
    (cached per module). Present (already-generated) modules are dropped.

    Returns a mapping of module name to the sorted list of newly-governed
    qualnames in it. An empty ``entries`` sequence yields an empty mapping.

    Examples:
    - newly_governed_specs([], package_dir=None, generated_dir="__generated__") == {}
    """

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

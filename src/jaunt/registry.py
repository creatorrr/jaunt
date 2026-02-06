from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jaunt.spec_ref import SpecRef


@dataclass(frozen=True, slots=True)
class SpecEntry:
    kind: Literal["magic", "test"]
    spec_ref: SpecRef
    module: str
    qualname: str
    source_file: str
    obj: object
    decorator_kwargs: dict[str, object]


_MAGIC_REGISTRY: dict[SpecRef, SpecEntry] = {}
_TEST_REGISTRY: dict[SpecRef, SpecEntry] = {}


def register_magic(entry: SpecEntry) -> None:
    """Register a magic spec entry (last write wins)."""

    _MAGIC_REGISTRY[entry.spec_ref] = entry


def register_test(entry: SpecEntry) -> None:
    """Register a test spec entry (last write wins)."""

    _TEST_REGISTRY[entry.spec_ref] = entry


def get_magic_registry() -> dict[SpecRef, SpecEntry]:
    """Return the global magic registry (treat as read-only)."""

    return _MAGIC_REGISTRY


def get_test_registry() -> dict[SpecRef, SpecEntry]:
    """Return the global test registry (treat as read-only)."""

    return _TEST_REGISTRY


def clear_registries() -> None:
    """Clear all global registries (intended for tests)."""

    _MAGIC_REGISTRY.clear()
    _TEST_REGISTRY.clear()


def get_specs_by_module(kind: Literal["magic", "test"]) -> dict[str, list[SpecEntry]]:
    """Group specs by entry.module with stable ordering within each module."""

    if kind == "magic":
        entries = _MAGIC_REGISTRY.values()
    elif kind == "test":
        entries = _TEST_REGISTRY.values()
    else:  # pragma: no cover
        raise ValueError(f"unknown kind: {kind!r}")

    grouped: dict[str, list[SpecEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.module, []).append(entry)

    for module, module_entries in grouped.items():
        module_entries.sort(key=lambda e: (e.qualname, str(e.spec_ref)))
        grouped[module] = module_entries

    return grouped

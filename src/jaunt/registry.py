"""The ledger: global spec registries for magic and test entries.

Every spec is branded at import time, like the Scientific People tattooing
Foyle's face -- an indelible mark of identity.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Literal

from jaunt.spec_ref import SpecRef


@dataclass(frozen=True, slots=True)
class DecoratorApiRecord:
    symbol_path: str
    expression: str
    position: Literal["above_magic", "below_magic"]
    resolved_target: str | None = None
    signature: str | None = None
    annotation_quality: Literal["good", "weak", "missing", "unknown"] = "unknown"


@dataclass(frozen=True, slots=True)
class SpecEntry:
    kind: Literal["magic", "test", "contract"]
    spec_ref: SpecRef
    module: str
    qualname: str
    source_file: str
    obj: object | None
    decorator_kwargs: dict[str, object]
    class_name: str | None = None
    auto_deps: tuple[SpecRef, ...] = ()
    sealed_members: tuple[str, ...] = ()
    base_deps: tuple[SpecRef, ...] = ()
    decorator_api_records: tuple[DecoratorApiRecord, ...] = ()
    effective_signature: str | None = None
    effective_signature_source: Literal["decorated", "original"] | None = None
    decorator_warnings: tuple[str, ...] = ()
    origin: Literal["decorator", "module"] = "decorator"


@dataclass(frozen=True, slots=True)
class ModuleMagicDefaults:
    module: str
    source_file: str
    decorator_kwargs: dict[str, object]


_MAGIC_REGISTRY: dict[SpecRef, SpecEntry] = {}
_TEST_REGISTRY: dict[SpecRef, SpecEntry] = {}
_CONTRACT_REGISTRY: dict[SpecRef, SpecEntry] = {}
_MODULE_MAGIC_REGISTRY: dict[str, ModuleMagicDefaults] = {}


def register_contract(entry: SpecEntry) -> None:
    """Register a contract spec entry (last write wins)."""

    _CONTRACT_REGISTRY[entry.spec_ref] = entry


def get_contract_registry() -> dict[SpecRef, SpecEntry]:
    """Return the global contract registry (treat as read-only)."""

    return _CONTRACT_REGISTRY


def register_magic(entry: SpecEntry) -> None:
    """Register a magic spec entry (last write wins)."""

    existing = _MAGIC_REGISTRY.get(entry.spec_ref)
    if existing is not None and existing.origin != entry.origin:
        warnings.warn(
            f"jaunt spec {entry.spec_ref!s} registered from both a module scan "
            "and a decorator; the decorator registration wins. This usually means "
            "the module scan failed to skip a decorated symbol (aliased import?).",
            UserWarning,
            stacklevel=2,
        )
    _MAGIC_REGISTRY[entry.spec_ref] = entry


def register_module_magic(defaults: ModuleMagicDefaults) -> None:
    """Register the module-level magic defaults for a governed module."""

    _MODULE_MAGIC_REGISTRY[defaults.module] = defaults


def get_module_magic_defaults(module: str) -> ModuleMagicDefaults | None:
    """Return the module-magic defaults for ``module`` (``None`` if ungoverned)."""

    return _MODULE_MAGIC_REGISTRY.get(module)


def get_module_magic_registry() -> dict[str, ModuleMagicDefaults]:
    """Return the global module-magic registry (treat as read-only)."""

    return _MODULE_MAGIC_REGISTRY


def unregister_magic(spec_ref: SpecRef) -> SpecEntry | None:
    """Remove and return a magic spec entry (``None`` if absent).

    Used by whole-class absorption: inner ``@magic`` method specs are folded
    into their class's spec at class-decoration time.
    """

    return _MAGIC_REGISTRY.pop(spec_ref, None)


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
    _CONTRACT_REGISTRY.clear()
    _MODULE_MAGIC_REGISTRY.clear()


def get_specs_by_module(kind: Literal["magic", "test", "contract"]) -> dict[str, list[SpecEntry]]:
    """Group specs by entry.module with stable ordering within each module."""

    if kind == "magic":
        entries = _MAGIC_REGISTRY.values()
    elif kind == "test":
        entries = _TEST_REGISTRY.values()
    elif kind == "contract":
        entries = _CONTRACT_REGISTRY.values()
    else:  # pragma: no cover
        raise ValueError(f"unknown kind: {kind!r}")

    grouped: dict[str, list[SpecEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.module, []).append(entry)

    for module, module_entries in grouped.items():
        module_entries.sort(key=lambda e: (e.qualname, str(e.spec_ref)))
        grouped[module] = module_entries

    return grouped

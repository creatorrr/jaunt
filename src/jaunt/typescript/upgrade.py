"""Compatibility checks for model-free TypeScript toolchain upgrades."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_SEMANTIC_CONTRACT_FIELDS = (
    "moduleId",
    "specPath",
    "facadePath",
    "apiMirrorPath",
    "implementationPath",
    "contextPath",
    "project",
    "packageOwner",
    "dependencies",
    "options",
    "symbols",
    "typeDeclarations",
    "typeImports",
    "contextDocs",
    "semanticEnvironmentDigest",
)

_REQUIRED_SEMANTIC_CONTRACT_FIELDS = (
    "moduleId",
    "symbols",
    "options",
    "typeDeclarations",
    "typeImports",
    "contextDocs",
    "semanticEnvironmentDigest",
    "dependencies",
)


def semantic_contract_payload(sidecar: Mapping[str, Any]) -> dict[str, Any]:
    """Return the model-facing contract fields independent of digest schemes.

    TypeScript worker releases may change how structural and API digests are
    encoded without changing the authored contract.  These fields are the
    actual declaration, dependency, route, and context inputs that authorize a
    previous candidate for deterministic recomposition.
    """

    return {field: sidecar.get(field) for field in _SEMANTIC_CONTRACT_FIELDS}


def has_compatible_semantic_contract(
    actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    """Whether two sidecars describe the same model-facing contract."""

    # Older or malformed sidecars without the analyzer-owned semantic payload
    # cannot prove that a digest-only change is safe to reuse.
    if any(
        field not in actual or field not in expected for field in _REQUIRED_SEMANTIC_CONTRACT_FIELDS
    ):
        return False
    return semantic_contract_payload(actual) == semantic_contract_payload(expected)


def compatible_semantic_modules(
    root: Path, modules: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...]
) -> frozenset[str]:
    """Return modules whose saved contracts match through their dependency closure."""

    expected_by_id: dict[str, Mapping[str, Any]] = {}
    self_compatible: set[str] = set()
    dependencies: dict[str, set[str]] = {}
    for module in modules:
        module_id = module.get("moduleId")
        sidecar_path = module.get("sidecarPath")
        if not isinstance(sidecar_path, str):
            routes = module.get("routes")
            if isinstance(routes, Mapping):
                sidecar_path = routes.get("sidecarPath")
        expected_value = module.get("sidecar")
        if not isinstance(module_id, str) or not isinstance(sidecar_path, str):
            continue
        if isinstance(expected_value, str):
            try:
                expected = json.loads(expected_value)
            except json.JSONDecodeError:
                continue
        elif isinstance(expected_value, Mapping):
            expected = expected_value
        else:
            continue
        if not isinstance(expected, Mapping):
            continue
        expected_by_id[module_id] = expected
        try:
            actual_path = (root / sidecar_path).resolve()
            actual_path.relative_to(root.resolve())
            actual = json.loads(actual_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(actual, Mapping) or not has_compatible_semantic_contract(
            actual, expected
        ):
            continue
        self_compatible.add(module_id)
        raw_dependencies = expected.get("dependencies", ())
        dependencies[module_id] = {
            item.partition("#")[0]
            for item in raw_dependencies
            if isinstance(item, str) and item.partition("#")[0] != module_id
        }

    compatible: set[str] = set()
    pending = set(self_compatible)
    while pending:
        ready = {
            module_id
            for module_id in pending
            if dependencies.get(module_id, set()) <= compatible
            and dependencies.get(module_id, set()) <= expected_by_id.keys()
        }
        if not ready:
            break
        compatible.update(ready)
        pending.difference_update(ready)
    return frozenset(compatible)

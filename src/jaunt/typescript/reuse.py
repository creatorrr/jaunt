"""Persist proof for model-free TypeScript API-boundary reuse.

The proof lives under ``.jaunt/`` because it connects two consecutive local
artifact states.  Generated batteries remain the committed source of truth;
this cache only lets a later ``jaunt test`` verify that their recorded API
boundary was replaced by a compiler-validated equivalent during ``jaunt build``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from itertools import product
from pathlib import Path
from typing import Any

_SCHEMA = 1
_PROOF_PATH = Path(".jaunt/typescript/test-battery-reuse.json")
_RECORD_KEYS = frozenset({"moduleId", "apiDigest", "apiSourceDigest"})


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return _sha256(encoded)


def _module_path(module: Mapping[str, Any], key: str) -> str | None:
    value = module.get(key)
    if not isinstance(value, str):
        routes = module.get("routes")
        value = routes.get(key) if isinstance(routes, Mapping) else None
    if not isinstance(value, str):
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return value


def _record(module_id: object, api_digest: object, api_source: object) -> dict[str, str] | None:
    if not all(isinstance(value, str) for value in (module_id, api_digest, api_source)):
        return None
    return {
        "moduleId": str(module_id),
        "apiDigest": str(api_digest),
        "apiSourceDigest": _sha256(str(api_source)),
    }


def expected_target_api_record(module: Mapping[str, Any]) -> dict[str, str] | None:
    """Return the API record used by generated-battery provenance."""

    return _record(module.get("moduleId"), module.get("apiDigest"), module.get("apiSource"))


def live_target_api_record(root: Path, module: Mapping[str, Any]) -> dict[str, str] | None:
    """Read the committed API record that existed before an artifact transaction."""

    api_path = _module_path(module, "apiMirrorPath")
    sidecar_path = _module_path(module, "sidecarPath")
    if api_path is None or sidecar_path is None:
        return None
    try:
        api_source = (root / api_path).read_text(encoding="utf-8")
        sidecar = json.loads((root / sidecar_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(sidecar, Mapping):
        return None
    return _record(sidecar.get("moduleId"), sidecar.get("apiDigest"), api_source)


def capture_target_api_records(
    root: Path, modules: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, str]]:
    """Snapshot committed API records before a build writes artifacts."""

    records: dict[str, dict[str, str]] = {}
    for module in modules:
        module_id = module.get("moduleId")
        record = live_target_api_record(root, module)
        if isinstance(module_id, str) and record is not None and record["moduleId"] == module_id:
            records[module_id] = record
    return records


def target_api_digest(modules: Sequence[Mapping[str, Any]]) -> str:
    """Hash the selected modules exactly as generated-battery provenance does."""

    records = [expected_target_api_record(module) for module in modules]
    return _canonical_digest([record for record in records if record is not None])


def _valid_record(value: object, *, module_id: str) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or set(value) != _RECORD_KEYS:
        return None
    record = {str(key): item for key, item in value.items() if isinstance(item, str)}
    if set(record) != _RECORD_KEYS or record.get("moduleId") != module_id:
        return None
    return record


def _load(root: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads((root / _PROOF_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, Mapping) or payload.get("schema") != _SCHEMA:
        return {}
    raw = payload.get("transitions")
    if not isinstance(raw, Mapping):
        return {}
    transitions: dict[str, dict[str, Any]] = {}
    for module_id, value in raw.items():
        if not isinstance(module_id, str) or not isinstance(value, Mapping):
            continue
        current = _valid_record(value.get("current"), module_id=module_id)
        raw_previous = value.get("previous")
        if current is None or not isinstance(raw_previous, list):
            continue
        previous = [
            record
            for item in raw_previous
            if (record := _valid_record(item, module_id=module_id)) is not None
        ]
        if previous:
            transitions[module_id] = {"previous": previous, "current": current}
    return transitions


def _store(root: Path, transitions: Mapping[str, Mapping[str, Any]]) -> None:
    path = root / _PROOF_PATH
    if not transitions:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": _SCHEMA,
            "transitions": {key: transitions[key] for key in sorted(transitions)},
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except OSError:
        # This cache is an optimization. A read-only or broken .jaunt directory
        # must never turn a successful validated build into a failure.
        return


def update_target_api_reuse_proof(
    root: Path,
    *,
    before: Mapping[str, Mapping[str, str]],
    modules: Sequence[Mapping[str, Any]],
    reused_module_ids: set[str],
    touched_module_ids: set[str],
) -> None:
    """Record validated old-to-new API transitions for a later test command."""

    transitions = _load(root)
    by_id = {
        str(module_id): module
        for module in modules
        if isinstance((module_id := module.get("moduleId")), str)
    }
    for module_id in touched_module_ids:
        if module_id not in reused_module_ids:
            transitions.pop(module_id, None)
            continue
        old = before.get(module_id)
        module = by_id.get(module_id)
        new = live_target_api_record(root, module) if module is not None else None
        if old is None or new is None:
            transitions.pop(module_id, None)
            continue
        existing = transitions.get(module_id)
        if old == new:
            if existing is None or existing.get("current") != new:
                transitions.pop(module_id, None)
            continue
        previous: list[dict[str, str]] = []
        if existing is not None and existing.get("current") == old:
            raw_previous = existing.get("previous", [])
            if isinstance(raw_previous, list):
                previous.extend(item for item in raw_previous if isinstance(item, dict))
        previous.append(dict(old))
        deduplicated: list[dict[str, str]] = []
        for record in previous:
            if record != new and record not in deduplicated:
                deduplicated.append(record)
        transitions[module_id] = {
            "previous": deduplicated[-8:],
            "current": new,
        }
    _store(root, transitions)


def proven_previous_target_api_digests(
    root: Path,
    modules: Sequence[Mapping[str, Any]],
    *,
    additional_previous: Mapping[str, Mapping[str, str]] | None = None,
) -> frozenset[str]:
    """Return prior aggregate API digests proven equivalent to this boundary."""

    transitions = _load(root)
    if not transitions and not additional_previous:
        return frozenset()
    choices: list[list[dict[str, str]]] = []
    combinations = 1
    for module in modules:
        current = expected_target_api_record(module)
        if current is None:
            return frozenset()
        module_id = current["moduleId"]
        transition = transitions.get(module_id)
        options = [current]
        if transition is not None and transition.get("current") == current:
            raw_previous = transition.get("previous", [])
            if isinstance(raw_previous, list):
                options.extend(item for item in raw_previous if isinstance(item, dict))
        if additional_previous is not None:
            extra = _valid_record(additional_previous.get(module_id), module_id=module_id)
            if extra is not None and extra != current and extra not in options:
                options.append(extra)
        choices.append(options)
        combinations *= len(options)
        if combinations > 256:
            # Battery target fan-out is normally tiny. Fail closed rather than
            # let a long upgrade history create combinatorial work.
            return frozenset()
    current_digest = _canonical_digest([options[0] for options in choices])
    return frozenset(
        digest
        for records in product(*choices)
        if (digest := _canonical_digest(list(records))) != current_digest
    )


__all__ = [
    "capture_target_api_records",
    "expected_target_api_record",
    "proven_previous_target_api_digests",
    "target_api_digest",
    "update_target_api_reuse_proof",
]

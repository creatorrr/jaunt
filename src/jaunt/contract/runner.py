"""Wire digests + battery header + drift state for a contract function."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jaunt.contract.battery import parse_battery
from jaunt.contract.drift import DriftState, compute_drift_state
from jaunt.digest import contract_digests
from jaunt.registry import SpecEntry


def battery_path(root: Path, battery_dir: str, entry: SpecEntry) -> Path:
    parts = entry.module.split(".")
    return root / battery_dir / Path(*parts) / f"test_{entry.qualname}.py"


@dataclass(frozen=True, slots=True)
class ContractStatus:
    spec_ref: str
    state: DriftState
    strength: str | None
    battery_path: Path
    detail: str = ""


def _norm(value: str) -> str:
    return value[len("sha256:") :] if value.startswith("sha256:") else value


def evaluate_entry(
    root: Path,
    battery_dir: str,
    derive: list[str],
    entry: SpecEntry,
    *,
    run_battery: Callable[[Path], bool | None],
) -> ContractStatus:
    path = battery_path(root, battery_dir, entry)
    spec_ref = str(entry.spec_ref)

    if not path.is_file():
        return ContractStatus(spec_ref, DriftState.UNBUILT, None, path)

    parsed = parse_battery(path.read_text(encoding="utf-8"))
    header = parsed.header
    if header is None:
        return ContractStatus(spec_ref, DriftState.UNBUILT, None, path)

    digs = contract_digests(entry.source_file, entry.qualname)
    prose_match = _norm(header.get("prose-digest", "")) == digs.prose
    signature_match = _norm(header.get("signature", "")) == digs.signature
    body_match = _norm(header.get("body-digest", "")) == digs.body
    strength = header.get("strength")

    # Short-circuit before running the battery (steps 1-3).
    if not (prose_match and signature_match):
        state = compute_drift_state(
            has_battery=True,
            prose_match=prose_match,
            signature_match=signature_match,
            body_match=body_match,
            battery_passed=None,
        )
        return ContractStatus(spec_ref, state, strength, path)

    passed = run_battery(path)
    state = compute_drift_state(
        has_battery=True,
        prose_match=prose_match,
        signature_match=signature_match,
        body_match=body_match,
        battery_passed=passed,
    )
    return ContractStatus(spec_ref, state, strength, path)


def run_battery_file(path: Path, *, root: Path, source_roots: list[str]) -> bool:
    """Run a single battery file with pytest in a subprocess. True == all passed."""

    import os

    env = dict(os.environ)
    extra = os.pathsep.join(str((root / sr).resolve()) for sr in source_roots)
    env["PYTHONPATH"] = extra + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(path),
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "--import-mode=importlib",
        ],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    spec_ref: str
    ok: bool
    strength: str
    failures: list[str]
    battery_path: Path
    wrote: bool


def reconcile_entry(
    root: Path,
    battery_dir: str,
    derive: list[str],
    strength_enabled: bool,
    entry: SpecEntry,
    *,
    module_namespace: dict[str, object],
    tool_version: str,
) -> ReconcileResult:
    from jaunt.contract.derive import (
        derive_regions,
        evaluate_blocks,
        extract_blocks_structured,
    )
    from jaunt.contract.strength import compute_strength, format_strength
    from jaunt.digest import contract_digests, load_function_node

    spec_ref = str(entry.spec_ref)
    path = battery_path(root, battery_dir, entry)

    node = load_function_node(entry.source_file, entry.qualname)
    docstring = _docstring_of(node)
    blocks = extract_blocks_structured(docstring)

    fn = module_namespace.get(entry.qualname)
    if not callable(fn):
        return ReconcileResult(spec_ref, False, "0/0", ["function not importable"], path, False)

    failures = evaluate_blocks(fn, blocks, module_namespace)
    if failures:
        return ReconcileResult(spec_ref, False, "0/0", failures, path, False)

    digs = contract_digests(entry.source_file, entry.qualname)
    strength = "0/0"
    if strength_enabled:
        import ast

        func_src = ast.unparse(node)
        killed, applicable = compute_strength(func_src, entry.qualname, blocks, module_namespace)
        strength = format_strength(killed, applicable)

    regions = derive_regions(blocks, func_name=entry.qualname, derive=derive)
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    from jaunt.contract.battery import merge_battery

    text = merge_battery(
        existing,
        import_module=entry.module,
        func_name=entry.qualname,
        regions=regions,
        header_fields={
            "derived_from": spec_ref,
            "prose_digest": digs.prose,
            "signature": digs.signature,
            "body_digest": digs.body,
            "strength": strength,
            "tool_version": tool_version,
        },
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return ReconcileResult(spec_ref, True, strength, [], path, True)


def _docstring_of(node) -> str:
    import ast

    return ast.get_docstring(node, clean=True) or ""

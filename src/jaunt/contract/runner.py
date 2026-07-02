"""Wire digests + battery header + drift state for a contract function."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jaunt.contract.battery import parse_battery
from jaunt.contract.derive import ContractBlocks
from jaunt.contract.drift import DriftState, compute_drift_state
from jaunt.digest import contract_digests
from jaunt.registry import SpecEntry


def battery_path(root: Path, battery_dir: str, entry: SpecEntry) -> Path:
    parts = entry.module.split(".")
    fname = f"test_{entry.qualname.replace('.', '_')}.py"
    return root / battery_dir / Path(*parts) / fname


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
            "-p",
            "pytest_asyncio",
            "-o",
            "asyncio_mode=auto",
        ],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode in (0, 5)


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
    model_extract: Callable[[str], ContractBlocks] | None = None,
    source_roots: list[str] | None = None,
) -> ReconcileResult:
    import ast

    from jaunt.contract.battery import merge_battery
    from jaunt.contract.cases import CaseParseError, parse_case_blocks
    from jaunt.contract.derive import (
        battery_extra_imports,
        derive_case_regions,
        evaluate_cases,
    )
    from jaunt.contract.strength import compute_case_strength, format_strength
    from jaunt.digest import contract_digests, load_contract_node

    spec_ref = str(entry.spec_ref)
    path = battery_path(root, battery_dir, entry)
    source_roots = source_roots or []

    node = load_contract_node(entry.source_file, entry.qualname)
    if isinstance(node, ast.ClassDef):
        # Task 9 wires whole-class reconcile; keep the error explicit until then.
        raise ValueError("whole-class reconcile not wired yet (adoption-parity Task 9)")

    module_names = _module_top_level_names(entry.source_file)
    async_map = {entry.qualname: isinstance(node, ast.AsyncFunctionDef)}
    docstring = _docstring_of(node)

    try:
        blocks = parse_case_blocks(
            docstring,
            target=entry.qualname,
            async_map=async_map,
            module_names=module_names,
        )
    except CaseParseError as exc:
        return ReconcileResult(spec_ref, False, "0/0", [f"{exc} (line: {exc.line})"], path, False)

    if blocks.is_empty() and model_extract is not None and docstring.strip():
        legacy = model_extract(docstring)
        blocks = parse_case_blocks(
            _legacy_blocks_to_docstring(legacy),
            target=entry.qualname,
            async_map=async_map,
            module_names=module_names,
        )

    fn = module_namespace.get(entry.qualname)
    if not callable(fn):
        return ReconcileResult(spec_ref, False, "0/0", ["function not importable"], path, False)

    eval_ns: dict[str, object] = {entry.qualname: fn}
    for case in (*blocks.examples, *blocks.raises):
        for name in case.imports:
            eval_ns[name] = module_namespace.get(name)

    failures = evaluate_cases(blocks, namespace=eval_ns)
    if failures:
        return ReconcileResult(spec_ref, False, "0/0", failures, path, False)

    digs = contract_digests(entry.source_file, entry.qualname)
    strength = "0/0"
    excluded = 0
    if strength_enabled:
        strength_ns = dict(eval_ns)
        for name in module_names:
            if name in module_namespace:
                strength_ns[name] = module_namespace[name]
        killed, applicable, excluded = compute_case_strength(
            ast.unparse(node), entry.qualname, blocks, strength_ns
        )
        strength = format_strength(killed, applicable)

    regions = derive_case_regions(blocks, target=entry.qualname, derive=derive)
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
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
            "strength_excluded": str(excluded),
            "tool_version": tool_version,
        },
        extra_imports=battery_extra_imports(blocks),
    )

    if blocks.has_fixture_cases():
        ok = _validate_via_pytest(text, path, root=root, entry=entry, source_roots=source_roots)
        if not ok:
            return ReconcileResult(
                spec_ref,
                False,
                "0/0",
                ["fixture-dependent cases failed under pytest; run the battery for detail"],
                path,
                False,
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return ReconcileResult(spec_ref, True, strength, [], path, True)


def _module_top_level_names(source_file: str) -> frozenset[str]:
    import ast

    tree = ast.parse(Path(source_file).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return frozenset(names)


def _legacy_blocks_to_docstring(blocks: ContractBlocks) -> str:
    lines = []
    if blocks.examples:
        lines.append("Examples:")
        lines += [f"    - {r.input_expr} -> {r.expected_expr}" for r in blocks.examples]
        lines.append("")
    if blocks.raises:
        lines.append("Raises:")
        lines += [f"    - {r.input_expr} raises {r.exc_name}" for r in blocks.raises]
    return "\n".join(lines)


def _validate_via_pytest(
    text: str,
    path: Path,
    *,
    root: Path,
    entry: SpecEntry,
    source_roots: list[str],
) -> bool:
    """Write the merged battery to a temp sibling, run it, and always clean up."""

    tmp = path.with_name(f"_jaunt_validate_{path.name}")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    module_parts = entry.module.split(".")
    derived_root = Path(entry.source_file).resolve().parents[len(module_parts) - 1]
    effective_roots = [str(derived_root), *source_roots]
    try:
        tmp.write_text(text, encoding="utf-8")
        return run_battery_file(tmp, root=root, source_roots=effective_roots)
    finally:
        tmp.unlink(missing_ok=True)


def _docstring_of(node) -> str:
    import ast

    return ast.get_docstring(node, clean=True) or ""

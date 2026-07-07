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
    strength_excluded: int = 0


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
    excluded = int(header.get("strength-excluded", "0"))

    # Short-circuit before running the battery (steps 1-3).
    if not (prose_match and signature_match):
        state = compute_drift_state(
            has_battery=True,
            prose_match=prose_match,
            signature_match=signature_match,
            body_match=body_match,
            battery_passed=None,
        )
        return ContractStatus(spec_ref, state, strength, path, strength_excluded=excluded)

    passed = run_battery(path)
    state = compute_drift_state(
        has_battery=True,
        prose_match=prose_match,
        signature_match=signature_match,
        body_match=body_match,
        battery_passed=passed,
    )
    return ContractStatus(spec_ref, state, strength, path, strength_excluded=excluded)


def run_battery_file(path: Path, *, root: Path, source_roots: list[str]) -> bool:
    """Run a single battery file with pytest in a subprocess. True == all passed."""

    import os

    env = dict(os.environ)
    extra = os.pathsep.join(str((root / sr).resolve()) for sr in source_roots)
    env["PYTHONPATH"] = extra + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # Property batteries disable the Hypothesis example database (database=None),
    # but Hypothesis still writes derived caches (unicode data, constants) under
    # .hypothesis/ in the cwd. Redirect them into jaunt's sidecar dir so
    # check/reconcile never dirty the adopter's working tree. Outcomes are
    # unaffected: the caches are derived data, not replayed examples.
    env.setdefault("HYPOTHESIS_STORAGE_DIRECTORY", str(root / ".jaunt" / "hypothesis"))
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
    strength_excluded: int = 0


def reconcile_entry(
    root: Path,
    battery_dir: str,
    derive: list[str],
    strength_enabled: bool,
    entry: SpecEntry,
    *,
    module_namespace: dict[str, object],
    tool_version: str,
    model_extract: Callable[[str, str], ContractBlocks] | None = None,
    source_roots: list[str] | None = None,
    property_max_examples: int = 50,
) -> ReconcileResult:
    import ast

    from jaunt.contract.battery import merge_battery
    from jaunt.contract.cases import CaseParseError, parse_case_blocks
    from jaunt.contract.derive import (
        battery_extra_imports,
        derive_case_regions,
        evaluate_cases,
    )
    from jaunt.contract.properties import (
        PropertyBlocks,
        parse_property_blocks,
        properties_extra_imports,
        render_properties_region,
    )
    from jaunt.contract.strength import compute_case_strength, format_strength
    from jaunt.digest import contract_digests, load_contract_node

    spec_ref = str(entry.spec_ref)
    path = battery_path(root, battery_dir, entry)
    source_roots = source_roots or []

    node = load_contract_node(entry.source_file, entry.qualname)
    if isinstance(node, ast.ClassDef):
        return _reconcile_class(
            node,
            root=root,
            battery_dir=battery_dir,
            derive=derive,
            strength_enabled=strength_enabled,
            entry=entry,
            module_namespace=module_namespace,
            tool_version=tool_version,
            path=path,
            spec_ref=spec_ref,
            source_roots=source_roots,
            property_max_examples=property_max_examples,
        )

    module_names = _module_top_level_names(entry.source_file)
    async_map = {entry.qualname: isinstance(node, ast.AsyncFunctionDef)}
    docstring = _docstring_of(node)
    want_properties = "properties" in derive

    def _parse_props(doc: str) -> PropertyBlocks:
        return parse_property_blocks(
            doc, target=entry.qualname, async_map=async_map, module_names=module_names
        )

    pblocks = PropertyBlocks()
    try:
        blocks = parse_case_blocks(
            docstring,
            target=entry.qualname,
            async_map=async_map,
            module_names=module_names,
        )
        if want_properties:
            pblocks = _parse_props(docstring)
        if (
            blocks.is_empty()
            and not pblocks.cases
            and model_extract is not None
            and docstring.strip()
        ):
            legacy = model_extract(docstring, entry.qualname)
            synth = _legacy_blocks_to_docstring(legacy)
            blocks = parse_case_blocks(
                synth,
                target=entry.qualname,
                async_map=async_map,
                module_names=module_names,
            )
            if want_properties:
                pblocks = _parse_props(synth)
        elif want_properties and pblocks.prose and model_extract is not None:
            # Structured and prose bullets coexist: send only the prose bullets
            # through the model, round-trip its rows through the Tier-1 grammar,
            # and merge with the deterministically parsed cases.
            prose_doc = "Properties:\n" + "\n".join(f"- {b}" for b in pblocks.prose)
            legacy = model_extract(prose_doc, entry.qualname)
            synth = _legacy_blocks_to_docstring(ContractBlocks(properties=legacy.properties))
            reparsed = _parse_props(synth)
            if reparsed.prose:
                bad = "; ".join(reparsed.prose)
                return ReconcileResult(
                    spec_ref,
                    False,
                    "0/0",
                    [f"model-derived property is not in 'given … :: …' form: {bad}"],
                    path,
                    False,
                )
            pblocks = PropertyBlocks(cases=(*pblocks.cases, *reparsed.cases), prose=pblocks.prose)
    except CaseParseError as exc:
        return ReconcileResult(spec_ref, False, "0/0", [f"{exc} (line: {exc.line})"], path, False)

    if pblocks.cases and not _hypothesis_importable():
        return ReconcileResult(
            spec_ref,
            False,
            "0/0",
            [
                "contract.derive includes 'properties' but the 'hypothesis' package is "
                "not importable in this environment; install hypothesis>=6"
            ],
            path,
            False,
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
        # Property cases never enter the per-mutant loop (wrong cost profile);
        # count them so the score stays honest about what it measured.
        excluded += len(pblocks.cases)

    regions = derive_case_regions(blocks, target=entry.qualname, derive=derive)
    if pblocks.cases:
        regions.append(render_properties_region(pblocks.cases, max_examples=property_max_examples))
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
        extra_imports=tuple(
            sorted({*battery_extra_imports(blocks), *properties_extra_imports(pblocks)})
        ),
    )

    if blocks.has_fixture_cases() or pblocks.cases:
        ok = _validate_via_pytest(text, path, root=root, entry=entry, source_roots=source_roots)
        if not ok:
            return ReconcileResult(
                spec_ref,
                False,
                "0/0",
                [
                    "fixture- or property-based cases failed under pytest; "
                    "run the battery for detail"
                ],
                path,
                False,
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return ReconcileResult(spec_ref, True, strength, [], path, True, strength_excluded=excluded)


def _reconcile_class(
    node,
    *,
    root: Path,
    battery_dir: str,
    derive: list[str],
    strength_enabled: bool,
    entry: SpecEntry,
    module_namespace: dict[str, object],
    tool_version: str,
    path: Path,
    spec_ref: str,
    source_roots: list[str],
    property_max_examples: int = 50,
) -> ReconcileResult:
    import ast

    from jaunt.contract.battery import merge_battery
    from jaunt.contract.cases import CaseBlocks, CaseParseError, parse_case_blocks
    from jaunt.contract.derive import (
        battery_extra_imports,
        derive_case_regions,
        evaluate_cases,
    )
    from jaunt.contract.properties import (
        PropertyBlocks,
        parse_property_blocks,
        properties_extra_imports,
        render_properties_region,
    )
    from jaunt.contract.strength import compute_case_strength, format_strength
    from jaunt.digest import contract_digests

    cls_name = entry.qualname
    methods = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
    async_map: dict[str, bool] = {cls_name: False}
    for m in methods:
        async_map[f"{cls_name}.{m.name}"] = isinstance(m, ast.AsyncFunctionDef)
    module_names = _module_top_level_names(entry.source_file)
    want_properties = "properties" in derive

    # Partition by which docstring each case came from: class-docstring cases
    # render in the base `examples`/`errors` regions, method-docstring cases in
    # `examples-<method>`/`errors-<method>` regions. (Do NOT partition by
    # `case.method` — a class-docstring case like `Counter(1).peek() == 1` has
    # `method="peek"` for async resolution but still belongs to the class region.)
    # Tier-2 (model-derived) properties are function-path only in v1; class
    # docstrings get the deterministic Tier-1 grammar, prose bullets are skipped.
    class_props = PropertyBlocks()
    method_props: list[tuple[str, PropertyBlocks]] = []
    all_props = PropertyBlocks()
    try:
        class_doc = ast.get_docstring(node, clean=True) or ""
        class_doc_blocks = parse_case_blocks(
            class_doc,
            target=cls_name,
            async_map=async_map,
            module_names=module_names,
        )
        if want_properties:
            class_props = parse_property_blocks(
                class_doc, target=cls_name, async_map=async_map, module_names=module_names
            )
            all_props = class_props
        all_blocks = class_doc_blocks
        method_blocks: list[tuple[str, CaseBlocks]] = []
        for m in methods:
            if m.name.startswith("_"):
                continue
            doc = ast.get_docstring(m, clean=True) or ""
            if not doc:
                continue
            mb = parse_case_blocks(
                doc,
                target=cls_name,
                async_map=async_map,
                module_names=module_names,
                method=m.name,
            )
            if not mb.is_empty():
                method_blocks.append((m.name, mb))
                all_blocks = all_blocks.merged(mb)
            if want_properties:
                mp = parse_property_blocks(
                    doc, target=cls_name, async_map=async_map, module_names=module_names
                )
                if mp.cases:
                    method_props.append((m.name, mp))
                    all_props = all_props.merged(mp)
    except CaseParseError as exc:
        return ReconcileResult(spec_ref, False, "0/0", [f"{exc} (line: {exc.line})"], path, False)

    if all_props.cases and not _hypothesis_importable():
        return ReconcileResult(
            spec_ref,
            False,
            "0/0",
            [
                "contract.derive includes 'properties' but the 'hypothesis' package is "
                "not importable in this environment; install hypothesis>=6"
            ],
            path,
            False,
        )

    cls_obj = module_namespace.get(cls_name)
    if not callable(cls_obj):
        return ReconcileResult(spec_ref, False, "0/0", ["class not importable"], path, False)

    eval_ns: dict[str, object] = {cls_name: cls_obj}
    for case in (*all_blocks.examples, *all_blocks.raises):
        for name in case.imports:
            eval_ns[name] = module_namespace.get(name)

    failures = evaluate_cases(all_blocks, namespace=eval_ns)
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
            ast.unparse(node), cls_name, all_blocks, strength_ns
        )
        strength = format_strength(killed, applicable)
        excluded += len(all_props.cases)

    regions = derive_case_regions(class_doc_blocks, target=cls_name, derive=derive)
    if class_props.cases:
        regions.append(
            render_properties_region(class_props.cases, max_examples=property_max_examples)
        )
    for name, mb in method_blocks:
        regions += derive_case_regions(mb, target=cls_name, derive=derive, region_suffix=name)
    for name, mp in method_props:
        regions.append(
            render_properties_region(
                mp.cases, max_examples=property_max_examples, region_suffix=name
            )
        )

    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    text = merge_battery(
        existing,
        import_module=entry.module,
        func_name=cls_name,
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
        extra_imports=tuple(
            sorted({*battery_extra_imports(all_blocks), *properties_extra_imports(all_props)})
        ),
    )

    if all_blocks.has_fixture_cases() or all_props.cases:
        if not _validate_via_pytest(text, path, root=root, entry=entry, source_roots=source_roots):
            return ReconcileResult(
                spec_ref,
                False,
                "0/0",
                [
                    "fixture- or property-based cases failed under pytest; "
                    "run the battery for detail"
                ],
                path,
                False,
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return ReconcileResult(spec_ref, True, strength, [], path, True, strength_excluded=excluded)


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


def _hypothesis_importable() -> bool:
    import importlib.util

    return importlib.util.find_spec("hypothesis") is not None


def _legacy_blocks_to_docstring(blocks: ContractBlocks) -> str:
    lines = []
    if blocks.examples:
        lines.append("Examples:")
        lines += [f"    - {r.input_expr} -> {r.expected_expr}" for r in blocks.examples]
        lines.append("")
    if blocks.raises:
        lines.append("Raises:")
        lines += [f"    - {r.input_expr} raises {r.exc_name}" for r in blocks.raises]
        lines.append("")
    if blocks.properties:
        lines.append("Properties:")
        lines += [f"    - given {r.bindings} :: {r.expr}" for r in blocks.properties]
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

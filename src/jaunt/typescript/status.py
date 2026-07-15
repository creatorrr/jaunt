"""TypeScript freshness, check, specs, orphan, and clean operations."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jaunt.config import JauntConfig
from jaunt.errors import JauntConfigError
from jaunt.journal import JournalEvent, append_events
from jaunt.targets.base import (
    TargetArtifact,
    TargetCheckReport,
    TargetDiagnostic,
    TargetStatus,
    TargetWorkspace,
)
from jaunt.typescript.artifacts import incomplete_transaction_manifests
from jaunt.typescript.builder import (
    TypeScriptAnalysis,
    WorkerFactory,
    _diagnostic,
    _module_id,
    _module_path,
    _path_hash,
    _safe_path,
    _sha256,
    _target,
    analyze,
    validate_overlay,
    worker_session,
)
from jaunt.typescript.upgrade import compatible_semantic_modules

_PLACEHOLDER_MARKERS = ("state=unbuilt", 'state = "unbuilt"', "state: unbuilt")


@dataclass(frozen=True, slots=True)
class CleanReport:
    removed: tuple[str, ...] = ()
    would_remove: tuple[str, ...] = ()
    exit_code: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _read(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except (FileNotFoundError, UnicodeError):
        return None


def _sidecar(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return {str(key): item for key, item in parsed.items()} if isinstance(parsed, Mapping) else None


def _digest(value: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str):
            return item
    return None


def _invalid(code: str, message: str, path: str | None = None) -> tuple[TargetDiagnostic, ...]:
    return (TargetDiagnostic(code=code, message=message, path=path),)


def _module_freshness_digest(module: Mapping[str, Any]) -> str:
    """Hash the complete expected sidecar used for daemon supersession.

    ``structuralDigest`` deliberately ignores TSDoc prose.  A daemon proposal
    must be invalidated by prose, compiler/fingerprint, dependency-API, and
    route changes too, so its public status digest covers the entire current
    sidecar contract rather than structure alone.
    """

    sidecar = module.get("sidecar")
    if isinstance(sidecar, str):
        payload = sidecar
    elif isinstance(sidecar, Mapping):
        payload = json.dumps(sidecar, sort_keys=True, separators=(",", ":"), default=str)
    else:
        payload = json.dumps(
            {
                key: module.get(key)
                for key in (
                    "moduleId",
                    "structuralDigest",
                    "proseDigest",
                    "apiDigest",
                    "specPath",
                    "facadePath",
                    "apiMirrorPath",
                    "implementationPath",
                    "sidecarPath",
                    "project",
                    "packageOwner",
                    "dependencies",
                )
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return _sha256(payload.encode("utf-8"))


def classify_modules(
    root: Path,
    modules: Sequence[Mapping[str, Any]],
    *,
    orphans: Sequence[TargetArtifact] = (),
    diagnostics: Sequence[TargetDiagnostic] = (),
    metadata: Mapping[str, Any] | None = None,
) -> TargetStatus:
    """Compare committed artifacts with analyzer-owned IR and sidecars."""

    root = root.resolve()
    fresh: set[str] = set()
    stale: dict[str, str] = {}
    unbuilt: set[str] = set()
    invalid: dict[str, tuple[TargetDiagnostic, ...]] = {}
    digests: dict[str, str] = {}
    compatible_toolchain_modules = compatible_semantic_modules(root, tuple(modules))

    for module in modules:
        module_id = _module_id(module)
        digests[module_id] = _module_freshness_digest(module)
        implementation_rel = _module_path(module, "implementationPath")
        api_rel = _module_path(module, "apiMirrorPath")
        sidecar_rel = _module_path(module, "sidecarPath")
        facade_rel = _module_path(module, "facadePath")
        implementation = _read(_safe_path(root, implementation_rel))
        api = _read(_safe_path(root, api_rel))
        actual_sidecar_source = _read(_safe_path(root, sidecar_rel))

        if implementation is None or actual_sidecar_source is None:
            unbuilt.add(module_id)
            continue
        if any(marker in implementation for marker in _PLACEHOLDER_MARKERS):
            unbuilt.add(module_id)
            continue
        if not _safe_path(root, facade_rel).is_file():
            invalid[module_id] = _invalid(
                "JAUNT_TS_FACADE_MISSING",
                "The public facade is missing; run `jaunt sync`.",
                facade_rel,
            )
            continue
        expected_sidecar = _sidecar(module.get("sidecar"))
        actual_sidecar = _sidecar(actual_sidecar_source)
        if expected_sidecar is None or actual_sidecar is None:
            invalid[module_id] = _invalid(
                "JAUNT_TS_SIDECAR_INVALID",
                "The Jaunt TypeScript sidecar is malformed.",
                sidecar_rel,
            )
            continue

        state = actual_sidecar.get("state")
        if state == "unbuilt":
            unbuilt.add(module_id)
            continue
        if state != "built" or "jaunt:state=built" not in implementation:
            invalid[module_id] = _invalid(
                "JAUNT_TS_PROVENANCE_INVALID",
                "The generated implementation has invalid or missing build provenance.",
                implementation_rel,
            )
            continue
        provenance = {
            "moduleId": f"// jaunt:module={module_id}",
            "structuralDigest": f"// jaunt:structural={actual_sidecar.get('structuralDigest', '')}",
            "proseDigest": f"// jaunt:prose={actual_sidecar.get('proseDigest', '')}",
            "apiDigest": f"// jaunt:api={actual_sidecar.get('apiDigest', '')}",
        }
        missing_provenance = [
            key for key, marker in provenance.items() if marker not in implementation
        ]
        if missing_provenance:
            invalid[module_id] = _invalid(
                "JAUNT_TS_PROVENANCE_DRIFT",
                "The generated implementation header does not match its sidecar: "
                + ", ".join(missing_provenance),
                implementation_rel,
            )
            continue

        artifact_hashes = actual_sidecar.get("artifactHashes")
        if not isinstance(artifact_hashes, Mapping):
            invalid[module_id] = _invalid(
                "JAUNT_TS_SIDECAR_HASHES_MISSING",
                "The TypeScript sidecar does not contain artifact hashes; rebuild it.",
                sidecar_rel,
            )
            continue
        drifted: list[str] = []
        for relative, expected_hash in artifact_hashes.items():
            if not isinstance(relative, str) or not isinstance(expected_hash, str):
                drifted.append(str(relative))
                continue
            actual_hash = _path_hash(_safe_path(root, relative))
            normalized = (
                expected_hash if expected_hash.startswith("sha256:") else f"sha256:{expected_hash}"
            )
            if actual_hash != normalized:
                drifted.append(relative)
        if drifted:
            if implementation_rel in drifted:
                code = "JAUNT_TS_IMPLEMENTATION_DRIFT"
                message = "The generated TypeScript implementation was edited or corrupted."
            elif api_rel in drifted:
                code = "JAUNT_TS_API_DRIFT"
                message = "The deterministic API mirror was edited; it can be restored for free."
            elif facade_rel in drifted:
                code = "JAUNT_TS_FACADE_DRIFT"
                message = "The public facade changed and must be revalidated."
            else:
                code = "JAUNT_TS_ARTIFACT_DRIFT"
                message = "Generated TypeScript artifacts no longer match their sidecar."
            invalid[module_id] = _invalid(
                code,
                f"{message} Drifted paths: " + ", ".join(sorted(drifted)),
                sidecar_rel,
            )
            continue

        expected_structural = _digest(expected_sidecar, "structuralDigest", "structureDigest")
        actual_structural = _digest(actual_sidecar, "structuralDigest", "structureDigest")
        expected_prose = _digest(expected_sidecar, "proseDigest")
        actual_prose = _digest(actual_sidecar, "proseDigest")
        expected_api_digest = _digest(expected_sidecar, "apiDigest")
        actual_api_digest = _digest(actual_sidecar, "apiDigest")
        expected_environment = _digest(expected_sidecar, "semanticEnvironmentDigest")
        actual_environment = _digest(actual_sidecar, "semanticEnvironmentDigest")
        stale_reason: str | None = None
        compatible_toolchain_drift = module_id in compatible_toolchain_modules
        if expected_environment is not None and actual_environment is None:
            # Legacy sidecars cannot prove that their model-facing environment
            # is unchanged. Rebuild once instead of attempting an old-boundary
            # restamp that may fail or preserve an incompatible candidate.
            stale_reason = "structural"
        elif expected_structural != actual_structural:
            stale_reason = "toolchain" if compatible_toolchain_drift else "structural"
        elif expected_prose != actual_prose:
            stale_reason = "prose"
        elif expected_api_digest != actual_api_digest:
            # ``apiDigest`` covers both structure and prose. Check it only after
            # their individual digests so a docs-only edit reaches the semantic
            # gate instead of being misreported as structural drift.
            stale_reason = "toolchain" if compatible_toolchain_drift else "structural"
        elif expected_environment != actual_environment:
            stale_reason = "structural"
        elif any(
            expected_sidecar.get(key) != actual_sidecar.get(key)
            for key in (
                "schema",
                "fingerprint",
                "toolVersion",
                "workerVersion",
                "typescriptVersion",
                "compilerOptionsHash",
                "promptDigest",
            )
            if key in expected_sidecar
        ):
            stale_reason = "toolchain" if compatible_toolchain_drift else "fingerprint"
        if stale_reason is not None:
            stale[module_id] = stale_reason
            continue

        identity_keys = (
            "moduleId",
            "specPath",
            "facadePath",
            "apiMirrorPath",
            "implementationPath",
            "project",
            "packageOwner",
            "dependencies",
        )
        if any(
            expected_sidecar.get(key) != actual_sidecar.get(key)
            for key in identity_keys
            if key in expected_sidecar
        ):
            invalid[module_id] = _invalid(
                "JAUNT_TS_SIDECAR_IDENTITY_DRIFT",
                "The TypeScript sidecar no longer belongs to the analyzed module route.",
                sidecar_rel,
            )
            continue
        expected_api = module.get("apiSource")
        if not isinstance(expected_api, str) or api != expected_api:
            invalid[module_id] = _invalid(
                "JAUNT_TS_API_DRIFT",
                "The deterministic API mirror is missing or edited; run `jaunt sync`.",
                api_rel,
            )
            continue
        fresh.add(module_id)

    transaction_diagnostics = list(diagnostics)
    for manifest in incomplete_transaction_manifests(root):
        transaction_diagnostics.append(
            TargetDiagnostic(
                code="JAUNT_TS_INCOMPLETE_TRANSACTION",
                message="A previous TypeScript artifact transaction did not finish.",
                path=manifest.relative_to(root).as_posix(),
            )
        )
    return TargetStatus(
        language="ts",
        root=root,
        fresh=frozenset(fresh),
        stale=stale,
        unbuilt=frozenset(unbuilt),
        invalid=invalid,
        digests=digests,
        orphans=tuple(orphans),
        diagnostics=tuple(transaction_diagnostics),
        metadata=dict(metadata or {}),
    )


def _orphans(root: Path, result: Mapping[str, Any]) -> tuple[TargetArtifact, ...]:
    raw = result.get("artifacts", [])
    if not isinstance(raw, list):
        return ()
    artifacts: list[TargetArtifact] = []
    for item in raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            continue
        artifacts.append(
            TargetArtifact(
                path=_safe_path(root, str(item["path"])),
                kind=str(item.get("kind", "unknown")),
                module_id=str(item["moduleId"]) if isinstance(item.get("moduleId"), str) else None,
            )
        )
    return tuple(sorted(artifacts, key=lambda artifact: artifact.path.as_posix()))


def _battery_artifacts(
    root: Path,
    config: JauntConfig,
    analysis: TypeScriptAnalysis,
    *,
    target_ids: Sequence[str] = (),
) -> tuple[TargetArtifact, ...]:
    """Find provenance-owned generated tests and contract batteries.

    The worker owns implementation-side orphan discovery. Batteries live under
    configurable test roots, so Python compares their provenance paths with the
    current test-spec and contract records. A targeted command deliberately skips
    orphan discovery: once the source has disappeared there is no sound target ID
    with which to attribute it, and deleting an unrelated artifact would be worse
    than leaving it for an unscoped ``clean --orphans``.
    """

    if target_ids:
        return ()
    target = config.typescript_target
    if target is None:
        return ()

    from jaunt.typescript.contracts import _battery_path
    from jaunt.typescript.tester import _implicit_class_test_specs, _test_output

    expected: dict[Path, TargetArtifact] = {}
    raw_test_specs = analysis.workspace.get("testSpecs", [])
    test_specs = (
        [item for item in raw_test_specs if isinstance(item, Mapping)]
        if isinstance(raw_test_specs, list)
        else []
    )
    modules = {_module_id(module): module for module in analysis.modules}
    test_specs.extend(
        _implicit_class_test_specs(
            root,
            config,
            modules,
            explicit_specs=test_specs,
        )
    )
    for item in test_specs:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            continue
        source = str(item["path"])
        for tier in ("example", "derived"):
            relative = _test_output(source, target.generated_dir, tier)
            path = _safe_path(root, relative)
            expected[path] = TargetArtifact(
                path=path,
                kind="generated-test",
                module_id=f"ts-test:{source}#{tier}",
            )

    raw_contracts = analysis.workspace.get("contracts", [])
    if isinstance(raw_contracts, list):
        for item in raw_contracts:
            if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
                continue
            source_relative = str(item["path"])
            source = _safe_path(root, source_relative)
            symbols = item.get("symbols", [])
            if not isinstance(symbols, list):
                continue
            for raw_symbol in symbols:
                symbol = (
                    str(raw_symbol.get("name"))
                    if isinstance(raw_symbol, Mapping)
                    else str(raw_symbol)
                )
                path = _battery_path(root, config, source, symbol)
                expected[path] = TargetArtifact(
                    path=path,
                    kind="contract-battery",
                    module_id=f"ts-contract:{source_relative}#{symbol}",
                )

    candidates: set[Path] = set()
    for entry in target.test_roots:
        roots = (
            [path for path in root.glob(entry) if path.is_dir()]
            if any(character in entry for character in "*?[")
            else [_safe_path(root, entry)]
        )
        for test_root in roots:
            generated_root_name = target.generated_dir.strip("/")
            if not test_root.is_dir():
                continue
            for generated_root in test_root.rglob(generated_root_name):
                if not generated_root.is_dir():
                    continue
                candidates.update(generated_root.glob("*.test.ts"))
                candidates.update(generated_root.glob("*.test.tsx"))

    battery_root = _safe_path(root, target.contract_battery_dir)
    if battery_root.is_dir():
        candidates.update(battery_root.rglob("*.contract.test.ts"))
        candidates.update(battery_root.rglob("*.contract.test.tsx"))

    artifacts: list[TargetArtifact] = []
    for candidate in sorted(candidates, key=lambda path: path.as_posix()):
        safe = _safe_path(root, candidate.relative_to(root).as_posix())
        source = _read(safe)
        if source is None:
            continue
        is_contract = source.startswith("// ⚙️ jaunt:contract-battery")
        is_generated_test = source.startswith("// ⚙️ jaunt:generated")
        if not is_contract and not is_generated_test:
            continue
        owned = expected.get(safe)
        if owned is not None:
            continue
        source_match = re.search(r"(?m)^// jaunt:source=([^\r\n]+)$", source)
        source_path = source_match.group(1) if source_match else "unknown"
        kind = "contract-battery" if is_contract else "generated-test"
        artifacts.append(
            TargetArtifact(
                path=safe,
                kind=kind,
                module_id=(
                    f"ts-contract:{source_path}" if is_contract else f"ts-test:{source_path}"
                ),
            )
        )
    return tuple(artifacts)


def _workspace_diagnostics(analysis: TypeScriptAnalysis) -> tuple[TargetDiagnostic, ...]:
    raw = analysis.workspace.get("diagnostics", [])
    if not isinstance(raw, list):
        return ()
    return tuple(_diagnostic(item) for item in raw if isinstance(item, Mapping))


async def run_status(
    root: Path,
    config: JauntConfig,
    *,
    target_ids: Sequence[str] = (),
    worker_factory: WorkerFactory | None = None,
) -> TargetStatus:
    root = root.resolve()
    async with worker_session(root, config, worker_factory=worker_factory) as (client, initialized):
        analysis = await analyze(client, initialized, target_ids=target_ids)
        from jaunt.typescript.tester import _test_battery_diagnostics

        module_ids = tuple(_module_id(module) for module in analysis.modules)
        validation_diagnostics: tuple[TargetDiagnostic, ...] = ()
        if module_ids:
            validated = await validate_overlay(
                client,
                analysis,
                {},
                module_ids,
                sync_module_ids=module_ids,
                scoped_validation=bool(target_ids),
            )
            validation_diagnostics = tuple(
                _diagnostic(diagnostic) for diagnostic in validated.diagnostics
            )
        params: dict[str, Any] = {}
        if target_ids:
            params["moduleIds"] = list(target_ids)
        orphan_result = await client.request("findOrphans", params)
        battery_orphans = _battery_artifacts(
            root,
            config,
            analysis,
            target_ids=target_ids,
        )
        battery_diagnostics = _test_battery_diagnostics(
            root,
            config,
            analysis.workspace,
            {_module_id(module): module for module in analysis.modules},
            client,
            initialized,
            target_ids=target_ids,
        )
    npm_skill_metadata: Mapping[str, Any] = {}
    target = config.typescript_target
    if target is not None and target.auto_skills_enabled(bool(config.skills.auto)):
        from jaunt.skills_npm import plan_npm_skills, typescript_package_owners

        npm_skill_metadata = plan_npm_skills(
            project_root=root,
            package_owners=typescript_package_owners(root, target),
            max_readme_chars=config.skills.max_chars_per_skill,
        ).metadata()
    elif target is not None:
        npm_skill_metadata = {"enabled": False}

    return classify_modules(
        root,
        analysis.modules,
        orphans=tuple(
            sorted(
                (*_orphans(root, orphan_result), *battery_orphans),
                key=lambda artifact: artifact.path.as_posix(),
            )
        ),
        diagnostics=(
            *_workspace_diagnostics(analysis),
            *validation_diagnostics,
            *battery_diagnostics,
        ),
        metadata={"npm_skills": npm_skill_metadata},
    )


async def run_check(
    root: Path,
    config: JauntConfig,
    *,
    target_ids: Sequence[str] = (),
    magic_only: bool = False,
    contracts_only: bool = False,
    worker_factory: WorkerFactory | None = None,
) -> TargetCheckReport:
    if magic_only and contracts_only:
        raise JauntConfigError("--magic-only and --contracts-only are mutually exclusive")
    status = await run_status(
        root,
        config,
        target_ids=() if contracts_only else target_ids,
        worker_factory=worker_factory,
    )
    checked: list[Mapping[str, Any]] = []
    blocked: list[Mapping[str, Any]] = []
    magic_runner_diagnostics: tuple[TargetDiagnostic, ...] = ()
    if not contracts_only:
        from jaunt.typescript.tester import (
            _run_test_batches,
            _selected_test_specs,
            _test_output,
            _workspace_test_file_owners,
        )

        async with worker_session(root, config, worker_factory=worker_factory) as (
            client,
            initialized,
        ):
            analysis = await analyze(client, initialized)
            modules = {_module_id(module): module for module in analysis.modules}
            test_specs = _selected_test_specs(
                root,
                config,
                analysis.workspace,
                modules,
                target_ids=target_ids,
            )
            generated_dir = _target(config).generated_dir
            battery_files = tuple(
                sorted(
                    relative
                    for spec in test_specs
                    for tier in ("example", "derived")
                    if (relative := _test_output(str(spec.get("path", "")), generated_dir, tier))
                    and _safe_path(root, relative).is_file()
                )
            )
            if battery_files:
                typed = await _run_test_batches(
                    client,
                    root,
                    config,
                    analysis.workspace,
                    files=battery_files,
                    explicit_owners=_workspace_test_file_owners(root, config, analysis.workspace),
                    typecheck_only=True,
                )
                if not bool(typed.get("ok", False)):
                    magic_runner_diagnostics = (
                        TargetDiagnostic(
                            code="JAUNT_TS_TEST_TYPECHECK",
                            message=(
                                "Committed generated TypeScript tests failed policy-aware "
                                "typechecking; run `jaunt test --language ts`."
                            ),
                            data={"scope": "magic", "runner": typed},
                        ),
                    )
    if not magic_only:
        from jaunt.typescript.contracts import (
            _MUTATION_SCHEME,
            _battery_body_digest_issue,
            _battery_header_metadata,
            _battery_path,
            _parse_strength_metadata,
        )
        from jaunt.typescript.properties import PROPERTY_RENDERER_SCHEME
        from jaunt.typescript.tester import (
            _run_test_batches,
            _workspace_test_file_owners,
        )

        async with worker_session(root, config, worker_factory=worker_factory) as (
            client,
            initialized,
        ):
            analysis = await analyze(client, initialized)
            contracts = analysis.workspace.get("contracts", [])
            battery_files: list[str] = []
            if isinstance(contracts, list):
                for contract in contracts:
                    if not isinstance(contract, Mapping):
                        continue
                    source_value = contract.get("path")
                    symbols = contract.get("symbols", [])
                    if not isinstance(source_value, str) or not isinstance(symbols, list):
                        blocked.append({"reason": "malformed-contract", **dict(contract)})
                        continue
                    source = _safe_path(root, source_value)
                    if not source.is_file():
                        blocked.append({"reason": "missing-source", **dict(contract)})
                        continue
                    source_digest = _sha256(source.read_bytes())
                    for symbol_value in symbols:
                        symbol = (
                            str(symbol_value.get("name"))
                            if isinstance(symbol_value, Mapping)
                            else str(symbol_value)
                        )
                        contract_id = f"{source_value}#{symbol}"
                        qualified_id = (
                            f"ts:{Path(source_value).with_suffix('').as_posix()}#{symbol}"
                        )
                        if target_ids and not {contract_id, qualified_id}.intersection(target_ids):
                            continue
                        battery = _battery_path(root, config, source, symbol)
                        record = {
                            "target": contract_id,
                            "source": source_value,
                            "battery": battery.relative_to(root).as_posix(),
                        }
                        if not battery.is_file():
                            blocked.append({"reason": "missing-battery", **record})
                            continue
                        battery_source = battery.read_text(encoding="utf-8")
                        metadata = _battery_header_metadata(battery_source)
                        if metadata is None:
                            blocked.append({"reason": "malformed-provenance", **record})
                            continue
                        if metadata.get("source") != source_value:
                            blocked.append({"reason": "wrong-source", **record})
                            continue
                        if metadata.get("source_digest") != source_digest:
                            blocked.append({"reason": "stale-battery", **record})
                            continue
                        if (
                            metadata.get("property_scheme") != PROPERTY_RENDERER_SCHEME
                            or re.fullmatch(
                                r"sha256:[0-9a-f]{64}",
                                metadata.get("property_digest", ""),
                            )
                            is None
                        ):
                            blocked.append(
                                {
                                    "reason": "stale-property-renderer",
                                    "expected": PROPERTY_RENDERER_SCHEME,
                                    **record,
                                }
                            )
                            continue
                        body_digest_issue = _battery_body_digest_issue(battery_source)
                        if body_digest_issue is not None:
                            blocked.append(
                                {
                                    "reason": body_digest_issue,
                                    "guidance": (
                                        "The committed TypeScript contract battery body is not "
                                        "covered by valid provenance. Run "
                                        "`jaunt reconcile --language ts` to regenerate it."
                                    ),
                                    **record,
                                }
                            )
                            continue
                        if config.contract.strength:
                            strength = _parse_strength_metadata(battery_source)
                            if strength is None:
                                blocked.append({"reason": "missing-strength", **record})
                                continue
                            if strength.get("scheme") != _MUTATION_SCHEME:
                                blocked.append(
                                    {
                                        "reason": "stale-strength-scheme",
                                        "expected": _MUTATION_SCHEME,
                                        **record,
                                    }
                                )
                                continue
                            surviving_count = strength.get("survived", 0)
                            if isinstance(surviving_count, int) and surviving_count:
                                blocked.append(
                                    {"reason": "surviving-mutants", "strength": strength, **record}
                                )
                                continue
                            record["strength"] = strength
                        checked.append(record)
                        battery_files.append(battery.relative_to(root).as_posix())
            if battery_files:
                owners = _workspace_test_file_owners(root, config, analysis.workspace)
                typed = await _run_test_batches(
                    client,
                    root,
                    config,
                    analysis.workspace,
                    files=tuple(battery_files),
                    explicit_owners=owners,
                    typecheck_only=True,
                )
                if not bool(typed.get("ok", False)):
                    blocked.append({"reason": "typecheck-failed", "runner": typed})
                else:
                    run = await _run_test_batches(
                        client,
                        root,
                        config,
                        analysis.workspace,
                        files=tuple(battery_files),
                        explicit_owners=owners,
                    )
                    if not bool(run.get("ok", False)):
                        blocked.append({"reason": "contract-failed", "runner": run})
    error_diagnostics = tuple(
        diagnostic
        for diagnostic in (*status.diagnostics, *magic_runner_diagnostics)
        if diagnostic.severity == "error"
    )
    magic_blocked = bool(
        status.stale
        or status.unbuilt
        or status.invalid
        or any(orphan.kind != "contract-battery" for orphan in status.orphans)
        or error_diagnostics
    )
    contract_orphans = tuple(
        orphan for orphan in status.orphans if orphan.kind == "contract-battery"
    )
    contracts_blocked = bool(blocked or contract_orphans)
    reported_diagnostics = tuple(
        diagnostic
        for diagnostic in (*status.diagnostics, *magic_runner_diagnostics)
        if not contracts_only or diagnostic.data.get("scope") != "magic"
    )
    exit_code = (
        4 if (not contracts_only and magic_blocked) or (not magic_only and contracts_blocked) else 0
    )
    return TargetCheckReport(
        language="ts",
        root=root,
        fresh=status.fresh if not contracts_only else frozenset(),
        stale=status.stale if not contracts_only else {},
        unbuilt=status.unbuilt if not contracts_only else frozenset(),
        invalid=status.invalid if not contracts_only else {},
        orphans=tuple(
            orphan
            for orphan in status.orphans
            if (not contracts_only or orphan.kind == "contract-battery")
            and (not magic_only or orphan.kind != "contract-battery")
        ),
        checked=tuple(checked),
        blocked=tuple(blocked),
        diagnostics=reported_diagnostics,
        exit_code=exit_code,
    )


def _spec_dependency_graph(
    modules: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Project worker symbol dependencies into the public ``specs`` payload."""

    dependency_graph: dict[str, list[str]] = {}
    for module in modules:
        raw_symbols = module.get("symbols", [])
        if not isinstance(raw_symbols, list):
            continue
        for raw_symbol in raw_symbols:
            if not isinstance(raw_symbol, Mapping):
                continue
            symbol_id = raw_symbol.get("id")
            if not isinstance(symbol_id, str) or not symbol_id:
                continue
            dependencies: set[str] = set()
            options = raw_symbol.get("options")
            if isinstance(options, Mapping):
                raw_dependencies = options.get("deps", [])
                if isinstance(raw_dependencies, list):
                    dependencies.update(
                        item for item in raw_dependencies if isinstance(item, str) and item
                    )
            heritage = raw_symbol.get("heritage")
            if isinstance(heritage, Mapping):
                base_id = heritage.get("resolvedBaseId")
                if isinstance(base_id, str) and base_id:
                    dependencies.add(base_id)
            dependency_graph[symbol_id] = sorted(dependencies)
    return dict(sorted(dependency_graph.items()))


async def run_specs(
    root: Path,
    config: JauntConfig,
    *,
    target_ids: Sequence[str] = (),
    worker_factory: WorkerFactory | None = None,
) -> TargetWorkspace:
    root = root.resolve()
    async with worker_session(root, config, worker_factory=worker_factory) as (client, initialized):
        analysis = await analyze(client, initialized, target_ids=target_ids)
    projects_raw = analysis.workspace.get("projects", [])
    routes_raw = analysis.workspace.get("routes", [])
    specs_raw = analysis.workspace.get("specs", [])
    projects = (
        tuple(
            str(item.get("id", item.get("configPath")))
            for item in projects_raw
            if isinstance(item, Mapping)
        )
        if isinstance(projects_raw, list)
        else ()
    )
    owners = (
        tuple(
            sorted(
                {
                    str(item.get("packageOwner"))
                    for item in routes_raw
                    if isinstance(item, Mapping) and item.get("packageOwner") is not None
                }
            )
        )
        if isinstance(routes_raw, list)
        else ()
    )
    dependency_graph = _spec_dependency_graph(analysis.modules)
    return TargetWorkspace(
        language="ts",
        module_ids=tuple(_module_id(module) for module in analysis.modules),
        owners=owners,
        projects=projects,
        metadata={
            "specs": tuple(dict(item) for item in specs_raw if isinstance(item, Mapping))
            if isinstance(specs_raw, list)
            else (),
            "routes": tuple(dict(item) for item in routes_raw if isinstance(item, Mapping))
            if isinstance(routes_raw, list)
            else (),
            "dependency_graph": dict(sorted(dependency_graph.items())),
            "worker_version": initialized.worker_version,
            "typescript_version": initialized.typescript_version,
        },
    )


def _owned_artifacts(
    root: Path,
    analysis: TypeScriptAnalysis,
    *,
    target_ids: Sequence[str] = (),
) -> tuple[TargetArtifact, ...]:
    artifacts: list[TargetArtifact] = []
    selected = {target.split("#", 1)[0] for target in target_ids}
    for module in analysis.modules:
        module_id = _module_id(module)
        if selected and module_id not in selected:
            continue
        for key, kind in (
            ("apiMirrorPath", "api-mirror"),
            ("implementationPath", "implementation"),
            ("sidecarPath", "sidecar"),
        ):
            path = _safe_path(root, _module_path(module, key))
            if path.exists():
                artifacts.append(TargetArtifact(path=path, kind=kind, module_id=module_id))
    return tuple(artifacts)


def _test_spec_selected(item: Mapping[str, Any], target_ids: Sequence[str]) -> bool:
    if not target_ids:
        return True
    raw_targets = item.get("targets", [])
    targets = (
        {target for target in raw_targets if isinstance(target, str)}
        if isinstance(raw_targets, list)
        else set()
    )
    for selected in target_ids:
        if "#" in selected:
            if selected in targets:
                return True
        elif any(target.split("#", 1)[0] == selected for target in targets):
            return True
    return False


def _owned_magic_batteries(
    root: Path,
    config: JauntConfig,
    analysis: TypeScriptAnalysis,
    *,
    target_ids: Sequence[str] = (),
) -> tuple[TargetArtifact, ...]:
    """Return only current provenance-owned example/derived Vitest batteries."""

    target = config.typescript_target
    if target is None:
        return ()
    from jaunt.typescript.tester import _implicit_class_test_specs, _test_output

    raw_test_specs = analysis.workspace.get("testSpecs", [])
    explicit_specs = (
        [item for item in raw_test_specs if isinstance(item, Mapping)]
        if isinstance(raw_test_specs, list)
        else []
    )
    modules = {_module_id(module): module for module in analysis.modules}
    test_specs = [
        *explicit_specs,
        *_implicit_class_test_specs(
            root,
            config,
            modules,
            explicit_specs=explicit_specs,
        ),
    ]
    artifacts: list[TargetArtifact] = []
    for item in test_specs:
        source = item.get("path")
        if not isinstance(source, str) or not _test_spec_selected(item, target_ids):
            continue
        for tier in ("example", "derived"):
            path = _safe_path(root, _test_output(source, target.generated_dir, tier))
            content = _read(path)
            if content is None or not content.startswith("// ⚙️ jaunt:generated"):
                continue
            artifacts.append(
                TargetArtifact(
                    path=path,
                    kind="generated-test",
                    module_id=f"ts-test:{source}#{tier}",
                )
            )
    return tuple(artifacts)


async def run_clean(
    root: Path,
    config: JauntConfig,
    *,
    target_ids: Sequence[str] = (),
    orphans_only: bool = False,
    dry_run: bool = False,
    worker_factory: WorkerFactory | None = None,
) -> CleanReport:
    root = root.resolve()
    async with worker_session(root, config, worker_factory=worker_factory) as (client, initialized):
        analysis = await analyze(client, initialized, target_ids=target_ids)
        params: dict[str, Any] = {}
        if target_ids:
            params["moduleIds"] = list(target_ids)
        orphan_result = await client.request("findOrphans", params)
        battery_artifacts = _battery_artifacts(
            root,
            config,
            analysis,
            target_ids=target_ids,
        )
    worker_orphans = _orphans(root, orphan_result)
    if orphans_only:
        candidates = tuple((*worker_orphans, *battery_artifacts))
    else:
        # Ordinary clean resets magic-mode output only. Contract batteries are
        # committed lifecycle artifacts and remain until an explicit orphan clean
        # or contract operation removes them.
        magic_battery_orphans = tuple(
            artifact for artifact in battery_artifacts if artifact.kind == "generated-test"
        )
        candidates = tuple(
            {
                artifact.path: artifact
                for artifact in (
                    *worker_orphans,
                    *magic_battery_orphans,
                    *_owned_artifacts(root, analysis, target_ids=target_ids),
                    *_owned_magic_batteries(
                        root,
                        config,
                        analysis,
                        target_ids=target_ids,
                    ),
                )
            }.values()
        )
    relative = tuple(sorted(artifact.path.relative_to(root).as_posix() for artifact in candidates))
    if dry_run:
        return CleanReport(would_remove=relative)
    for artifact in candidates:
        artifact.path.unlink(missing_ok=True)
    append_events(root, [JournalEvent("clean", "ts", path) for path in relative])
    return CleanReport(removed=relative)


__all__ = [
    "CleanReport",
    "classify_modules",
    "run_check",
    "run_clean",
    "run_specs",
    "run_status",
]

"""Model-free config-v2 and worker-validated TypeScript artifact migrations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jaunt.config import JauntConfig, load_config
from jaunt.errors import JauntConfigError

_ARTIFACT_MIGRATION_ID = "typescript-artifacts-v1"
_PRIVATE_SPEC_RE = re.compile(r"\.jaunt\.(?:ts|tsx)$")

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_PY_BUILD_KEYS = (
    "infer_deps",
    "ty_retry_attempts",
    "async_runner",
    "check_generated_imports",
    "generated_import_allowlist",
    "emit_stubs",
)
_SHARED_BUILD_KEYS = ("jobs", "include_target_tests", "instructions")
_PY_TEST_KEYS = ("infer_deps", "pytest_args", "auto_class_tests")
_SHARED_TEST_KEYS = ("jobs",)


@dataclass(frozen=True, slots=True)
class ConfigV2Migration:
    """A validated, byte-addressed config migration proposal."""

    path: Path
    before_sha256: str
    source: str
    changed: bool

    def to_json(self, root: Path) -> dict[str, object]:
        try:
            path = self.path.relative_to(root.resolve()).as_posix()
        except ValueError:
            path = str(self.path)
        return {
            "migration": "config-v2",
            "path": path,
            "changed": self.changed,
            "before_sha256": self.before_sha256,
            "after_sha256": _sha256(self.source.encode("utf-8")),
        }


@dataclass(frozen=True, slots=True)
class TypeScriptMigrationAction:
    """One deterministic write or explicit non-writing migration decision."""

    module_id: str
    path: str
    kind: str
    classification: str
    description: str

    def to_json(self) -> dict[str, str]:
        return {
            "migration": _ARTIFACT_MIGRATION_ID,
            "module_id": self.module_id,
            "path": self.path,
            "kind": self.kind,
            "classification": self.classification,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class TypeScriptMigrationDiagnostic:
    code: str
    message: str
    classification: str
    module_id: str | None = None
    path: str | None = None
    severity: str = "warning"

    def to_json(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "classification": self.classification,
            "severity": self.severity,
            **({"module_id": self.module_id} if self.module_id else {}),
            **({"path": self.path} if self.path else {}),
        }


@dataclass(frozen=True, slots=True)
class TypeScriptMigrationPlan:
    """Worker-validated, byte-addressed TypeScript artifact migration plan."""

    root: Path
    actions: tuple[TypeScriptMigrationAction, ...]
    diagnostics: tuple[TypeScriptMigrationDiagnostic, ...]
    expected_inputs: Mapping[str, str]
    writes: tuple[Any, ...]
    plan_digest: str

    @property
    def blocked(self) -> bool:
        return any(item.classification == "manual-intervention" for item in self.diagnostics)

    @property
    def requires_rebuild(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    item.module_id
                    for item in self.diagnostics
                    if item.classification == "model-rebuild" and item.module_id
                }
            )
        )

    def to_json(
        self, *, applied: bool = False, applied_paths: Sequence[str] = ()
    ) -> dict[str, object]:
        return {
            "schema_version": 2,
            "command": "migrate",
            # A blocked plan is still a successful, non-mutating analysis. The
            # CLI reports ``ok: false`` only when an attempted apply is refused.
            "ok": True,
            "language": "ts",
            "applied": applied,
            "plan_digest": self.plan_digest,
            "blocked": self.blocked,
            "actions": [action.to_json() for action in self.actions],
            "diagnostics": [diagnostic.to_json() for diagnostic in self.diagnostics],
            "requires_rebuild": list(self.requires_rebuild),
            **({"applied_paths": list(applied_paths)} if applied else {}),
        }


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _table(value: object) -> dict[str, Any]:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def _selected(table: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: table[key] for key in keys if key in table}


def _v2_data(v1: dict[str, Any]) -> dict[str, Any]:
    paths = _table(v1.get("paths"))
    build = _table(v1.get("build"))
    test = _table(v1.get("test"))
    prompts = _table(v1.get("prompts"))
    contract = _table(v1.get("contract"))

    python_target = dict(paths)
    python_target.update(_selected(build, _PY_BUILD_KEYS))
    python_test = _selected(test, _PY_TEST_KEYS)
    if "infer_deps" in python_test:
        python_test["test_infer_deps"] = python_test.pop("infer_deps")
    python_target.update(python_test)
    if "battery_dir" in contract:
        python_target["contract_battery_dir"] = contract["battery_dir"]

    migrated: dict[str, Any] = {"version": 2, "target": {"py": python_target}}
    for key in (
        "llm",
        "agent",
        "codex",
        "daemon",
        "skills",
        "semantic_gate",
        "context",
    ):
        if key in v1:
            migrated[key] = v1[key]

    shared_build = _selected(build, _SHARED_BUILD_KEYS)
    if shared_build:
        migrated["build"] = shared_build
    shared_test = _selected(test, _SHARED_TEST_KEYS)
    if shared_test:
        migrated["test"] = shared_test
    if prompts:
        migrated["prompts"] = {"py": prompts}
    shared_contract = {key: value for key, value in contract.items() if key != "battery_dir"}
    if shared_contract:
        migrated["contract"] = shared_contract
    return migrated


def _key(value: str) -> str:
    return value if _BARE_KEY.fullmatch(value) else json.dumps(value, ensure_ascii=False)


def _value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JauntConfigError("Cannot migrate non-finite TOML numbers")
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise JauntConfigError(f"Cannot migrate unsupported TOML value: {type(value).__name__}")


def _ordered_items(table: dict[str, Any]) -> tuple[list[tuple[str, object]], list[tuple[str, Any]]]:
    scalars: list[tuple[str, object]] = []
    children: list[tuple[str, Any]] = []
    for key, value in table.items():
        if isinstance(value, dict):
            children.append((key, value))
        else:
            scalars.append((key, value))
    return scalars, children


def _render_table(
    lines: list[str], table: dict[str, Any], path: tuple[str, ...], *, heading: bool
) -> None:
    scalars, children = _ordered_items(table)
    if heading:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("[" + ".".join(_key(part) for part in path) + "]")
    for key, value in scalars:
        lines.append(f"{_key(key)} = {_value(value)}")
    for key, child in children:
        _render_table(lines, child, (*path, key), heading=True)


def render_toml(data: dict[str, Any]) -> str:
    """Render the validated Jaunt subset of TOML in deterministic insertion order."""

    lines: list[str] = []
    _render_table(lines, data, (), heading=False)
    return "\n".join(lines).rstrip() + "\n"


def _python_views_equal(before: JauntConfig, after: JauntConfig) -> bool:
    return all(
        getattr(before, field) == getattr(after, field)
        for field in (
            "paths",
            "llm",
            "build",
            "test",
            "prompts",
            "agent",
            "codex",
            "daemon",
            "skills",
            "contract",
            "context",
            "semantic_gate",
        )
    )


def plan_config_v2(root: Path, config_path: Path | None = None) -> ConfigV2Migration:
    """Return a validated migration without changing the project."""

    root = root.resolve()
    path = (config_path or root / "jaunt.toml").resolve()
    try:
        before_bytes = path.read_bytes()
    except OSError as exc:
        raise JauntConfigError(f"Failed reading config file: {path}") from exc
    try:
        raw = tomllib.loads(before_bytes.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise JauntConfigError(f"Invalid TOML in {path}: {exc}") from exc
    version = raw.get("version")
    if version == 2:
        source = before_bytes.decode("utf-8")
        return ConfigV2Migration(path, _sha256(before_bytes), source, False)
    if version != 1:
        raise JauntConfigError("`jaunt migrate --config-v2` requires config version 1 or 2")

    before = load_config(root=root, config_path=path)
    source = render_toml(_v2_data(raw))

    # Parse through the public loader from the same root so relative prompt paths
    # receive exactly the same resolution they will have after the atomic replace.
    fd, temp_name = tempfile.mkstemp(prefix=".jaunt-v2-", suffix=".toml", dir=root)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        after = load_config(root=root, config_path=temp_path)
    finally:
        temp_path.unlink(missing_ok=True)
    if not _python_views_equal(before, after):
        raise JauntConfigError(
            "Refusing config-v2 migration because the Python compatibility view changed"
        )
    return ConfigV2Migration(path, _sha256(before_bytes), source, source.encode() != before_bytes)


def apply_config_v2(plan: ConfigV2Migration) -> bool:
    """Atomically apply ``plan`` if the config still matches its input digest."""

    try:
        current = plan.path.read_bytes()
    except OSError as exc:
        raise JauntConfigError(f"Failed reading config file: {plan.path}") from exc
    if _sha256(current) != plan.before_sha256:
        raise JauntConfigError("jaunt.toml changed after the config-v2 plan; no write was made")
    if not plan.changed:
        return False
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(plan.path.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(prefix=".jaunt-config-", dir=plan.path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(plan.source)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, plan.path)
    finally:
        temp_path.unlink(missing_ok=True)
    return True


def _json_object(source: object) -> dict[str, Any] | None:
    if isinstance(source, Mapping):
        return {str(key): value for key, value in source.items()}
    if not isinstance(source, str):
        return None
    try:
        value = json.loads(source)
    except json.JSONDecodeError:
        return None
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else None


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _legacy_layout_plan(
    root: Path, config: JauntConfig
) -> tuple[
    tuple[TypeScriptMigrationAction, ...],
    tuple[TypeScriptMigrationDiagnostic, ...],
    dict[str, str],
]:
    """Detect preview layouts without trying to infer their intended public route."""

    from jaunt.typescript.builder import _path_hash, _safe_path

    target = config.typescript_target
    if target is None:
        raise JauntConfigError("TypeScript migration requires [target.ts]")
    roots: list[Path] = []
    for entry in target.source_roots:
        if any(character in entry for character in "*?["):
            roots.extend(path for path in sorted(root.glob(entry)) if path.is_dir())
        else:
            candidate = _safe_path(root, entry)
            if candidate.is_dir():
                roots.append(candidate)
    ambiguous: dict[Path, str] = {}
    for source_root in roots:
        specs = (*source_root.rglob("*.jaunt.ts"), *source_root.rglob("*.jaunt.tsx"))
        for spec in specs:
            stem = _PRIVATE_SPEC_RE.sub("", spec.name)
            old_impl = spec.parent / target.generated_dir / "impl.ts"
            sibling_index = spec.parent / "index.ts"
            signals: list[str] = []
            if stem == "spec" and sibling_index.is_file():
                signals.append("a sibling index.ts facade")
            if old_impl.is_file():
                signals.append(f"{target.generated_dir}/impl.ts")
            if signals:
                ambiguous[spec.resolve()] = ", ".join(signals)
        for register in source_root.rglob("register.mjs"):
            if register.parent.name == "jaunt":
                ambiguous[register.resolve()] = "the preview register.mjs loader"
    actions: list[TypeScriptMigrationAction] = []
    diagnostics: list[TypeScriptMigrationDiagnostic] = []
    expected: dict[str, str] = {}
    for path, reason in sorted(ambiguous.items(), key=lambda item: item[0].as_posix()):
        relative = _relative(root, path)
        digest = _path_hash(path)
        if digest is not None:
            expected[relative] = digest
        description = (
            f"Manual migration required for {relative}: detected {reason}; "
            "Jaunt will not guess a private-spec or facade rewrite."
        )
        actions.append(
            TypeScriptMigrationAction(
                module_id=f"ts-legacy:{relative}",
                path=relative,
                kind="legacy-layout",
                classification="manual-intervention",
                description=description,
            )
        )
        diagnostics.append(
            TypeScriptMigrationDiagnostic(
                code="JAUNT_TS_MIGRATE_LAYOUT_AMBIGUOUS",
                message=description,
                classification="manual-intervention",
                path=relative,
                severity="error",
            )
        )
    return tuple(actions), tuple(diagnostics), expected


def _scheme_value(sidecar: Mapping[str, Any], name: str) -> object:
    fingerprint = sidecar.get("fingerprint")
    if isinstance(fingerprint, Mapping) and name in fingerprint:
        return fingerprint.get(name)
    return sidecar.get(name)


def _built_migration_issue(
    root: Path,
    module: Mapping[str, Any],
    *,
    semantic_compatible: bool = False,
) -> TypeScriptMigrationDiagnostic | None:
    """Return a no-write rebuild decision when built TypeScript artifacts are incompatible."""

    from jaunt.typescript.builder import _module_id, _module_path, _safe_path

    module_id = _module_id(module)
    implementation_path = _module_path(module, "implementationPath")
    implementation = _safe_path(root, implementation_path)
    try:
        implementation_source = implementation.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeError):
        return None
    if "// jaunt:state=built" not in implementation_source:
        return None
    sidecar_path = _module_path(module, "sidecarPath")
    try:
        actual_source = _safe_path(root, sidecar_path).read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeError):
        actual_source = ""
    actual = _json_object(actual_source)
    expected = _json_object(module.get("sidecar"))
    if actual is None or expected is None:
        return TypeScriptMigrationDiagnostic(
            code="JAUNT_TS_MIGRATE_REBUILD_REQUIRED",
            message=(
                f"{module_id} has a built implementation but no compatible sidecar; "
                "run `jaunt build --language ts --force` to rebuild it."
            ),
            classification="model-rebuild",
            module_id=module_id,
            path=sidecar_path,
        )
    if "semanticEnvironmentDigest" in expected and "semanticEnvironmentDigest" not in actual:
        return TypeScriptMigrationDiagnostic(
            code="JAUNT_TS_MIGRATE_REBUILD_REQUIRED",
            message=(
                f"{module_id} predates persisted TypeScript environment proof; "
                "run `jaunt build --language ts` to rebuild it once."
            ),
            classification="model-rebuild",
            module_id=module_id,
            path=sidecar_path,
        )
    for field in ("schema",):
        if actual.get(field) != expected.get(field):
            return TypeScriptMigrationDiagnostic(
                code="JAUNT_TS_MIGRATE_ALPHA_SCHEME_INCOMPATIBLE",
                message=(
                    f"{module_id} uses incompatible TypeScript {field} {actual.get(field)!r}; "
                    f"current artifacts require {expected.get(field)!r}. Rebuild with "
                    "`jaunt build --language ts --force`."
                ),
                classification="model-rebuild",
                module_id=module_id,
                path=sidecar_path,
            )
    for field in ("protocol", "ir"):
        if _scheme_value(actual, field) != _scheme_value(expected, field):
            return TypeScriptMigrationDiagnostic(
                code="JAUNT_TS_MIGRATE_ALPHA_SCHEME_INCOMPATIBLE",
                message=(
                    f"{module_id} uses incompatible TypeScript {field} "
                    f"{_scheme_value(actual, field)!r}; current artifacts require "
                    f"{_scheme_value(expected, field)!r}. Rebuild with "
                    "`jaunt build --language ts --force`."
                ),
                classification="model-rebuild",
                module_id=module_id,
                path=sidecar_path,
            )
    identity_fields = (
        "moduleId",
        "specPath",
        "facadePath",
        "apiMirrorPath",
        "implementationPath",
        "project",
        "packageOwner",
    )
    drift = [field for field in identity_fields if actual.get(field) != expected.get(field)]
    if drift:
        return TypeScriptMigrationDiagnostic(
            code="JAUNT_TS_MIGRATE_LAYOUT_INCOMPATIBLE",
            message=(
                f"{module_id} sidecar layout does not match the current route "
                f"({', '.join(drift)}); no paths were guessed. Rebuild after resolving the layout."
            ),
            classification="model-rebuild",
            module_id=module_id,
            path=sidecar_path,
        )
    digest_fields = ("structuralDigest", "proseDigest", "apiDigest")
    changed = [field for field in digest_fields if actual.get(field) != expected.get(field)]
    if changed:
        if semantic_compatible:
            return None
        return TypeScriptMigrationDiagnostic(
            code="JAUNT_TS_MIGRATE_REBUILD_REQUIRED",
            message=(
                f"{module_id} contract changed ({', '.join(changed)}); deterministic migration "
                "cannot authorize the old implementation. Run `jaunt build --language ts`."
            ),
            classification="model-rebuild",
            module_id=module_id,
            path=implementation_path,
        )
    return None


def _action_description(kind: str, path: str, classification: str) -> str:
    verb = {
        "api-mirror": "repair API mirror",
        "facade": "create validated public facade",
        "placeholder": "emit typed unbuilt placeholder",
        "sidecar": "refresh artifact sidecar",
        "implementation": "re-stamp built implementation provenance",
    }.get(kind, f"rewrite {kind}")
    return f"[{classification}] {verb}: {path}"


async def plan_typescript_migration(
    root: Path,
    config: JauntConfig,
    *,
    worker_factory: Any | None = None,
) -> TypeScriptMigrationPlan:
    """Plan model-free TypeScript artifact repairs through the project worker."""

    from jaunt.typescript.builder import (
        MISSING_INPUT,
        _artifact_preconditions,
        _artifact_writes,
        _input_hashes,
        _module_id,
        _module_path,
        _path_hash,
        _safe_path,
        analyze,
        validate_overlay,
        worker_session,
    )
    from jaunt.typescript.status import classify_modules
    from jaunt.typescript.upgrade import compatible_semantic_modules

    root = root.resolve()
    if config.version != 2 or config.typescript_target is None:
        raise JauntConfigError("TypeScript migration requires config version 2 with [target.ts]")
    legacy_actions, legacy_diagnostics, legacy_inputs = _legacy_layout_plan(root, config)
    if legacy_diagnostics:
        payload = json.dumps(
            {
                "actions": [action.to_json() for action in legacy_actions],
                "diagnostics": [item.to_json() for item in legacy_diagnostics],
                "inputs": legacy_inputs,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return TypeScriptMigrationPlan(
            root=root,
            actions=legacy_actions,
            diagnostics=legacy_diagnostics,
            expected_inputs=legacy_inputs,
            writes=(),
            plan_digest=_sha256(payload.encode("utf-8")),
        )

    # The runtime protocol accepts a callable worker factory. Keep the public
    # annotation loose here so importing this config-migration module stays cheap.
    async with worker_session(root, config, worker_factory=worker_factory) as (
        client,
        initialized,
    ):
        analysis = await analyze(client, initialized)
        modules = analysis.modules
        semantic_compatible = compatible_semantic_modules(root, tuple(modules))
        status = classify_modules(root, modules)
        expected_inputs = {
            **_input_hashes(analysis.contracts),
            **_artifact_preconditions(root, modules),
            **legacy_inputs,
        }
        writes: list[Any] = []
        actions: list[TypeScriptMigrationAction] = list(legacy_actions)
        diagnostics: list[TypeScriptMigrationDiagnostic] = list(legacy_diagnostics)
        repairable_invalid = {
            "JAUNT_TS_API_DRIFT",
            "JAUNT_TS_FACADE_MISSING",
            "JAUNT_TS_FACADE_DRIFT",
            "JAUNT_TS_ARTIFACT_DRIFT",
            "JAUNT_TS_SIDECAR_HASHES_MISSING",
        }
        for module in modules:
            module_id = _module_id(module)
            issue = _built_migration_issue(
                root,
                module,
                semantic_compatible=module_id in semantic_compatible,
            )
            invalid_codes = {item.code for item in status.invalid.get(module_id, ())}
            if issue is None and invalid_codes - repairable_invalid:
                issue = TypeScriptMigrationDiagnostic(
                    code="JAUNT_TS_MIGRATE_REBUILD_REQUIRED",
                    message=(
                        f"{module_id} has non-repairable generated artifact diagnostics: "
                        + ", ".join(sorted(invalid_codes - repairable_invalid))
                        + ". Run `jaunt build --language ts --force`."
                    ),
                    classification="model-rebuild",
                    module_id=module_id,
                    path=_module_path(module, "implementationPath"),
                )
            if issue is not None:
                diagnostics.append(issue)
                actions.append(
                    TypeScriptMigrationAction(
                        module_id=module_id,
                        path=issue.path or _module_path(module, "implementationPath"),
                        kind="rebuild",
                        classification="model-rebuild",
                        description=issue.message,
                    )
                )
                continue
            built = "// jaunt:state=built" in (
                _safe_path(root, _module_path(module, "implementationPath")).read_text(
                    encoding="utf-8"
                )
                if _safe_path(root, _module_path(module, "implementationPath")).is_file()
                else ""
            )
            recompose = built and status.stale.get(module_id) == "toolchain"
            restamp = (
                built
                and not recompose
                and (module_id in status.stale or module_id in status.invalid)
            )
            validated = await validate_overlay(
                client,
                analysis,
                {},
                (module_id,),
                restamp_module_ids=(module_id,) if restamp else (),
                recompose_module_ids=(module_id,) if recompose else (),
                sync_module_ids=() if (restamp or recompose) else (module_id,),
            )
            if not validated.valid:
                rendered = (
                    "; ".join(f"{item.code}: {item.message}" for item in validated.diagnostics)
                    or "worker validation failed"
                )
                diagnostic = TypeScriptMigrationDiagnostic(
                    code="JAUNT_TS_MIGRATE_MANUAL_INTERVENTION",
                    message=(
                        f"{module_id} cannot be migrated deterministically: {rendered}. "
                        "No artifacts were changed."
                    ),
                    classification="manual-intervention",
                    module_id=module_id,
                    path=_module_path(module, "specPath"),
                    severity="error",
                )
                diagnostics.append(diagnostic)
                actions.append(
                    TypeScriptMigrationAction(
                        module_id=module_id,
                        path=diagnostic.path or _module_path(module, "specPath"),
                        kind="validation",
                        classification="manual-intervention",
                        description=diagnostic.message,
                    )
                )
                continue
            for write in _artifact_writes(validated):
                current = _path_hash(_safe_path(root, write.path))
                proposed = _sha256((write.content or "").encode("utf-8"))
                if current == proposed:
                    continue
                classification = (
                    "free-recompose"
                    if recompose and write.kind in {"implementation", "api-mirror", "sidecar"}
                    else (
                        "free-restamp"
                        if restamp and write.kind in {"implementation", "sidecar"}
                        else "deterministic-rewrite"
                    )
                )
                writes.append(write)
                actions.append(
                    TypeScriptMigrationAction(
                        module_id=module_id,
                        path=write.path,
                        kind=write.kind,
                        classification=classification,
                        description=_action_description(write.kind, write.path, classification),
                    )
                )
                expected_inputs.setdefault(write.path, current or MISSING_INPUT)

    actions.sort(key=lambda item: (item.module_id, item.path, item.classification))
    diagnostics.sort(key=lambda item: (item.module_id or "", item.path or "", item.code))
    writes.sort(
        key=lambda item: (
            str(getattr(item, "module_id", "")),
            str(getattr(item, "path", "")),
        )
    )
    payload = json.dumps(
        {
            "actions": [action.to_json() for action in actions],
            "diagnostics": [item.to_json() for item in diagnostics],
            "inputs": dict(sorted(expected_inputs.items())),
            "writes": [
                {
                    "path": getattr(write, "path", ""),
                    "sha256": _sha256(str(getattr(write, "content", "")).encode("utf-8")),
                }
                for write in writes
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return TypeScriptMigrationPlan(
        root=root,
        actions=tuple(actions),
        diagnostics=tuple(diagnostics),
        expected_inputs=dict(sorted(expected_inputs.items())),
        writes=tuple(writes),
        plan_digest=_sha256(payload.encode("utf-8")),
    )


def apply_typescript_migration(plan: TypeScriptMigrationPlan) -> tuple[str, ...]:
    """Atomically apply a previously validated plan if every input is unchanged."""

    from jaunt.journal import JournalEvent, append_events
    from jaunt.typescript.builder import _Write, atomic_write_manifest

    if plan.blocked:
        raise JauntConfigError(
            "TypeScript migration requires manual intervention; no artifacts were written"
        )
    writes = tuple(write for write in plan.writes if isinstance(write, _Write))
    if len(writes) != len(plan.writes):
        raise JauntConfigError("TypeScript migration plan contains an invalid write record")
    applied = atomic_write_manifest(
        plan.root,
        writes,
        expected_inputs=plan.expected_inputs,
        preserve_existing_facades=False,
        preserve_real_implementations=False,
    )
    append_events(
        plan.root,
        [JournalEvent("migrate", write.module_id, write.path) for write in applied],
    )
    return tuple(write.path for write in applied)

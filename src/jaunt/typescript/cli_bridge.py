"""Version-2 CLI payloads and concise human rendering for the TypeScript target."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jaunt.targets.base import (
    TargetBuildReport,
    TargetCheckReport,
    TargetDiagnostic,
    TargetStatus,
    TargetTestReport,
    TargetWorkspace,
)
from jaunt.typescript.builder import SyncReport
from jaunt.typescript.contracts import LifecycleReport
from jaunt.typescript.design import DesignReport
from jaunt.typescript.status import CleanReport


def local_id(value: str) -> str:
    return value.removeprefix("ts:")


def diagnostic_payload(item: TargetDiagnostic) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": item.code,
        "message": item.message,
        "severity": item.severity,
    }
    if item.path is not None:
        payload["path"] = item.path
    if item.line is not None:
        payload["line"] = item.line
    if item.column is not None:
        payload["column"] = item.column
    if item.data:
        payload["data"] = dict(item.data)
    return payload


def failures_payload(
    failures: Mapping[str, tuple[TargetDiagnostic, ...]],
    *,
    local: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    return {
        local_id(module_id) if local else module_id: [
            diagnostic_payload(item) for item in diagnostics
        ]
        for module_id, diagnostics in sorted(failures.items())
    }


def _orphan_paths(items: tuple[Any, ...], root: Path | None) -> list[str]:
    paths: list[str] = []
    for item in items:
        path = item.path
        if root is not None:
            try:
                path = path.relative_to(root)
            except ValueError:
                pass
        paths.append(path.as_posix())
    return sorted(paths)


def build_payload(report: TargetBuildReport, *, command: str = "build") -> dict[str, Any]:
    generated = sorted(report.generated)
    skipped = sorted(report.skipped)
    refrozen = sorted(report.refrozen)
    failed = failures_payload(report.failed)
    target = {
        "generated": [local_id(item) for item in generated],
        "skipped": [local_id(item) for item in skipped],
        "refrozen": [local_id(item) for item in refrozen],
        "failed": failures_payload(report.failed, local=True),
    }
    payload: dict[str, Any] = {
        "schema_version": 2,
        "command": command,
        "ok": report.exit_code == 0,
        "generated": generated,
        "skipped": skipped,
        "refrozen": refrozen,
        "failed": failed,
        "targets": {"ts": target},
    }
    if report.advisories:
        payload["advisories"] = {
            key: list(value) for key, value in sorted(report.advisories.items())
        }
    if report.metadata:
        payload.update(dict(report.metadata))
    return payload


def test_payload(report: TargetTestReport) -> dict[str, Any]:
    base = build_payload(
        TargetBuildReport(
            language="ts",
            generated=report.generated,
            skipped=report.skipped,
            refrozen=report.refrozen,
            failed=report.failed,
            exit_code=report.exit_code,
        ),
        command="test",
    )
    base["vitest"] = dict(report.runner)
    target = base["targets"]["ts"]
    target["vitest"] = dict(report.runner)
    cost = report.runner.get("cost")
    if isinstance(cost, Mapping):
        base["cost"] = dict(cost)
        target["cost"] = dict(cost)
    return base


def status_payload(status: TargetStatus) -> dict[str, Any]:
    stale = dict(sorted(status.stale.items()))
    invalid = failures_payload(status.invalid)
    orphans = _orphan_paths(status.orphans, status.root)
    target = {
        "fresh": [local_id(item) for item in sorted(status.fresh)],
        "stale": {local_id(key): value for key, value in stale.items()},
        "unbuilt": [local_id(item) for item in sorted(status.unbuilt)],
        "invalid": failures_payload(status.invalid, local=True),
        "orphans": orphans,
    }
    payload: dict[str, Any] = {
        "schema_version": 2,
        "command": "status",
        "ok": True,
        "fresh": sorted(status.fresh),
        "stale": sorted(stale),
        "stale_changes": stale,
        "unbuilt": sorted(status.unbuilt),
        "invalid": invalid,
        "digests": dict(sorted(status.digests.items())),
        "orphans": orphans,
        "diagnostics": [diagnostic_payload(item) for item in status.diagnostics],
        "targets": {"ts": target},
    }
    payload.update(dict(status.metadata))
    if status.metadata:
        target.update(dict(status.metadata))
    return payload


def check_payload(report: TargetCheckReport) -> dict[str, Any]:
    orphans = _orphan_paths(report.orphans, report.root)
    magic = {
        "fresh": [local_id(item) for item in sorted(report.fresh)],
        "stale": {local_id(key): value for key, value in sorted(report.stale.items())},
        "unbuilt": [local_id(item) for item in sorted(report.unbuilt)],
        "invalid": failures_payload(report.invalid, local=True),
        "orphans": orphans,
    }
    return {
        "schema_version": 2,
        "command": "check",
        "ok": report.exit_code == 0,
        "blocked": list(report.blocked),
        "checked": list(report.checked),
        "diagnostics": [diagnostic_payload(item) for item in report.diagnostics],
        "invalid": failures_payload(report.invalid),
        "stale": dict(sorted(report.stale.items())),
        "unbuilt": sorted(report.unbuilt),
        "orphans": orphans,
        "magic": {"ts": magic},
        "targets": {"ts": {"magic": magic}},
    }


def specs_payload(workspace: TargetWorkspace) -> dict[str, Any]:
    specs = list(workspace.metadata.get("specs", ()))
    routes = list(workspace.metadata.get("routes", ()))
    dependency_graph = workspace.metadata.get("dependency_graph", {})
    return {
        "schema_version": 2,
        "command": "specs",
        "ok": True,
        "specs": specs,
        "dependency_graph": dependency_graph,
        "projects": list(workspace.projects),
        "owners": list(workspace.owners),
        "routes": routes,
        "targets": {
            "ts": {
                "specs": specs,
                "dependency_graph": dependency_graph,
                "routes": routes,
            }
        },
    }


def sync_payload(report: SyncReport) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "command": "sync",
        "ok": report.ok,
        "mirrors": list(report.mirrors),
        "placeholders": list(report.placeholders),
        "created_facades": list(report.created_facades),
        "failed": failures_payload(report.failed),
        "targets": {
            "ts": {
                "mirrors": list(report.mirrors),
                "placeholders": list(report.placeholders),
                "created_facades": list(report.created_facades),
                "failed": failures_payload(report.failed, local=True),
            }
        },
    }


def clean_payload(report: CleanReport) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "command": "clean",
        "ok": report.exit_code == 0,
        "removed": list(report.removed),
        "would_remove": list(report.would_remove),
        "targets": {
            "ts": {
                "removed": list(report.removed),
                "would_remove": list(report.would_remove),
            }
        },
    }


def design_payload(report: DesignReport) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "command": "design",
        "ok": report.ok,
        "target": report.target_id,
        "patch": report.patch,
        "applied": report.applied,
        "diagnostics": [diagnostic_payload(item) for item in report.diagnostics],
        "usage": dict(report.usage or {}),
        "targets": {
            "ts": {
                "target": local_id(report.target_id),
                "applied": report.applied,
            }
        },
    }


def lifecycle_payload(report: LifecycleReport) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 2,
        "command": report.command,
        "ok": report.ok,
        "changed": list(report.changed),
        "removed": list(report.removed),
        "proposed": dict(report.proposed),
        "diagnostics": [diagnostic_payload(item) for item in report.diagnostics],
        "usage": dict(report.usage or {}),
        "targets": {
            "ts": {
                "selected": [local_id(item) for item in report.targets],
                "changed": list(report.changed),
                "removed": list(report.removed),
            }
        },
    }
    payload.update(dict(report.metadata))
    if "strength" in report.metadata:
        payload["targets"]["ts"]["strength"] = report.metadata["strength"]
    return payload


def _npm_skill_warnings(payload: Mapping[str, Any]) -> tuple[str, ...]:
    metadata = payload.get("npm_skills")
    if not isinstance(metadata, Mapping):
        runner = payload.get("vitest")
        metadata = runner.get("npm_skills") if isinstance(runner, Mapping) else None
    if not isinstance(metadata, Mapping):
        return ()
    warnings = metadata.get("warnings")
    if not isinstance(warnings, (list, tuple)):
        return ()
    return tuple(item for item in warnings if isinstance(item, str) and item)


def human_lines(payload: Mapping[str, Any]) -> tuple[str, ...]:
    command = str(payload.get("command", "typescript"))
    lines = [f"TypeScript {command}:"]
    npm_skills = payload.get("npm_skills")
    if isinstance(npm_skills, Mapping):
        plan = npm_skills.get("plan")
        if isinstance(plan, Mapping):
            file_count = plan.get("file_count", 0)
            total_bytes = plan.get("total_bytes", 0)
            lines.append(f"  npm skill plan: {file_count} files, {total_bytes} bytes")
        elif npm_skills.get("enabled") is False:
            lines.append("  npm skill plan: disabled for TypeScript")
    for key in (
        "generated",
        "skipped",
        "refrozen",
        "mirrors",
        "placeholders",
        "created_facades",
        "removed",
        "would_remove",
        "fresh",
        "stale",
        "unbuilt",
        "orphans",
    ):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            lines.append(f"  {key}: {len(value)}")
            lines.extend(f"    - {item}" for item in value)
    failures = payload.get("failed", payload.get("invalid"))
    if isinstance(failures, Mapping) and failures:
        lines.append(f"  failed: {len(failures)}")
        for module_id, diagnostics in failures.items():
            lines.append(f"    - {module_id}")
            if isinstance(diagnostics, list):
                lines.extend(
                    f"      {item.get('code', 'error')}: {item.get('message', '')}"
                    for item in diagnostics
                    if isinstance(item, Mapping)
                )
    strength = payload.get("strength")
    if isinstance(strength, Mapping):
        lines.append(f"  mutation strength: {'enabled' if strength.get('enabled') else 'disabled'}")
        targets = strength.get("targets")
        if isinstance(targets, Mapping):
            for target_id, raw in sorted(targets.items()):
                if not isinstance(raw, Mapping):
                    continue
                score = raw.get("score")
                if not isinstance(score, Mapping):
                    continue
                lines.append(
                    f"    - {target_id}: {score.get('killed', 0)}/{score.get('applicable', 0)} "
                    f"killed, {score.get('excluded', 0)} excluded"
                )
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        lines.append(f"  diagnostics: {len(diagnostics)}")
        for item in diagnostics:
            if not isinstance(item, Mapping):
                continue
            location = f" ({item['path']})" if item.get("path") else ""
            lines.append(
                f"    - {item.get('severity', 'error')} "
                f"{item.get('code', 'diagnostic')}: {item.get('message', '')}{location}"
            )
    blocked = payload.get("blocked")
    if isinstance(blocked, list) and blocked:
        lines.append(f"  blocked: {len(blocked)}")
        for item in blocked:
            if isinstance(item, Mapping):
                detail = item.get("target", item.get("battery", ""))
                suffix = f": {detail}" if detail else ""
                lines.append(f"    - {item.get('reason', 'blocked')}{suffix}")
    patch = payload.get("patch")
    if isinstance(patch, str) and patch:
        lines.extend(("", patch.rstrip()))
    proposed = payload.get("proposed")
    if isinstance(proposed, Mapping) and proposed:
        lines.append(f"  proposed files: {len(proposed)}")
        lines.extend(f"    - {path}" for path in proposed)
    lines.extend(f"  warning: {warning}" for warning in _npm_skill_warnings(payload))
    return tuple(lines)


__all__ = [
    "build_payload",
    "check_payload",
    "clean_payload",
    "design_payload",
    "human_lines",
    "lifecycle_payload",
    "specs_payload",
    "status_payload",
    "sync_payload",
    "test_payload",
]

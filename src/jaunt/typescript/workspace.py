"""Python-side view of routes returned by the TypeScript analyzer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from jaunt.typescript.protocol import ProtocolValidationError, WorkspaceStamp


def _wire_mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolValidationError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def root_relative_path(root: Path, value: str, *, label: str) -> Path:
    """Resolve a protocol POSIX path without allowing workspace escape."""

    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ProtocolValidationError(f"{label} must be a safe root-relative POSIX path")
    root = root.resolve()
    result = (root / Path(*pure.parts)).resolve()
    if result != root and root not in result.parents:
        raise ProtocolValidationError(f"{label} escapes the workspace: {value!r}")
    return result


@dataclass(frozen=True, slots=True)
class TypeScriptProject:
    id: str
    config_path: str
    role: str
    references: tuple[str, ...] = ()
    package_owner: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, value: object) -> TypeScriptProject:
        raw = _wire_mapping(value, label="project")
        project_id = raw.get("id", raw.get("projectId"))
        config_path = raw.get("configPath", raw.get("path"))
        if not isinstance(project_id, str) or not isinstance(config_path, str):
            raise ProtocolValidationError("project id/configPath must be strings")
        refs = raw.get("references", [])
        if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs):
            raise ProtocolValidationError("project.references must be an array of strings")
        known = {"id", "projectId", "configPath", "path", "role", "references", "packageOwner"}
        owner = raw.get("packageOwner")
        return cls(
            id=project_id,
            config_path=config_path,
            role=str(raw.get("role", "production")),
            references=tuple(refs),
            package_owner=str(owner) if owner is not None else None,
            data={key: item for key, item in raw.items() if key not in known},
        )


@dataclass(frozen=True, slots=True)
class TypeScriptRoute:
    module_id: str
    spec_path: str
    facade_path: str
    api_path: str
    implementation_path: str
    project_id: str
    package_owner: str
    context_path: str | None = None
    sidecar_path: str = ""

    @classmethod
    def from_wire(cls, value: object) -> TypeScriptRoute:
        raw = _wire_mapping(value, label="route")

        def required(key: str, *aliases: str) -> str:
            item = raw.get(key)
            if item is None:
                item = next(
                    (raw.get(alias) for alias in aliases if raw.get(alias) is not None),
                    None,
                )
            if not isinstance(item, str) or not item:
                raise ProtocolValidationError(f"route.{key} must be a non-empty string")
            return item

        context = raw.get("contextPath")
        return cls(
            module_id=required("moduleId"),
            spec_path=required("specPath"),
            facade_path=required("facadePath"),
            api_path=required("apiMirrorPath", "apiPath"),
            implementation_path=required("implementationPath"),
            project_id=required("project", "projectId"),
            package_owner=required("packageOwner"),
            context_path=str(context) if context is not None else None,
            sidecar_path=required("sidecarPath"),
        )


@dataclass(frozen=True, slots=True)
class TypeScriptWorkspace:
    stamp: WorkspaceStamp
    projects: tuple[TypeScriptProject, ...]
    routes: tuple[TypeScriptRoute, ...]
    specs: tuple[Mapping[str, Any], ...]
    test_specs: tuple[Mapping[str, Any], ...]
    contracts: tuple[Mapping[str, Any], ...]
    diagnostics: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_wire(cls, value: object) -> TypeScriptWorkspace:
        raw = _wire_mapping(value, label="analyzeWorkspace result")

        def records(key: str) -> tuple[Mapping[str, Any], ...]:
            items = raw.get(key, [])
            if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
                raise ProtocolValidationError(f"{key} must be an array of objects")
            return tuple(dict(item) for item in items)

        projects_raw = raw.get("projects", [])
        routes_raw = raw.get("routes", [])
        if not isinstance(projects_raw, list) or not isinstance(routes_raw, list):
            raise ProtocolValidationError("projects and routes must be arrays")
        return cls(
            stamp=WorkspaceStamp.from_wire(raw),
            projects=tuple(TypeScriptProject.from_wire(item) for item in projects_raw),
            routes=tuple(TypeScriptRoute.from_wire(item) for item in routes_raw),
            specs=records("specs"),
            test_specs=records("testSpecs"),
            contracts=records("contracts"),
            diagnostics=records("diagnostics"),
        )

    @property
    def routes_by_module(self) -> dict[str, TypeScriptRoute]:
        return {route.module_id: route for route in self.routes}

"""Typed wire records for the Jaunt TypeScript JSONL protocol."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

PROTOCOL_VERSION = "jaunt-ts/1-draft.2"


class ProtocolValidationError(ValueError):
    """A worker message does not satisfy the protocol envelope."""


def _mapping(value: object, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolValidationError(f"{field_name} must be an object")
    return {str(key): item for key, item in value.items()}


def _string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolValidationError(f"{field_name} must be a non-empty string")
    return value


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProtocolValidationError(f"{field_name} must be a string")
    return value


def _only_keys(raw: Mapping[str, Any], allowed: set[str], *, field_name: str) -> None:
    extras = sorted(set(raw) - allowed)
    if extras:
        raise ProtocolValidationError(
            f"{field_name} contains unknown field(s): {', '.join(extras)}"
        )


def _integer(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolValidationError(f"{field_name} must be an integer")
    return value


def _strings(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProtocolValidationError(f"{field_name} must be an array of strings")
    if any(not isinstance(item, str) for item in value):
        raise ProtocolValidationError(f"{field_name} must be an array of strings")
    return tuple(item for item in value if isinstance(item, str))


@dataclass(frozen=True, slots=True)
class ProtocolDiagnostic:
    code: str
    message: str
    severity: Literal["error", "warning", "info"] = "error"
    path: str | None = None
    start: int | None = None
    end: int | None = None
    line: int | None = None
    column: int | None = None

    @classmethod
    def from_wire(cls, value: object) -> ProtocolDiagnostic:
        raw = _mapping(value, field_name="diagnostic")
        _only_keys(
            raw,
            {"code", "severity", "message", "path", "start", "end", "line", "column"},
            field_name="diagnostic",
        )
        severity = raw.get("severity")
        if severity not in {"error", "warning", "info"}:
            raise ProtocolValidationError(
                "diagnostic.severity must be 'error', 'warning', or 'info'"
            )

        def optional_integer(name: str, minimum: int) -> int | None:
            value = raw.get(name)
            if value is None:
                return None
            result = _integer(value, field_name=f"diagnostic.{name}")
            if result < minimum:
                raise ProtocolValidationError(f"diagnostic.{name} must be >= {minimum}")
            return result

        path = raw.get("path")
        if path is not None and not isinstance(path, str):
            raise ProtocolValidationError("diagnostic.path must be a string")
        return cls(
            code=_string(raw.get("code"), field_name="diagnostic.code"),
            message=_text(raw.get("message"), field_name="diagnostic.message"),
            severity=severity,
            path=path,
            start=optional_integer("start", 0),
            end=optional_integer("end", 0),
            line=optional_integer("line", 1),
            column=optional_integer("column", 1),
        )

    def to_wire(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.path is not None:
            result["path"] = self.path
        if self.start is not None:
            result["start"] = self.start
        if self.end is not None:
            result["end"] = self.end
        if self.line is not None:
            result["line"] = self.line
        if self.column is not None:
            result["column"] = self.column
        return result


@dataclass(frozen=True, slots=True)
class ProtocolError:
    code: str
    message: str
    retryable: bool
    diagnostics: tuple[ProtocolDiagnostic, ...] = ()

    @classmethod
    def from_wire(cls, value: object) -> ProtocolError:
        raw = _mapping(value, field_name="error")
        _only_keys(
            raw,
            {"code", "message", "retryable", "diagnostics"},
            field_name="error",
        )
        retryable = raw.get("retryable")
        if not isinstance(retryable, bool):
            raise ProtocolValidationError("error.retryable must be a boolean")
        diagnostics_raw = raw.get("diagnostics")
        if not isinstance(diagnostics_raw, list):
            raise ProtocolValidationError("error.diagnostics must be an array")
        return cls(
            code=_string(raw.get("code"), field_name="error.code"),
            message=_text(raw.get("message"), field_name="error.message"),
            retryable=retryable,
            diagnostics=tuple(ProtocolDiagnostic.from_wire(item) for item in diagnostics_raw),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "diagnostics": [diagnostic.to_wire() for diagnostic in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class ProtocolRequest:
    id: str
    method: str
    params: Mapping[str, Any]
    deadline_ms: int | None = None
    protocol: str = PROTOCOL_VERSION

    def to_wire(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "protocol": self.protocol,
            "id": self.id,
            "method": self.method,
            "params": dict(self.params),
        }
        if self.deadline_ms is not None:
            result["deadlineMs"] = self.deadline_ms
        return result


@dataclass(frozen=True, slots=True)
class ProtocolResponse:
    id: str
    ok: bool
    result: Mapping[str, Any] | None = None
    error: ProtocolError | None = None
    protocol: str = PROTOCOL_VERSION

    @classmethod
    def from_wire(cls, value: object) -> ProtocolResponse:
        raw = _mapping(value, field_name="response")
        protocol = _string(raw.get("protocol"), field_name="response.protocol")
        request_id = _string(raw.get("id"), field_name="response.id")
        ok = raw.get("ok")
        if not isinstance(ok, bool):
            raise ProtocolValidationError("response.ok must be a boolean")
        if ok:
            if "error" in raw:
                raise ProtocolValidationError("successful response must not contain error")
            _only_keys(raw, {"protocol", "id", "ok", "result"}, field_name="response")
            return cls(
                protocol=protocol,
                id=request_id,
                ok=True,
                result=_mapping(raw.get("result"), field_name="response.result"),
            )
        if "result" in raw:
            raise ProtocolValidationError("failed response must not contain result")
        _only_keys(raw, {"protocol", "id", "ok", "error"}, field_name="response")
        return cls(
            protocol=protocol,
            id=request_id,
            ok=False,
            error=ProtocolError.from_wire(raw.get("error")),
        )


@dataclass(frozen=True, slots=True)
class InitializeParams:
    root: str
    projects: tuple[str, ...]
    test_projects: tuple[str, ...]
    source_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    generated_dir: str
    tool_owner: str
    compiler_module_path: str
    client_version: str
    tool_version: str
    generation_fingerprint: str = ""

    def to_wire(self) -> dict[str, Any]:
        result = {
            "root": self.root,
            "projects": list(self.projects),
            "testProjects": list(self.test_projects),
            "sourceRoots": list(self.source_roots),
            "testRoots": list(self.test_roots),
            "generatedDir": self.generated_dir,
            "toolOwner": self.tool_owner,
            "compilerModulePath": self.compiler_module_path,
            "clientVersion": self.client_version,
            "toolVersion": self.tool_version,
        }
        if self.generation_fingerprint:
            result["generationFingerprint"] = self.generation_fingerprint
        return result


@dataclass(frozen=True, slots=True)
class WorkspaceStamp:
    session_id: str
    epoch: int
    snapshot: str
    input_hashes: Mapping[str, str]

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> WorkspaceStamp:
        hashes = _mapping(value.get("inputHashes"), field_name="result.inputHashes")
        if any(not isinstance(item, str) for item in hashes.values()):
            raise ProtocolValidationError("result.inputHashes values must be strings")
        return cls(
            session_id=_string(value.get("sessionId"), field_name="result.sessionId"),
            epoch=_integer(value.get("epoch"), field_name="result.epoch"),
            snapshot=_string(value.get("snapshot"), field_name="result.snapshot"),
            input_hashes={key: str(item) for key, item in hashes.items()},
        )


@dataclass(frozen=True, slots=True)
class InitializeResult:
    worker_version: str
    protocol: str
    typescript_version: str
    capabilities: tuple[str, ...]
    stamp: WorkspaceStamp
    package_manager: str = "unknown"

    @classmethod
    def from_wire(cls, value: object) -> InitializeResult:
        raw = _mapping(value, field_name="initialize result")
        return cls(
            worker_version=_string(raw.get("workerVersion"), field_name="workerVersion"),
            protocol=_string(raw.get("protocol"), field_name="protocol"),
            typescript_version=_string(
                raw.get("typescriptVersion"), field_name="typescriptVersion"
            ),
            capabilities=_strings(raw.get("capabilities"), field_name="capabilities"),
            stamp=WorkspaceStamp.from_wire(raw),
            package_manager=_string(
                raw.get("packageManager", "unknown"), field_name="packageManager"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectContractResult:
    source: str
    source_digest: str
    symbol: str
    kind: Literal["function", "class"]
    declaration_start: int
    declaration_end: int
    docs_start: int | None = None
    docs_end: int | None = None

    @classmethod
    def from_wire(cls, value: object) -> ProjectContractResult:
        raw = _mapping(value, field_name="projectContract result")
        _only_keys(
            raw,
            {
                "source",
                "sourceDigest",
                "symbol",
                "kind",
                "declarationStart",
                "declarationEnd",
                "docsStart",
                "docsEnd",
            },
            field_name="projectContract result",
        )
        source_digest = _string(
            raw.get("sourceDigest"), field_name="projectContract result.sourceDigest"
        )
        digest_value = source_digest.removeprefix("sha256:")
        if (
            not source_digest.startswith("sha256:")
            or len(digest_value) != 64
            or any(character not in "0123456789abcdef" for character in digest_value)
        ):
            raise ProtocolValidationError(
                "projectContract result.sourceDigest must be a lowercase sha256 digest"
            )
        kind = raw.get("kind")
        if kind not in {"function", "class"}:
            raise ProtocolValidationError(
                "projectContract result.kind must be 'function' or 'class'"
            )
        declaration_start = _integer(
            raw.get("declarationStart"),
            field_name="projectContract result.declarationStart",
        )
        declaration_end = _integer(
            raw.get("declarationEnd"), field_name="projectContract result.declarationEnd"
        )
        if declaration_start < 0 or declaration_end <= declaration_start:
            raise ProtocolValidationError(
                "projectContract declaration range must be non-empty and non-negative"
            )
        has_docs_start = "docsStart" in raw
        has_docs_end = "docsEnd" in raw
        if has_docs_start != has_docs_end:
            raise ProtocolValidationError(
                "projectContract result must provide docsStart and docsEnd together"
            )
        docs_start = (
            _integer(raw.get("docsStart"), field_name="projectContract result.docsStart")
            if has_docs_start
            else None
        )
        docs_end = (
            _integer(raw.get("docsEnd"), field_name="projectContract result.docsEnd")
            if has_docs_end
            else None
        )
        if docs_start is not None and docs_end is not None:
            if docs_start < 0 or docs_end <= docs_start or docs_end > declaration_start:
                raise ProtocolValidationError(
                    "projectContract TSDoc range must be non-empty and precede the declaration"
                )
        return cls(
            source=_string(raw.get("source"), field_name="projectContract result.source"),
            source_digest=source_digest,
            symbol=_string(raw.get("symbol"), field_name="projectContract result.symbol"),
            kind=kind,
            declaration_start=declaration_start,
            declaration_end=declaration_end,
            docs_start=docs_start,
            docs_end=docs_end,
        )


@dataclass(frozen=True, slots=True)
class ValidateOverlayParams:
    session_id: str
    expected_epoch: int
    expected_snapshot: str
    candidates: Mapping[str, str]
    module_ids: tuple[str, ...] = ()
    sync_module_ids: tuple[str, ...] = ()
    restamp_module_ids: tuple[str, ...] = ()
    recompose_module_ids: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "sessionId": self.session_id,
            "expectedEpoch": self.expected_epoch,
            "expectedSnapshot": self.expected_snapshot,
            "candidates": dict(self.candidates),
        }
        if self.module_ids:
            result["moduleIds"] = list(self.module_ids)
        if self.sync_module_ids:
            result["syncModuleIds"] = list(self.sync_module_ids)
        if self.restamp_module_ids:
            result["restampModuleIds"] = list(self.restamp_module_ids)
        if self.recompose_module_ids:
            result["recomposeModuleIds"] = list(self.recompose_module_ids)
        return result


@dataclass(frozen=True, slots=True)
class OverlayArtifact:
    path: str
    content: str
    sha256: str
    kind: str
    module_id: str

    @classmethod
    def from_wire(cls, value: object) -> OverlayArtifact:
        raw = _mapping(value, field_name="artifact")
        _only_keys(
            raw,
            {"path", "content", "sha256", "kind", "moduleId"},
            field_name="artifact",
        )
        return cls(
            path=_string(raw.get("path"), field_name="artifact.path"),
            content=_text(raw.get("content"), field_name="artifact.content"),
            sha256=_string(raw.get("sha256"), field_name="artifact.sha256"),
            kind=_string(raw.get("kind"), field_name="artifact.kind"),
            module_id=_string(raw.get("moduleId"), field_name="artifact.moduleId"),
        )


@dataclass(frozen=True, slots=True)
class ValidateOverlayResult:
    stamp: WorkspaceStamp
    valid: bool
    artifacts: tuple[OverlayArtifact, ...]
    diagnostics: tuple[ProtocolDiagnostic, ...]
    affected_projects: tuple[str, ...]

    @classmethod
    def from_wire(cls, value: object) -> ValidateOverlayResult:
        raw = _mapping(value, field_name="validateOverlay result")
        valid = raw.get("valid")
        if not isinstance(valid, bool):
            raise ProtocolValidationError("validateOverlay result.valid must be a boolean")
        artifacts = raw.get("artifacts", [])
        diagnostics = raw.get("diagnostics", [])
        if not isinstance(artifacts, list) or not isinstance(diagnostics, list):
            raise ProtocolValidationError("artifacts and diagnostics must be arrays")
        return cls(
            stamp=WorkspaceStamp.from_wire(raw),
            valid=valid,
            artifacts=tuple(OverlayArtifact.from_wire(item) for item in artifacts),
            diagnostics=tuple(ProtocolDiagnostic.from_wire(item) for item in diagnostics),
            affected_projects=_strings(
                raw.get("affectedProjects", []), field_name="affectedProjects"
            ),
        )

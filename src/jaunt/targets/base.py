"""Target-neutral identities, reports, and adapter protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from jaunt.config import JauntConfig

Language = Literal["py", "ts"]


@dataclass(frozen=True, slots=True)
class TargetRequest:
    root: Path
    config: JauntConfig
    target_ids: tuple[str, ...] = ()
    force: bool = False
    jobs: int | None = None


@dataclass(frozen=True, slots=True)
class TargetDiagnostic:
    code: str
    message: str
    severity: Literal["error", "warning", "info"] = "error"
    path: str | None = None
    line: int | None = None
    column: int | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TargetArtifact:
    path: Path
    kind: str
    module_id: str | None = None


@dataclass(frozen=True, slots=True)
class TargetWorkspace:
    language: Language
    module_ids: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TargetStatus:
    language: Language
    root: Path | None = None
    fresh: frozenset[str] = frozenset()
    stale: Mapping[str, str] = field(default_factory=dict)
    unbuilt: frozenset[str] = frozenset()
    invalid: Mapping[str, tuple[TargetDiagnostic, ...]] = field(default_factory=dict)
    digests: Mapping[str, str] = field(default_factory=dict)
    orphans: tuple[TargetArtifact, ...] = ()
    diagnostics: tuple[TargetDiagnostic, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TargetBuildReport:
    language: Language
    generated: frozenset[str] = frozenset()
    skipped: frozenset[str] = frozenset()
    refrozen: frozenset[str] = frozenset()
    failed: Mapping[str, tuple[TargetDiagnostic, ...]] = field(default_factory=dict)
    advisories: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    exit_code: int = 0


@dataclass(frozen=True, slots=True)
class TargetTestReport:
    language: Language
    generated: frozenset[str] = frozenset()
    skipped: frozenset[str] = frozenset()
    refrozen: frozenset[str] = frozenset()
    failed: Mapping[str, tuple[TargetDiagnostic, ...]] = field(default_factory=dict)
    runner: Mapping[str, Any] = field(default_factory=dict)
    exit_code: int = 0


@dataclass(frozen=True, slots=True)
class TargetCheckReport:
    language: Language
    root: Path | None = None
    fresh: frozenset[str] = frozenset()
    stale: Mapping[str, str] = field(default_factory=dict)
    unbuilt: frozenset[str] = frozenset()
    invalid: Mapping[str, tuple[TargetDiagnostic, ...]] = field(default_factory=dict)
    orphans: tuple[TargetArtifact, ...] = ()
    checked: tuple[Mapping[str, Any], ...] = ()
    blocked: tuple[Mapping[str, Any], ...] = ()
    diagnostics: tuple[TargetDiagnostic, ...] = ()
    exit_code: int = 0


class TargetAdapter(Protocol):
    language: Language

    async def discover(self, request: TargetRequest) -> TargetWorkspace: ...

    async def status(self, request: TargetRequest) -> TargetStatus: ...

    async def build(self, request: TargetRequest) -> TargetBuildReport: ...

    async def test(self, request: TargetRequest) -> TargetTestReport: ...

    async def check(self, request: TargetRequest) -> TargetCheckReport: ...

    async def find_orphans(self, request: TargetRequest) -> tuple[TargetArtifact, ...]: ...

"""Target-neutral adapter for the TypeScript implementation."""

from __future__ import annotations

from jaunt.targets.base import (
    Language,
    TargetArtifact,
    TargetBuildReport,
    TargetCheckReport,
    TargetRequest,
    TargetStatus,
    TargetTestReport,
    TargetWorkspace,
)
from jaunt.targets.python import PythonTargetServices, _call


class TypeScriptTargetAdapter:
    """Expose TypeScript services through the shared target protocol.

    ``services`` remains injectable for target-orchestrator unit tests. Normal
    callers use the concrete TypeScript builder/status/tester implementations.
    Imports stay local to keep the adapter independent from CLI rendering.
    """

    language: Language = "ts"

    def __init__(self, services: PythonTargetServices | None = None) -> None:
        self._services = services

    async def discover(self, request: TargetRequest) -> TargetWorkspace:
        if self._services is not None:
            return await _call(self._services.discover, request)
        from jaunt.typescript.status import run_specs

        return await run_specs(
            request.root,
            request.config,
            target_ids=request.target_ids,
        )

    async def status(self, request: TargetRequest) -> TargetStatus:
        if self._services is not None:
            return await _call(self._services.status, request)
        from jaunt.typescript.status import run_status

        return await run_status(
            request.root,
            request.config,
            target_ids=request.target_ids,
        )

    async def build(self, request: TargetRequest) -> TargetBuildReport:
        if self._services is not None:
            return await _call(self._services.build, request)
        from jaunt.typescript.builder import run_build

        return await run_build(
            request.root,
            request.config,
            target_ids=request.target_ids,
            force=request.force,
            jobs=request.jobs,
        )

    async def test(self, request: TargetRequest) -> TargetTestReport:
        if self._services is not None:
            return await _call(self._services.test, request)
        from jaunt.typescript.tester import run_test

        return await run_test(
            request.root,
            request.config,
            target_ids=request.target_ids,
            force=request.force,
            jobs=request.jobs,
        )

    async def check(self, request: TargetRequest) -> TargetCheckReport:
        if self._services is not None:
            return await _call(self._services.check, request)
        from jaunt.typescript.status import run_check

        return await run_check(
            request.root,
            request.config,
            target_ids=request.target_ids,
        )

    async def find_orphans(self, request: TargetRequest) -> tuple[TargetArtifact, ...]:
        if self._services is not None:
            return await _call(self._services.find_orphans, request)
        status = await self.status(request)
        return status.orphans

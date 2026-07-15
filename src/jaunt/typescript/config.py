"""Configuration values for the TypeScript target."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TypeScriptTargetConfig:
    """Static version-2 ``[target.ts]`` configuration.

    Paths are kept root-relative here. The worker resolves and containment-checks
    them against the configured workspace before reading user files.
    """

    source_roots: list[str]
    test_roots: list[str]
    projects: list[str]
    test_projects: list[str] = field(default_factory=list)
    tool_owner: str = "."
    generated_dir: str = "__generated__"
    test_runner: str = "vitest"
    vitest_config: str = ""
    vitest_args: list[str] = field(default_factory=list)
    auto_skills: bool | None = None
    auto_class_tests: bool = False
    fast_check_runs: int = 50
    contract_battery_dir: str = "tests/contract"

    def auto_skills_enabled(self, shared_default: bool) -> bool:
        """Resolve the target-local override without changing Python behavior."""

        return shared_default if self.auto_skills is None else self.auto_skills


@dataclass(frozen=True, slots=True)
class TypeScriptPromptsConfig:
    """Version-2 ``[prompts.ts]`` overrides resolved against the project root."""

    build_system: str = ""
    build_module: str = ""
    test_system: str = ""
    test_module: str = ""
    design_system: str = ""
    design_user: str = ""

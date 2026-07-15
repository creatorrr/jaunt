"""Project configuration loading for Jaunt.

This module is intentionally small and deterministic: it only reads `jaunt.toml`
and performs light validation/existence checks.
"""

from __future__ import annotations

import difflib
import glob
import keyword
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import jaunt
from jaunt.errors import JauntConfigError
from jaunt.typescript.config import TypeScriptPromptsConfig, TypeScriptTargetConfig


@dataclass(frozen=True)
class PathsConfig:
    source_roots: list[str]
    test_roots: list[str]
    generated_dir: str


@dataclass(frozen=True)
class PythonTargetConfig:
    """Version-2 ``[target.py]`` values.

    The existing ``PathsConfig``/``BuildConfig``/``TestConfig`` views remain the
    compatibility API consumed by the Python implementation. This record preserves
    the target boundary without making the Python AST pipeline target-aware.
    """

    source_roots: list[str]
    test_roots: list[str]
    generated_dir: str = "__generated__"
    infer_deps: bool = True
    test_infer_deps: bool = True
    emit_stubs: bool = True
    ty_retry_attempts: int = 1
    async_runner: str = "asyncio"
    check_generated_imports: bool = True
    generated_import_allowlist: list[str] = field(default_factory=list)
    pytest_args: list[str] = field(default_factory=lambda: ["-q"])
    auto_class_tests: bool = False
    contract_battery_dir: str = "tests/contract"


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key_env: str
    max_cost_per_build: float | None = None
    reasoning_effort: str | None = None
    anthropic_thinking_budget_tokens: int | None = None
    prompt_cache: bool = False
    prompt_cache_key: str = ""


_VALID_ASYNC_RUNNERS = ("asyncio", "anyio")
_VALID_AGENT_ENGINES = ("codex",)
_VALID_DERIVE = ("examples", "errors", "properties")
_VALID_REASONING_EFFORTS = ("low", "medium", "high")
_ALLOWED_SECTIONS = frozenset(
    {
        "version",
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
        "semantic_gate",
        "context",
    }
)
_PATHS_KEYS = frozenset({"source_roots", "test_roots", "generated_dir"})
_LLM_KEYS = frozenset(
    {
        "provider",
        "model",
        "api_key_env",
        "max_cost_per_build",
        "reasoning_effort",
        "anthropic_thinking_budget_tokens",
        "prompt_cache",
        "prompt_cache_key",
    }
)
_BUILD_KEYS = frozenset(
    {
        "jobs",
        "infer_deps",
        "ty_retry_attempts",
        "async_runner",
        "include_target_tests",
        "check_generated_imports",
        "generated_import_allowlist",
        "instructions",
        "emit_stubs",
    }
)
_TEST_KEYS = frozenset({"jobs", "infer_deps", "pytest_args", "auto_class_tests"})
_PROMPTS_KEYS = frozenset(
    {
        "build_system",
        "build_preamble",
        "build_module",
        "test_system",
        "test_module",
        "project_overview_system",
        "project_overview_user",
    }
)
_AGENT_KEYS = frozenset({"engine"})
_CODEX_KEYS = frozenset(
    {"model", "reasoning_effort", "sandbox", "fingerprint_cli_version", "features", "config"}
)
_DAEMON_KEYS = frozenset({"poll_interval", "max_jobs", "notify_command", "auto_commit"})
_SKILLS_KEYS = frozenset(
    {"auto", "max_chars_per_skill", "inject_user_skills", "builtin", "builtin_skills"}
)
_CONTRACT_KEYS = frozenset({"battery_dir", "derive", "strength", "property_max_examples"})
_SEMANTIC_GATE_KEYS = frozenset({"enabled", "model", "reasoning_effort"})
_CONTEXT_KEYS = frozenset(
    {"repo_map", "repo_map_file", "enrich", "max_chars", "overview", "search"}
)
_CONTEXT_SEARCH_KEYS = frozenset({"enabled", "internal_retrieval", "max_hits"})

_V2_ALLOWED_SECTIONS = frozenset(
    {
        "version",
        "target",
        "llm",
        "build",
        "test",
        "prompts",
        "agent",
        "codex",
        "daemon",
        "skills",
        "contract",
        "semantic_gate",
        "context",
    }
)
_V2_TARGET_KEYS = frozenset({"py", "ts"})
_V2_PY_TARGET_KEYS = frozenset(
    {
        "source_roots",
        "test_roots",
        "generated_dir",
        "infer_deps",
        "test_infer_deps",
        "emit_stubs",
        "ty_retry_attempts",
        "async_runner",
        "check_generated_imports",
        "generated_import_allowlist",
        "pytest_args",
        "auto_class_tests",
        "contract_battery_dir",
    }
)
_V2_TS_TARGET_KEYS = frozenset(
    {
        "source_roots",
        "test_roots",
        "projects",
        "test_projects",
        "tool_owner",
        "generated_dir",
        "test_runner",
        "vitest_config",
        "vitest_args",
        "auto_skills",
        "auto_class_tests",
        "fast_check_runs",
        "contract_battery_dir",
    }
)
_V2_BUILD_KEYS = frozenset({"jobs", "include_target_tests", "instructions"})
_V2_TEST_KEYS = frozenset({"jobs"})
_V2_CONTRACT_KEYS = frozenset({"derive", "strength", "property_max_examples"})
_V2_PROMPTS_KEYS = frozenset({"py", "ts"})
_V2_PROMPTS_PY_KEYS = _PROMPTS_KEYS
_V2_PROMPTS_TS_KEYS = frozenset(
    {"build_system", "build_module", "test_system", "test_module", "design_system", "design_user"}
)


def _default_builtin_skills() -> tuple[str, ...]:
    from jaunt.skills_builtin import DEFAULT_BUILTIN_SKILLS

    return DEFAULT_BUILTIN_SKILLS


@dataclass(frozen=True)
class BuildConfig:
    jobs: int
    infer_deps: bool
    ty_retry_attempts: int = 1
    async_runner: str = "asyncio"
    include_target_tests: bool = False
    check_generated_imports: bool = True
    generated_import_allowlist: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)
    emit_stubs: bool = True


@dataclass(frozen=True)
class TestConfig:
    __test__ = False  # prevent pytest collection

    jobs: int
    infer_deps: bool
    pytest_args: list[str]
    auto_class_tests: bool = False


@dataclass(frozen=True)
class PromptsConfig:
    build_system: str
    build_module: str
    test_system: str
    test_module: str
    build_preamble: str = ""
    project_overview_system: str = ""
    project_overview_user: str = ""


@dataclass(frozen=True)
class AgentConfig:
    engine: str = "codex"


@jaunt.contract
@dataclass(frozen=True)
class CodexConfig:
    """Codex engine settings; the defaults encode Jaunt's model policy.

    Every field defaults to Jaunt's canonical Codex configuration: model
    ``gpt-5.6-sol`` at ``medium`` reasoning effort, the ``workspace-write`` sandbox,
    CLI-version fingerprinting off, and empty ``features``/``config`` overrides.

    Examples:
    - CodexConfig().model == "gpt-5.6-sol"
    - CodexConfig().reasoning_effort == "medium"
    - CodexConfig().sandbox == "workspace-write"
    - CodexConfig().fingerprint_cli_version == False
    """

    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "medium"
    sandbox: str = "workspace-write"
    # Off by default: embedding `codex --version` in the generation fingerprint
    # couples `jaunt check` to environments that have the codex binary (a CI
    # runner without it resolves "unknown" and restales a byte-identical tree).
    fingerprint_cli_version: bool = False
    features: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DaemonConfig:
    poll_interval: float = 2.0
    max_jobs: int = 0
    notify_command: str = ""
    auto_commit: bool = False


@dataclass(frozen=True)
class SkillsConfig:
    auto: bool = True
    max_chars_per_skill: int = 8000
    inject_user_skills: list[str] = field(default_factory=list)
    builtin: bool = True
    builtin_skills: list[str] = field(default_factory=lambda: list(_default_builtin_skills()))


@dataclass(frozen=True)
class ContextSearchConfig:
    enabled: bool = False
    internal_retrieval: bool = True
    max_hits: int = 8


@dataclass(frozen=True)
class ContextConfig:
    repo_map: bool = True
    repo_map_file: str = "treedocs.yaml"
    enrich: bool = False
    max_chars: int = 6000
    search: ContextSearchConfig = field(default_factory=ContextSearchConfig)
    overview: bool = False


@dataclass(frozen=True)
class ContractConfig:
    battery_dir: str = "tests/contract"
    derive: list[str] = field(default_factory=lambda: ["examples", "errors"])
    strength: bool = True
    property_max_examples: int = 50


@dataclass(frozen=True)
class SemanticGateConfig:
    enabled: bool = True
    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "medium"


@dataclass(frozen=True)
class JauntConfig:
    version: int
    paths: PathsConfig
    llm: LLMConfig
    build: BuildConfig
    test: TestConfig
    prompts: PromptsConfig
    agent: AgentConfig = field(default_factory=AgentConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    contract: ContractConfig = field(default_factory=ContractConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    semantic_gate: SemanticGateConfig = field(default_factory=SemanticGateConfig)
    python_target: PythonTargetConfig | None = None
    typescript_target: TypeScriptTargetConfig | None = None
    typescript_prompts: TypeScriptPromptsConfig = field(default_factory=TypeScriptPromptsConfig)

    @property
    def target_languages(self) -> tuple[str, ...]:
        """Configured target languages in stable display/scheduling order."""

        if self.version == 1:
            return ("py",)
        languages: list[str] = []
        if self.python_target is not None:
            languages.append("py")
        if self.typescript_target is not None:
            languages.append("ts")
        return tuple(languages)


@jaunt.contract
def find_project_root(start: Path) -> Path:
    """Walk upward from ``start`` (a file or directory) looking for ``jaunt.toml``.

    Resolves ``start`` (its parent when ``start`` is a file), then ascends parent
    by parent and returns the first directory that directly contains a
    ``jaunt.toml`` file. When the filesystem root is reached without finding one,
    ``JauntConfigError`` is raised.

    Raises:
    - find_project_root(Path("/")) raises JauntConfigError
    """

    cur = start
    try:
        if cur.is_file():
            cur = cur.parent
    except OSError:
        # If `start` is a broken symlink or otherwise non-stat'able, treat as a
        # path we can still walk from.
        cur = cur.parent

    cur = cur.resolve()
    while True:
        if (cur / "jaunt.toml").is_file():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent

    raise JauntConfigError("Could not find jaunt.toml by walking upward from start path.")


def _as_table(value: Any, *, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise JauntConfigError(f"Expected [{name}] to be a table.")
    return value


def _reject_unknown(tbl: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    """Raise JauntConfigError for any key in `tbl` not present in `allowed`.

    Adds a difflib suggestion when a close match exists. `where` is a human label
    for the table (e.g. "jaunt.toml" for the top level or "[semantic_gate]").
    """
    for key in tbl:
        if key in allowed:
            continue
        matches = difflib.get_close_matches(key, sorted(allowed), n=1)
        if not matches:
            matches = [candidate for candidate in sorted(allowed) if key in candidate.split("_")][
                :1
            ]
        hint = f" — did you mean {matches[0]!r}?" if matches else ""
        raise JauntConfigError(f"unknown key {key!r} in {where}{hint}")


def _as_str_list(value: Any, *, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise JauntConfigError(f"Expected {name} to be a list of strings.")
    return list(value)


def _as_bool(value: Any, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise JauntConfigError(f"Expected {name} to be a boolean.")
    return value


def _as_int(value: Any, *, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise JauntConfigError(f"Expected {name} to be an integer.")
    return value


def _as_str(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise JauntConfigError(f"Expected {name} to be a string.")
    return value


def _as_float(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JauntConfigError(f"Expected {name} to be a number.")
    return float(value)


def _resolve_prompt_override(value: str, *, root: Path) -> str:
    if not value:
        return ""
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((root / path).resolve())


def _validate_ts_relative_path(
    value: str,
    *,
    name: str,
    allow_empty: bool = False,
    allow_current: bool = True,
) -> str:
    """Validate a root-relative POSIX path before it crosses the worker boundary."""

    if not value:
        if allow_empty:
            return value
        raise JauntConfigError(f"Invalid config: {name} must not be empty.")
    path = PurePosixPath(value)
    if "\\" in value or path.is_absolute() or ".." in path.parts:
        raise JauntConfigError(f"Invalid config: {name} must be a safe root-relative POSIX path.")
    if not allow_current and path == PurePosixPath("."):
        raise JauntConfigError(f"Invalid config: {name} must name a child path.")
    return value


def _v2_prompt_table(prompts: dict[str, Any], key: str, allowed: frozenset[str]) -> dict[str, Any]:
    table = _as_table(prompts.get(key), name=f"prompts.{key}")
    _reject_unknown(table, allowed, f"[prompts.{key}]")
    return table


def _normalize_v2_data(
    data: dict[str, Any], *, root: Path
) -> tuple[
    dict[str, Any],
    PythonTargetConfig | None,
    TypeScriptTargetConfig | None,
    TypeScriptPromptsConfig,
]:
    """Validate v2-only structure and return a legacy-shaped Python view.

    Existing Python services intentionally continue to consume ``cfg.paths``,
    ``cfg.build``, ``cfg.test``, and ``cfg.prompts``. Normalizing only that view
    keeps their behavior isolated from TypeScript target semantics.
    """

    _reject_unknown(data, _V2_ALLOWED_SECTIONS, "jaunt.toml")
    target_tbl = _as_table(data.get("target"), name="target")
    _reject_unknown(target_tbl, _V2_TARGET_KEYS, "[target]")
    if not target_tbl:
        raise JauntConfigError(
            "Invalid config: version 2 requires at least one of [target.py] or [target.ts]."
        )

    py_tbl = _as_table(target_tbl.get("py"), name="target.py") if "py" in target_tbl else None
    ts_tbl = _as_table(target_tbl.get("ts"), name="target.ts") if "ts" in target_tbl else None
    if py_tbl is not None:
        _reject_unknown(py_tbl, _V2_PY_TARGET_KEYS, "[target.py]")
    if ts_tbl is not None:
        _reject_unknown(ts_tbl, _V2_TS_TARGET_KEYS, "[target.ts]")

    build_tbl = _as_table(data.get("build"), name="build")
    _reject_unknown(build_tbl, _V2_BUILD_KEYS, "[build]")
    test_tbl = _as_table(data.get("test"), name="test")
    _reject_unknown(test_tbl, _V2_TEST_KEYS, "[test]")
    prompts_tbl = _as_table(data.get("prompts"), name="prompts")
    _reject_unknown(prompts_tbl, _V2_PROMPTS_KEYS, "[prompts]")
    prompts_py = _v2_prompt_table(prompts_tbl, "py", _V2_PROMPTS_PY_KEYS)
    prompts_ts = _v2_prompt_table(prompts_tbl, "ts", _V2_PROMPTS_TS_KEYS)

    # Validate all unchanged shared tables now. The existing parser validates
    # their values after this function converts the target-local Python fields.
    shared_allowlists = {
        "llm": _LLM_KEYS,
        "agent": _AGENT_KEYS,
        "codex": _CODEX_KEYS,
        "daemon": _DAEMON_KEYS,
        "skills": _SKILLS_KEYS,
        "contract": _V2_CONTRACT_KEYS,
        "semantic_gate": _SEMANTIC_GATE_KEYS,
        "context": _CONTEXT_KEYS,
    }
    for section, allowed in shared_allowlists.items():
        table = _as_table(data.get(section), name=section)
        _reject_unknown(table, allowed, f"[{section}]")
    context_tbl = _as_table(data.get("context"), name="context")
    search_tbl = _as_table(context_tbl.get("search"), name="context.search")
    _reject_unknown(search_tbl, _CONTEXT_SEARCH_KEYS, "[context.search]")

    python_target: PythonTargetConfig | None = None
    if py_tbl is not None:
        source_roots = _as_str_list(
            py_tbl.get("source_roots", ["src", "."]), name="target.py.source_roots"
        )
        test_roots = _as_str_list(py_tbl.get("test_roots", ["tests"]), name="target.py.test_roots")
        generated_dir = _as_str(
            py_tbl.get("generated_dir", "__generated__"), name="target.py.generated_dir"
        )
        python_target = PythonTargetConfig(
            source_roots=source_roots,
            test_roots=test_roots,
            generated_dir=generated_dir,
            infer_deps=_as_bool(py_tbl.get("infer_deps", True), name="target.py.infer_deps"),
            test_infer_deps=_as_bool(
                py_tbl.get("test_infer_deps", True), name="target.py.test_infer_deps"
            ),
            emit_stubs=_as_bool(py_tbl.get("emit_stubs", True), name="target.py.emit_stubs"),
            ty_retry_attempts=_as_int(
                py_tbl.get("ty_retry_attempts", 1), name="target.py.ty_retry_attempts"
            ),
            async_runner=_as_str(
                py_tbl.get("async_runner", "asyncio"), name="target.py.async_runner"
            ),
            check_generated_imports=_as_bool(
                py_tbl.get("check_generated_imports", True),
                name="target.py.check_generated_imports",
            ),
            generated_import_allowlist=_as_str_list(
                py_tbl.get("generated_import_allowlist", []),
                name="target.py.generated_import_allowlist",
            ),
            pytest_args=_as_str_list(
                py_tbl.get("pytest_args", ["-q"]), name="target.py.pytest_args"
            ),
            auto_class_tests=_as_bool(
                py_tbl.get("auto_class_tests", False), name="target.py.auto_class_tests"
            ),
            contract_battery_dir=_as_str(
                py_tbl.get("contract_battery_dir", "tests/contract"),
                name="target.py.contract_battery_dir",
            ),
        )
        if not source_roots:
            raise JauntConfigError("Invalid config: target.py.source_roots must not be empty.")

    typescript_target: TypeScriptTargetConfig | None = None
    if ts_tbl is not None:
        ts_source_roots = _as_str_list(
            ts_tbl.get("source_roots", ["src"]), name="target.ts.source_roots"
        )
        ts_test_roots = _as_str_list(
            ts_tbl.get("test_roots", ["tests"]), name="target.ts.test_roots"
        )
        ts_projects = _as_str_list(
            ts_tbl.get("projects", ["tsconfig.json"]), name="target.ts.projects"
        )
        ts_test_projects = _as_str_list(
            ts_tbl.get("test_projects", []), name="target.ts.test_projects"
        )
        fast_check_runs = _as_int(
            ts_tbl.get("fast_check_runs", 50), name="target.ts.fast_check_runs"
        )
        test_runner = _as_str(ts_tbl.get("test_runner", "vitest"), name="target.ts.test_runner")
        if not ts_source_roots:
            raise JauntConfigError("Invalid config: target.ts.source_roots must not be empty.")
        if not ts_projects:
            raise JauntConfigError("Invalid config: target.ts.projects must not be empty.")
        if fast_check_runs < 1:
            raise JauntConfigError(
                "Invalid config: target.ts.fast_check_runs must be a positive integer."
            )
        if test_runner != "vitest":
            raise JauntConfigError(
                "Invalid config: target.ts.test_runner must be 'vitest' in the initial "
                "TypeScript target."
            )
        for index, value in enumerate(ts_source_roots):
            _validate_ts_relative_path(value, name=f"target.ts.source_roots[{index}]")
        for index, value in enumerate(ts_test_roots):
            _validate_ts_relative_path(value, name=f"target.ts.test_roots[{index}]")
        for index, value in enumerate(ts_projects):
            _validate_ts_relative_path(value, name=f"target.ts.projects[{index}]")
        for index, value in enumerate(ts_test_projects):
            _validate_ts_relative_path(value, name=f"target.ts.test_projects[{index}]")
        tool_owner = _validate_ts_relative_path(
            _as_str(ts_tbl.get("tool_owner", "."), name="target.ts.tool_owner"),
            name="target.ts.tool_owner",
        )
        generated_dir = _validate_ts_relative_path(
            _as_str(ts_tbl.get("generated_dir", "__generated__"), name="target.ts.generated_dir"),
            name="target.ts.generated_dir",
            allow_current=False,
        )
        vitest_config = _validate_ts_relative_path(
            _as_str(ts_tbl.get("vitest_config", ""), name="target.ts.vitest_config"),
            name="target.ts.vitest_config",
            allow_empty=True,
        )
        vitest_args = _as_str_list(ts_tbl.get("vitest_args", []), name="target.ts.vitest_args")
        if vitest_args:
            raise JauntConfigError(
                "Invalid config: target.ts.vitest_args is not supported by the protected "
                "Vitest runner; configure Vitest through target.ts.vitest_config instead."
            )
        contract_battery_dir = _validate_ts_relative_path(
            _as_str(
                ts_tbl.get("contract_battery_dir", "tests/contract"),
                name="target.ts.contract_battery_dir",
            ),
            name="target.ts.contract_battery_dir",
            allow_current=False,
        )
        typescript_target = TypeScriptTargetConfig(
            source_roots=ts_source_roots,
            test_roots=ts_test_roots,
            projects=ts_projects,
            test_projects=ts_test_projects,
            tool_owner=tool_owner,
            generated_dir=generated_dir,
            test_runner=test_runner,
            vitest_config=vitest_config,
            vitest_args=vitest_args,
            auto_skills=(
                _as_bool(ts_tbl["auto_skills"], name="target.ts.auto_skills")
                if "auto_skills" in ts_tbl
                else None
            ),
            auto_class_tests=_as_bool(
                ts_tbl.get("auto_class_tests", False), name="target.ts.auto_class_tests"
            ),
            fast_check_runs=fast_check_runs,
            contract_battery_dir=contract_battery_dir,
        )

    def _prompt_value(table: dict[str, Any], key: str, name: str) -> str:
        return _resolve_prompt_override(
            _as_str(table.get(key, ""), name=name),
            root=root,
        )

    typescript_prompts = TypeScriptPromptsConfig(
        build_system=_prompt_value(prompts_ts, "build_system", "prompts.ts.build_system"),
        build_module=_prompt_value(prompts_ts, "build_module", "prompts.ts.build_module"),
        test_system=_prompt_value(prompts_ts, "test_system", "prompts.ts.test_system"),
        test_module=_prompt_value(prompts_ts, "test_module", "prompts.ts.test_module"),
        design_system=_prompt_value(prompts_ts, "design_system", "prompts.ts.design_system"),
        design_user=_prompt_value(prompts_ts, "design_user", "prompts.ts.design_user"),
    )

    normalized: dict[str, Any] = {
        key: value
        for key, value in data.items()
        if key
        in {
            "llm",
            "agent",
            "codex",
            "daemon",
            "skills",
            "contract",
            "semantic_gate",
            "context",
        }
    }
    normalized["version"] = 1
    normalized["paths"] = {
        "source_roots": python_target.source_roots if python_target else [],
        "test_roots": python_target.test_roots if python_target else [],
        "generated_dir": python_target.generated_dir if python_target else "__generated__",
    }
    normalized["build"] = dict(build_tbl)
    normalized["test"] = dict(test_tbl)
    normalized["prompts"] = dict(prompts_py)
    if python_target is not None:
        normalized["build"].update(
            {
                "infer_deps": python_target.infer_deps,
                "ty_retry_attempts": python_target.ty_retry_attempts,
                "async_runner": python_target.async_runner,
                "check_generated_imports": python_target.check_generated_imports,
                "generated_import_allowlist": python_target.generated_import_allowlist,
                "emit_stubs": python_target.emit_stubs,
            }
        )
        normalized["test"].update(
            {
                "infer_deps": python_target.test_infer_deps,
                "pytest_args": python_target.pytest_args,
                "auto_class_tests": python_target.auto_class_tests,
            }
        )
        contract = dict(normalized.get("contract", {}))
        contract["battery_dir"] = python_target.contract_battery_dir
        normalized["contract"] = contract
    return normalized, python_target, typescript_target, typescript_prompts


@jaunt.contract
def load_config(*, root: Path | None = None, config_path: Path | None = None) -> JauntConfig:
    """Load and validate ``jaunt.toml``.

    If neither ``root`` nor ``config_path`` are provided, the project root is
    discovered by walking upward from the current working directory. When
    ``config_path`` points at a file that does not exist, ``JauntConfigError`` is
    raised (wrapping the underlying ``FileNotFoundError``). Unknown sections or
    keys, an invalid TOML body, or a missing/unsupported ``version`` likewise
    raise ``JauntConfigError``.

    Raises:
    - load_config(config_path=Path("/nonexistent/jaunt.toml")) raises JauntConfigError
    """

    if config_path is None:
        if root is None:
            root = find_project_root(Path.cwd())
        config_path = root / "jaunt.toml"
    else:
        if root is None:
            root = config_path.parent

    assert root is not None
    root = root.resolve()
    config_path = config_path.resolve()

    try:
        raw = config_path.read_bytes()
    except FileNotFoundError as e:
        raise JauntConfigError(f"Missing jaunt.toml at: {config_path}") from e
    except OSError as e:
        raise JauntConfigError(f"Failed reading config file: {config_path}") from e

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise JauntConfigError(f"Config is not valid UTF-8: {config_path}") from e
    except tomllib.TOMLDecodeError as e:
        raise JauntConfigError(f"Invalid TOML in {config_path}: {e}") from e

    version = data.get("version", None)
    if version is None:
        raise JauntConfigError("Missing required `version = 1` in jaunt.toml.")
    version_i = _as_int(version, name="version")
    python_target: PythonTargetConfig | None = None
    typescript_target: TypeScriptTargetConfig | None = None
    typescript_prompts = TypeScriptPromptsConfig()
    if version_i == 2:
        data, python_target, typescript_target, typescript_prompts = _normalize_v2_data(
            data, root=root
        )
    elif version_i == 1:
        _reject_unknown(data, _ALLOWED_SECTIONS, "jaunt.toml")
    else:
        raise JauntConfigError(f"Unsupported config version: {version_i} (expected 1 or 2).")

    paths_tbl = _as_table(data.get("paths"), name="paths")
    _reject_unknown(paths_tbl, _PATHS_KEYS, "[paths]")
    llm_tbl = _as_table(data.get("llm"), name="llm")
    _reject_unknown(llm_tbl, _LLM_KEYS, "[llm]")
    build_tbl = _as_table(data.get("build"), name="build")
    _reject_unknown(build_tbl, _BUILD_KEYS, "[build]")
    test_tbl = _as_table(data.get("test"), name="test")
    _reject_unknown(test_tbl, _TEST_KEYS, "[test]")
    prompts_tbl = _as_table(data.get("prompts"), name="prompts")
    _reject_unknown(prompts_tbl, _PROMPTS_KEYS, "[prompts]")
    agent_tbl = _as_table(data.get("agent"), name="agent")
    _reject_unknown(agent_tbl, _AGENT_KEYS, "[agent]")
    codex_tbl = _as_table(data.get("codex"), name="codex")
    _reject_unknown(codex_tbl, _CODEX_KEYS, "[codex]")
    daemon_tbl = _as_table(data.get("daemon"), name="daemon")
    _reject_unknown(daemon_tbl, _DAEMON_KEYS, "[daemon]")
    skills_tbl = _as_table(data.get("skills"), name="skills")
    _reject_unknown(skills_tbl, _SKILLS_KEYS, "[skills]")
    contract_tbl = _as_table(data.get("contract"), name="contract")
    _reject_unknown(contract_tbl, _CONTRACT_KEYS, "[contract]")
    semantic_gate_tbl = _as_table(data.get("semantic_gate"), name="semantic_gate")
    _reject_unknown(semantic_gate_tbl, _SEMANTIC_GATE_KEYS, "[semantic_gate]")

    if "source_roots" in paths_tbl:
        source_roots = _as_str_list(paths_tbl["source_roots"], name="paths.source_roots")
    else:
        source_roots = ["src", "."]

    if "test_roots" in paths_tbl:
        test_roots = _as_str_list(paths_tbl["test_roots"], name="paths.test_roots")
    else:
        test_roots = ["tests"]

    if "generated_dir" in paths_tbl:
        generated_dir = _as_str(paths_tbl["generated_dir"], name="paths.generated_dir")
    else:
        generated_dir = "__generated__"

    if "provider" in llm_tbl:
        provider = _as_str(llm_tbl["provider"], name="llm.provider")
    else:
        provider = "openai"

    if "model" in llm_tbl:
        model = _as_str(llm_tbl["model"], name="llm.model")
    else:
        model = "gpt-5.2"

    if "api_key_env" in llm_tbl:
        api_key_env = _as_str(llm_tbl["api_key_env"], name="llm.api_key_env")
    else:
        api_key_env = "OPENAI_API_KEY"

    max_cost_per_build: float | None = None
    if "max_cost_per_build" in llm_tbl:
        max_cost_per_build = _as_float(llm_tbl["max_cost_per_build"], name="llm.max_cost_per_build")

    reasoning_effort: str | None = None
    if "reasoning_effort" in llm_tbl:
        reasoning_effort = _as_str(llm_tbl["reasoning_effort"], name="llm.reasoning_effort").strip()
        if not reasoning_effort:
            reasoning_effort = None

    anthropic_thinking_budget_tokens: int | None = None
    if "anthropic_thinking_budget_tokens" in llm_tbl:
        anthropic_thinking_budget_tokens = _as_int(
            llm_tbl["anthropic_thinking_budget_tokens"],
            name="llm.anthropic_thinking_budget_tokens",
        )

    if "prompt_cache" in llm_tbl:
        prompt_cache = _as_bool(llm_tbl["prompt_cache"], name="llm.prompt_cache")
    else:
        prompt_cache = False

    if "prompt_cache_key" in llm_tbl:
        prompt_cache_key = _as_str(llm_tbl["prompt_cache_key"], name="llm.prompt_cache_key")
    else:
        prompt_cache_key = ""

    if "jobs" in build_tbl:
        build_jobs = _as_int(build_tbl["jobs"], name="build.jobs")
    else:
        build_jobs = 8

    if "infer_deps" in build_tbl:
        build_infer_deps = _as_bool(build_tbl["infer_deps"], name="build.infer_deps")
    else:
        build_infer_deps = True

    if "ty_retry_attempts" in build_tbl:
        build_ty_retry_attempts = _as_int(
            build_tbl["ty_retry_attempts"], name="build.ty_retry_attempts"
        )
    else:
        build_ty_retry_attempts = 1

    if "async_runner" in build_tbl:
        async_runner = _as_str(build_tbl["async_runner"], name="build.async_runner")
    else:
        async_runner = "asyncio"

    if "include_target_tests" in build_tbl:
        include_target_tests = _as_bool(
            build_tbl["include_target_tests"],
            name="build.include_target_tests",
        )
    else:
        include_target_tests = False

    if "check_generated_imports" in build_tbl:
        check_generated_imports = _as_bool(
            build_tbl["check_generated_imports"],
            name="build.check_generated_imports",
        )
    else:
        check_generated_imports = True

    if "generated_import_allowlist" in build_tbl:
        generated_import_allowlist = _as_str_list(
            build_tbl["generated_import_allowlist"],
            name="build.generated_import_allowlist",
        )
    else:
        generated_import_allowlist = []

    if "instructions" in build_tbl:
        build_instructions = _as_str_list(build_tbl["instructions"], name="build.instructions")
    else:
        build_instructions = []

    if "emit_stubs" in build_tbl:
        emit_stubs = _as_bool(build_tbl["emit_stubs"], name="build.emit_stubs")
    else:
        emit_stubs = True

    if "jobs" in test_tbl:
        test_jobs = _as_int(test_tbl["jobs"], name="test.jobs")
    else:
        test_jobs = 4

    if "infer_deps" in test_tbl:
        test_infer_deps = _as_bool(test_tbl["infer_deps"], name="test.infer_deps")
    else:
        test_infer_deps = True

    if "pytest_args" in test_tbl:
        pytest_args = _as_str_list(test_tbl["pytest_args"], name="test.pytest_args")
    else:
        pytest_args = ["-q"]

    if "auto_class_tests" in test_tbl:
        auto_class_tests = _as_bool(test_tbl["auto_class_tests"], name="test.auto_class_tests")
    else:
        auto_class_tests = False

    if "build_system" in prompts_tbl:
        build_system = _resolve_prompt_override(
            _as_str(prompts_tbl["build_system"], name="prompts.build_system"),
            root=root,
        )
    else:
        build_system = ""

    if "build_preamble" in prompts_tbl:
        build_preamble = _resolve_prompt_override(
            _as_str(prompts_tbl["build_preamble"], name="prompts.build_preamble"),
            root=root,
        )
    else:
        build_preamble = ""

    if "build_module" in prompts_tbl:
        build_module = _resolve_prompt_override(
            _as_str(prompts_tbl["build_module"], name="prompts.build_module"),
            root=root,
        )
    else:
        build_module = ""

    if "test_system" in prompts_tbl:
        test_system = _resolve_prompt_override(
            _as_str(prompts_tbl["test_system"], name="prompts.test_system"),
            root=root,
        )
    else:
        test_system = ""

    if "test_module" in prompts_tbl:
        test_module = _resolve_prompt_override(
            _as_str(prompts_tbl["test_module"], name="prompts.test_module"),
            root=root,
        )
    else:
        test_module = ""

    if "project_overview_system" in prompts_tbl:
        project_overview_system = _resolve_prompt_override(
            _as_str(prompts_tbl["project_overview_system"], name="prompts.project_overview_system"),
            root=root,
        )
    else:
        project_overview_system = ""

    if "project_overview_user" in prompts_tbl:
        project_overview_user = _resolve_prompt_override(
            _as_str(prompts_tbl["project_overview_user"], name="prompts.project_overview_user"),
            root=root,
        )
    else:
        project_overview_user = ""

    if "engine" in agent_tbl:
        agent_engine = _as_str(agent_tbl["engine"], name="agent.engine").strip()
    else:
        agent_engine = "codex"

    if "model" in codex_tbl:
        codex_model = _as_str(codex_tbl["model"], name="codex.model")
    else:
        codex_model = "gpt-5.6-sol"

    if "reasoning_effort" in codex_tbl:
        codex_reasoning_effort = _as_str(
            codex_tbl["reasoning_effort"], name="codex.reasoning_effort"
        ).strip()
    else:
        codex_reasoning_effort = "medium"

    if "sandbox" in codex_tbl:
        codex_sandbox = _as_str(codex_tbl["sandbox"], name="codex.sandbox").strip()
    else:
        codex_sandbox = "workspace-write"

    if "fingerprint_cli_version" in codex_tbl:
        codex_fingerprint_cli_version = _as_bool(
            codex_tbl["fingerprint_cli_version"],
            name="codex.fingerprint_cli_version",
        )
    else:
        codex_fingerprint_cli_version = False

    if "features" in codex_tbl:
        codex_features = _as_str_list(codex_tbl["features"], name="codex.features")
    else:
        codex_features = []

    if "config" in codex_tbl:
        codex_config = _as_table(codex_tbl["config"], name="codex.config")
    else:
        codex_config = {}

    if "poll_interval" in daemon_tbl:
        daemon_poll_interval = _as_float(daemon_tbl["poll_interval"], name="daemon.poll_interval")
    else:
        daemon_poll_interval = 2.0

    if "max_jobs" in daemon_tbl:
        daemon_max_jobs = _as_int(daemon_tbl["max_jobs"], name="daemon.max_jobs")
    else:
        daemon_max_jobs = 0

    if "notify_command" in daemon_tbl:
        daemon_notify_command = _as_str(daemon_tbl["notify_command"], name="daemon.notify_command")
    else:
        daemon_notify_command = ""

    if "auto_commit" in daemon_tbl:
        daemon_auto_commit = _as_bool(daemon_tbl["auto_commit"], name="daemon.auto_commit")
    else:
        daemon_auto_commit = False

    if "auto" in skills_tbl:
        skills_auto = _as_bool(skills_tbl["auto"], name="skills.auto")
    else:
        skills_auto = True

    if "max_chars_per_skill" in skills_tbl:
        skills_max_chars_per_skill = _as_int(
            skills_tbl["max_chars_per_skill"], name="skills.max_chars_per_skill"
        )
    else:
        skills_max_chars_per_skill = 8000

    if "inject_user_skills" in skills_tbl:
        skills_inject_user = _as_str_list(
            skills_tbl["inject_user_skills"], name="skills.inject_user_skills"
        )
    else:
        skills_inject_user = []

    if "builtin" in skills_tbl:
        skills_builtin = _as_bool(skills_tbl["builtin"], name="skills.builtin")
    else:
        skills_builtin = True

    if "builtin_skills" in skills_tbl:
        skills_builtin_skills = _as_str_list(
            skills_tbl["builtin_skills"], name="skills.builtin_skills"
        )
    else:
        skills_builtin_skills = list(_default_builtin_skills())

    if "battery_dir" in contract_tbl:
        contract_battery_dir = _as_str(contract_tbl["battery_dir"], name="contract.battery_dir")
    else:
        contract_battery_dir = "tests/contract"

    if "derive" in contract_tbl:
        contract_derive = _as_str_list(contract_tbl["derive"], name="contract.derive")
        for entry in contract_derive:
            if entry not in _VALID_DERIVE:
                raise JauntConfigError(
                    f"Invalid config: contract.derive entries must be one of "
                    f"{_VALID_DERIVE!r}, got {entry!r}."
                )
    else:
        contract_derive = ["examples", "errors"]

    if "strength" in contract_tbl:
        contract_strength = _as_bool(contract_tbl["strength"], name="contract.strength")
    else:
        contract_strength = True

    if "property_max_examples" in contract_tbl:
        contract_property_max_examples = _as_int(
            contract_tbl["property_max_examples"], name="contract.property_max_examples"
        )
        if contract_property_max_examples < 1:
            raise JauntConfigError(
                "Invalid config: contract.property_max_examples must be a positive integer."
            )
    else:
        contract_property_max_examples = 50

    context_tbl = _as_table(data.get("context", {}), name="context")
    _reject_unknown(context_tbl, _CONTEXT_KEYS, "[context]")
    if "repo_map" in context_tbl:
        context_repo_map = _as_bool(context_tbl["repo_map"], name="context.repo_map")
    else:
        context_repo_map = True
    if "repo_map_file" in context_tbl:
        context_repo_map_file = _as_str(context_tbl["repo_map_file"], name="context.repo_map_file")
    else:
        context_repo_map_file = "treedocs.yaml"
    if "enrich" in context_tbl:
        context_enrich = _as_bool(context_tbl["enrich"], name="context.enrich")
    else:
        context_enrich = False
    if "max_chars" in context_tbl:
        context_max_chars = _as_int(context_tbl["max_chars"], name="context.max_chars")
    else:
        context_max_chars = 6000

    if "overview" in context_tbl:
        context_overview = _as_bool(context_tbl["overview"], name="context.overview")
    else:
        context_overview = False

    search_tbl = _as_table(context_tbl.get("search", {}), name="context.search")
    _reject_unknown(search_tbl, _CONTEXT_SEARCH_KEYS, "[context.search]")
    if "enabled" in search_tbl:
        search_enabled = _as_bool(search_tbl["enabled"], name="context.search.enabled")
    else:
        search_enabled = False
    if "internal_retrieval" in search_tbl:
        search_internal = _as_bool(
            search_tbl["internal_retrieval"], name="context.search.internal_retrieval"
        )
    else:
        search_internal = True
    if "max_hits" in search_tbl:
        search_max_hits = _as_int(search_tbl["max_hits"], name="context.search.max_hits")
    else:
        search_max_hits = 8

    if "enabled" in semantic_gate_tbl:
        semantic_gate_enabled = _as_bool(semantic_gate_tbl["enabled"], name="semantic_gate.enabled")
    else:
        semantic_gate_enabled = True

    if "model" in semantic_gate_tbl:
        semantic_gate_model = _as_str(semantic_gate_tbl["model"], name="semantic_gate.model")
    else:
        semantic_gate_model = "gpt-5.6-luna"

    if "reasoning_effort" in semantic_gate_tbl:
        semantic_gate_reasoning_effort = _as_str(
            semantic_gate_tbl["reasoning_effort"], name="semantic_gate.reasoning_effort"
        ).strip()
    else:
        semantic_gate_reasoning_effort = "medium"

    # Validation
    def _path_entry_matches(entry: str) -> bool:
        path = Path(entry)
        pattern = str(path if path.is_absolute() else root / path)
        if any(char in entry for char in "*?["):
            return any(Path(match).is_dir() for match in glob.glob(pattern, recursive=True))
        return Path(pattern).exists()

    for entry in source_roots:
        if any(char in entry for char in "*?[") and not _path_entry_matches(entry):
            raise JauntConfigError(
                f"Invalid config: paths.source_roots glob {entry!r} matched no directories."
            )
    for entry in test_roots:
        if any(char in entry for char in "*?[") and not _path_entry_matches(entry):
            raise JauntConfigError(
                f"Invalid config: paths.test_roots glob {entry!r} matched no directories."
            )
    if (version_i == 1 or python_target is not None) and not any(
        _path_entry_matches(sr) for sr in source_roots
    ):
        raise JauntConfigError(
            "Invalid config: none of paths.source_roots exist on disk relative to the project root."
        )

    if (version_i == 1 or python_target is not None) and (
        not generated_dir.isidentifier() or keyword.iskeyword(generated_dir)
    ):
        raise JauntConfigError(
            "Invalid config: paths.generated_dir must be a valid Python identifier."
        )

    if build_jobs < 1 or test_jobs < 1:
        raise JauntConfigError("Invalid config: jobs must be >= 1.")
    if build_ty_retry_attempts < 0:
        raise JauntConfigError("Invalid config: build.ty_retry_attempts must be >= 0.")
    if skills_max_chars_per_skill < 0:
        raise JauntConfigError("Invalid config: skills.max_chars_per_skill must be >= 0.")
    if async_runner not in _VALID_ASYNC_RUNNERS:
        raise JauntConfigError(
            f"Invalid config: build.async_runner must be one of {_VALID_ASYNC_RUNNERS!r}, "
            f"got {async_runner!r}."
        )
    if semantic_gate_reasoning_effort not in _VALID_REASONING_EFFORTS:
        raise JauntConfigError(
            f"Invalid config: semantic_gate.reasoning_effort must be one of "
            f"{_VALID_REASONING_EFFORTS!r}, got {semantic_gate_reasoning_effort!r}."
        )
    if agent_engine not in _VALID_AGENT_ENGINES:
        raise JauntConfigError(
            f"Invalid config: agent.engine must be 'codex' (got {agent_engine!r}). "
            "The 'legacy' and 'aider' engines have been removed; Codex is now the sole "
            "engine. Remove any [agent] engine override and any [aider] table from "
            "jaunt.toml and use [codex] instead."
        )
    if anthropic_thinking_budget_tokens is not None and anthropic_thinking_budget_tokens < 1:
        raise JauntConfigError("Invalid config: llm.anthropic_thinking_budget_tokens must be >= 1.")

    return JauntConfig(
        version=version_i,
        paths=PathsConfig(
            source_roots=source_roots,
            test_roots=test_roots,
            generated_dir=generated_dir,
        ),
        llm=LLMConfig(
            provider=provider,
            model=model,
            api_key_env=api_key_env,
            max_cost_per_build=max_cost_per_build,
            reasoning_effort=reasoning_effort,
            anthropic_thinking_budget_tokens=anthropic_thinking_budget_tokens,
            prompt_cache=prompt_cache,
            prompt_cache_key=prompt_cache_key,
        ),
        build=BuildConfig(
            jobs=build_jobs,
            infer_deps=build_infer_deps,
            ty_retry_attempts=build_ty_retry_attempts,
            async_runner=async_runner,
            include_target_tests=include_target_tests,
            check_generated_imports=check_generated_imports,
            generated_import_allowlist=generated_import_allowlist,
            instructions=build_instructions,
            emit_stubs=emit_stubs,
        ),
        test=TestConfig(
            jobs=test_jobs,
            infer_deps=test_infer_deps,
            pytest_args=pytest_args,
            auto_class_tests=auto_class_tests,
        ),
        prompts=PromptsConfig(
            build_preamble=build_preamble,
            build_system=build_system,
            build_module=build_module,
            test_system=test_system,
            test_module=test_module,
            project_overview_system=project_overview_system,
            project_overview_user=project_overview_user,
        ),
        agent=AgentConfig(engine=agent_engine),
        codex=CodexConfig(
            model=codex_model,
            reasoning_effort=codex_reasoning_effort,
            sandbox=codex_sandbox,
            fingerprint_cli_version=codex_fingerprint_cli_version,
            features=codex_features,
            config=codex_config,
        ),
        daemon=DaemonConfig(
            poll_interval=daemon_poll_interval,
            max_jobs=daemon_max_jobs,
            notify_command=daemon_notify_command,
            auto_commit=daemon_auto_commit,
        ),
        skills=SkillsConfig(
            auto=skills_auto,
            max_chars_per_skill=skills_max_chars_per_skill,
            inject_user_skills=skills_inject_user,
            builtin=skills_builtin,
            builtin_skills=skills_builtin_skills,
        ),
        contract=ContractConfig(
            battery_dir=contract_battery_dir,
            derive=contract_derive,
            strength=contract_strength,
            property_max_examples=contract_property_max_examples,
        ),
        context=ContextConfig(
            repo_map=context_repo_map,
            repo_map_file=context_repo_map_file,
            enrich=context_enrich,
            max_chars=context_max_chars,
            search=ContextSearchConfig(
                enabled=search_enabled,
                internal_retrieval=search_internal,
                max_hits=search_max_hits,
            ),
            overview=context_overview,
        ),
        semantic_gate=SemanticGateConfig(
            enabled=semantic_gate_enabled,
            model=semantic_gate_model,
            reasoning_effort=semantic_gate_reasoning_effort,
        ),
        python_target=python_target,
        typescript_target=typescript_target,
        typescript_prompts=typescript_prompts,
    )

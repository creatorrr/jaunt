"""Project configuration loading for Jaunt.

This module is intentionally small and deterministic: it only reads `jaunt.toml`
and performs light validation/existence checks.
"""

from __future__ import annotations

import keyword
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jaunt.errors import JauntConfigError


@dataclass(frozen=True)
class PathsConfig:
    source_roots: list[str]
    test_roots: list[str]
    generated_dir: str


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
_VALID_AGENT_ENGINES = ("legacy", "aider")
_VALID_AIDER_MODES = ("architect", "code")


@dataclass(frozen=True)
class BuildConfig:
    jobs: int
    infer_deps: bool
    ty_retry_attempts: int = 1
    async_runner: str = "asyncio"


@dataclass(frozen=True)
class TestConfig:
    __test__ = False  # prevent pytest collection

    jobs: int
    infer_deps: bool
    pytest_args: list[str]


@dataclass(frozen=True)
class PromptsConfig:
    build_system: str
    build_module: str
    test_system: str
    test_module: str


@dataclass(frozen=True)
class AgentConfig:
    engine: str = "aider"


@dataclass(frozen=True)
class AiderConfig:
    build_mode: str = "architect"
    test_mode: str = "code"
    skill_mode: str = "code"
    editor_model: str = ""
    map_tokens: int = 0
    save_traces: bool = False


@dataclass(frozen=True)
class JauntConfig:
    version: int
    paths: PathsConfig
    llm: LLMConfig
    build: BuildConfig
    test: TestConfig
    prompts: PromptsConfig
    agent: AgentConfig = field(default_factory=AgentConfig)
    aider: AiderConfig = field(default_factory=AiderConfig)


def find_project_root(start: Path) -> Path:
    """Walk upward from `start` (file or directory) looking for `jaunt.toml`."""

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


def load_config(*, root: Path | None = None, config_path: Path | None = None) -> JauntConfig:
    """Load and validate `jaunt.toml`.

    If neither `root` nor `config_path` are provided, the project root is
    discovered by walking upward from the current working directory.
    """

    if config_path is None:
        if root is None:
            root = find_project_root(Path.cwd())
        config_path = root / "jaunt.toml"
    else:
        if root is None:
            root = config_path.parent

    assert root is not None

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
    if version_i != 1:
        raise JauntConfigError(f"Unsupported config version: {version_i} (expected 1).")

    paths_tbl = _as_table(data.get("paths"), name="paths")
    llm_tbl = _as_table(data.get("llm"), name="llm")
    build_tbl = _as_table(data.get("build"), name="build")
    test_tbl = _as_table(data.get("test"), name="test")
    prompts_tbl = _as_table(data.get("prompts"), name="prompts")
    agent_tbl = _as_table(data.get("agent"), name="agent")
    aider_tbl = _as_table(data.get("aider"), name="aider")

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

    if "build_system" in prompts_tbl:
        build_system = _as_str(prompts_tbl["build_system"], name="prompts.build_system")
    else:
        build_system = ""

    if "build_module" in prompts_tbl:
        build_module = _as_str(prompts_tbl["build_module"], name="prompts.build_module")
    else:
        build_module = ""

    if "test_system" in prompts_tbl:
        test_system = _as_str(prompts_tbl["test_system"], name="prompts.test_system")
    else:
        test_system = ""

    if "test_module" in prompts_tbl:
        test_module = _as_str(prompts_tbl["test_module"], name="prompts.test_module")
    else:
        test_module = ""

    if "engine" in agent_tbl:
        agent_engine = _as_str(agent_tbl["engine"], name="agent.engine").strip()
    else:
        agent_engine = "aider"

    if "build_mode" in aider_tbl:
        aider_build_mode = _as_str(aider_tbl["build_mode"], name="aider.build_mode").strip()
    else:
        aider_build_mode = "architect"

    if "test_mode" in aider_tbl:
        aider_test_mode = _as_str(aider_tbl["test_mode"], name="aider.test_mode").strip()
    else:
        aider_test_mode = "code"

    if "skill_mode" in aider_tbl:
        aider_skill_mode = _as_str(aider_tbl["skill_mode"], name="aider.skill_mode").strip()
    else:
        aider_skill_mode = "code"

    if "editor_model" in aider_tbl:
        aider_editor_model = _as_str(aider_tbl["editor_model"], name="aider.editor_model")
    else:
        aider_editor_model = ""

    if "map_tokens" in aider_tbl:
        aider_map_tokens = _as_int(aider_tbl["map_tokens"], name="aider.map_tokens")
    else:
        aider_map_tokens = 0

    if "save_traces" in aider_tbl:
        aider_save_traces = _as_bool(aider_tbl["save_traces"], name="aider.save_traces")
    else:
        aider_save_traces = False

    # Validation
    if not any((root / sr).exists() for sr in source_roots):
        raise JauntConfigError(
            "Invalid config: none of paths.source_roots exist on disk relative to the project root."
        )

    if not generated_dir.isidentifier() or keyword.iskeyword(generated_dir):
        raise JauntConfigError(
            "Invalid config: paths.generated_dir must be a valid Python identifier."
        )

    if build_jobs < 1 or test_jobs < 1:
        raise JauntConfigError("Invalid config: jobs must be >= 1.")
    if build_ty_retry_attempts < 0:
        raise JauntConfigError("Invalid config: build.ty_retry_attempts must be >= 0.")
    if async_runner not in _VALID_ASYNC_RUNNERS:
        raise JauntConfigError(
            f"Invalid config: build.async_runner must be one of {_VALID_ASYNC_RUNNERS!r}, "
            f"got {async_runner!r}."
        )
    if agent_engine not in _VALID_AGENT_ENGINES:
        raise JauntConfigError(
            f"Invalid config: agent.engine must be one of {_VALID_AGENT_ENGINES!r}, "
            f"got {agent_engine!r}."
        )
    for key, value in (
        ("aider.build_mode", aider_build_mode),
        ("aider.test_mode", aider_test_mode),
        ("aider.skill_mode", aider_skill_mode),
    ):
        if value not in _VALID_AIDER_MODES:
            raise JauntConfigError(
                f"Invalid config: {key} must be one of {_VALID_AIDER_MODES!r}, got {value!r}."
            )
    if aider_map_tokens < 0:
        raise JauntConfigError("Invalid config: aider.map_tokens must be >= 0.")
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
        ),
        test=TestConfig(jobs=test_jobs, infer_deps=test_infer_deps, pytest_args=pytest_args),
        prompts=PromptsConfig(
            build_system=build_system,
            build_module=build_module,
            test_system=test_system,
            test_module=test_module,
        ),
        agent=AgentConfig(engine=agent_engine),
        aider=AiderConfig(
            build_mode=aider_build_mode,
            test_mode=aider_test_mode,
            skill_mode=aider_skill_mode,
            editor_model=aider_editor_model,
            map_tokens=aider_map_tokens,
            save_traces=aider_save_traces,
        ),
    )

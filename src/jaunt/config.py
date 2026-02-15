"""Project configuration loading for Jaunt.

This module is intentionally small and deterministic: it only reads `jaunt.toml`
and performs light validation/existence checks.
"""

from __future__ import annotations

import keyword
import tomllib
from dataclasses import dataclass
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


@dataclass(frozen=True)
class BuildConfig:
    jobs: int
    infer_deps: bool


@dataclass(frozen=True)
class TestConfig:
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
class MCPConfig:
    enabled: bool


@dataclass(frozen=True)
class JauntConfig:
    version: int
    paths: PathsConfig
    llm: LLMConfig
    build: BuildConfig
    test: TestConfig
    prompts: PromptsConfig
    mcp: MCPConfig


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
    mcp_tbl = _as_table(data.get("mcp"), name="mcp")

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

    if "jobs" in build_tbl:
        build_jobs = _as_int(build_tbl["jobs"], name="build.jobs")
    else:
        build_jobs = 8

    if "infer_deps" in build_tbl:
        build_infer_deps = _as_bool(build_tbl["infer_deps"], name="build.infer_deps")
    else:
        build_infer_deps = True

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

    if "enabled" in mcp_tbl:
        mcp_enabled = _as_bool(mcp_tbl["enabled"], name="mcp.enabled")
    else:
        mcp_enabled = True

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

    return JauntConfig(
        version=version_i,
        paths=PathsConfig(
            source_roots=source_roots,
            test_roots=test_roots,
            generated_dir=generated_dir,
        ),
        llm=LLMConfig(provider=provider, model=model, api_key_env=api_key_env),
        build=BuildConfig(jobs=build_jobs, infer_deps=build_infer_deps),
        test=TestConfig(jobs=test_jobs, infer_deps=test_infer_deps, pytest_args=pytest_args),
        prompts=PromptsConfig(
            build_system=build_system,
            build_module=build_module,
            test_system=test_system,
            test_module=test_module,
        ),
        mcp=MCPConfig(enabled=mcp_enabled),
    )

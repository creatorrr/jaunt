from __future__ import annotations

from pathlib import Path

import pytest

from jaunt.config import load_config
from jaunt.errors import JauntConfigError


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_v1_config_keeps_legacy_target_shape(tmp_path: Path) -> None:
    _write(tmp_path / "jaunt.toml", "version = 1\n")
    (tmp_path / "src").mkdir()

    cfg = load_config(root=tmp_path)

    assert cfg.version == 1
    assert cfg.paths.source_roots == ["src", "."]
    assert cfg.python_target is None
    assert cfg.typescript_target is None
    assert cfg.target_languages == ("py",)


def test_v2_typescript_only_does_not_require_python_roots(tmp_path: Path) -> None:
    _write(
        tmp_path / "jaunt.toml",
        """\
version = 2

[target.ts]
source_roots = ["packages/*/src"]
test_roots = ["packages/*/tests"]
projects = ["tsconfig.json"]
test_projects = ["tsconfig.test.json"]
tool_owner = "."
generated_dir = "__generated__"
auto_skills = false
worker_timeout_seconds = 120
worker_startup_timeout_seconds = 45
""",
    )

    cfg = load_config(root=tmp_path)

    assert cfg.version == 2
    assert cfg.python_target is None
    assert cfg.paths.source_roots == []
    assert cfg.target_languages == ("ts",)
    assert cfg.typescript_target is not None
    assert cfg.typescript_target.projects == ["tsconfig.json"]
    assert cfg.typescript_target.test_projects == ["tsconfig.test.json"]
    assert cfg.typescript_target.auto_skills is False
    assert cfg.typescript_target.auto_skills_enabled(True) is False
    assert cfg.typescript_target.worker_timeout_seconds == 120
    assert cfg.typescript_target.worker_startup_timeout_seconds == 45


def test_v2_mixed_config_builds_exact_python_compatibility_views(tmp_path: Path) -> None:
    (tmp_path / "python-src").mkdir()
    _write(
        tmp_path / "jaunt.toml",
        """\
version = 2

[build]
jobs = 3
include_target_tests = true
instructions = ["Prefer helpers."]

[test]
jobs = 2

[target.py]
source_roots = ["python-src"]
test_roots = ["python-tests"]
generated_dir = "__gen__"
infer_deps = false
test_infer_deps = false
emit_stubs = false
ty_retry_attempts = 2
async_runner = "anyio"
check_generated_imports = false
generated_import_allowlist = ["extra"]
pytest_args = ["-q", "-x"]
auto_class_tests = true
contract_battery_dir = "contract-tests"

[target.ts]
projects = ["tsconfig.json"]
source_roots = ["src"]

[prompts.py]
build_module = "prompts/python.md"

[prompts.ts]
build_module = "prompts/typescript.md"
design_user = "prompts/design.md"
""",
    )

    cfg = load_config(root=tmp_path)

    assert cfg.target_languages == ("py", "ts")
    assert cfg.paths.source_roots == ["python-src"]
    assert cfg.paths.generated_dir == "__gen__"
    assert cfg.build.jobs == 3
    assert cfg.build.infer_deps is False
    assert cfg.build.include_target_tests is True
    assert cfg.build.instructions == ["Prefer helpers."]
    assert cfg.build.ty_retry_attempts == 2
    assert cfg.build.async_runner == "anyio"
    assert cfg.build.check_generated_imports is False
    assert cfg.build.generated_import_allowlist == ["extra"]
    assert cfg.build.emit_stubs is False
    assert cfg.test.jobs == 2
    assert cfg.test.infer_deps is False
    assert cfg.test.pytest_args == ["-q", "-x"]
    assert cfg.test.auto_class_tests is True
    assert cfg.contract.battery_dir == "contract-tests"
    assert cfg.prompts.build_module == str((tmp_path / "prompts/python.md").resolve())
    assert cfg.typescript_prompts.build_module == str(
        (tmp_path / "prompts/typescript.md").resolve()
    )


@pytest.mark.parametrize(
    "body,match",
    [
        ("version = 2\n", "at least one"),
        ("version = 2\n[paths]\nsource_roots=[]\n", "unknown key 'paths'"),
        ("version = 2\n[target.ts]\nprojects=[]\n", "projects must not be empty"),
        ("version = 2\n[target.ts]\nprojects=['x']\nwat=true\n", "unknown key 'wat'"),
        (
            "version = 2\n[target.ts]\nprojects=['x']\nfast_check_runs=0\n",
            "positive integer",
        ),
        (
            "version = 2\n[target.ts]\nprojects=['x']\nworker_timeout_seconds=0\n",
            "worker_timeout_seconds must be finite and positive",
        ),
        (
            "version = 2\n[target.ts]\nprojects=['x']\nworker_startup_timeout_seconds=-1\n",
            "worker_startup_timeout_seconds must be finite and positive",
        ),
        (
            "version = 2\n[target.ts]\nprojects=['x']\nworker_timeout_seconds=nan\n",
            "worker_timeout_seconds must be finite and positive",
        ),
        (
            "version = 2\n[target.ts]\nprojects=['x']\nworker_startup_timeout_seconds=+inf\n",
            "worker_startup_timeout_seconds must be finite and positive",
        ),
        (
            "version = 2\n[target.ts]\nprojects=['x']\n[contract]\nbattery_dir='tests'\n",
            "unknown key 'battery_dir'",
        ),
        (
            "version = 2\n[target.ts]\nprojects=['../outside/tsconfig.json']\n",
            "safe root-relative POSIX path",
        ),
        (
            "version = 2\n[target.ts]\nprojects=['x']\ngenerated_dir='.'\n",
            "must name a child path",
        ),
        (
            "version = 2\n[target.ts]\nprojects=['x']\ntool_owner='/tmp'\n",
            "safe root-relative POSIX path",
        ),
        (
            "version = 2\n[target.ts]\nprojects=['x']\nvitest_args=['--reporter=verbose']\n",
            "vitest_args is not supported",
        ),
    ],
)
def test_v2_config_is_strict(tmp_path: Path, body: str, match: str) -> None:
    _write(tmp_path / "jaunt.toml", body)
    with pytest.raises(JauntConfigError, match=match):
        load_config(root=tmp_path)

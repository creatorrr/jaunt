from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from jaunt.cli import main
from jaunt.config import load_config
from jaunt.errors import JauntConfigError
from jaunt.typescript.migrate import apply_config_v2, plan_config_v2


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    path = tmp_path / "jaunt.toml"
    path.write_text(
        """\
version = 1

[paths]
source_roots = ["src"]
test_roots = ["tests"]
generated_dir = "__gen__"

[build]
jobs = 3
infer_deps = false
ty_retry_attempts = 2
async_runner = "anyio"
include_target_tests = true
check_generated_imports = false
generated_import_allowlist = ["compat"]
instructions = ["Keep it small."]
emit_stubs = false

[test]
jobs = 2
infer_deps = false
pytest_args = ["-q", "-x"]
auto_class_tests = true

[prompts]
build_module = "prompts/build.md"

[contract]
battery_dir = "contract-tests"
derive = ["examples"]
strength = false
property_max_examples = 12

[codex]
model = "gpt-5.6-sol"
config = { model_context_window = 100000 }
""",
        encoding="utf-8",
    )
    return path


def test_config_v2_plan_is_python_neutral_and_idempotent(tmp_path: Path) -> None:
    path = _project(tmp_path)
    before = load_config(root=tmp_path)
    path.chmod(0o640)

    plan = plan_config_v2(tmp_path)

    assert plan.changed is True
    assert path.read_text(encoding="utf-8").startswith("version = 1")
    assert "[target.py]" in plan.source
    assert "test_infer_deps = false" in plan.source
    assert "[prompts.py]" in plan.source
    assert "battery_dir" not in plan.source.split("[contract]", 1)[-1]
    assert apply_config_v2(plan) is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o640

    after = load_config(root=tmp_path)
    assert after.version == 2
    assert after.target_languages == ("py",)
    for name in (
        "paths",
        "llm",
        "build",
        "test",
        "prompts",
        "agent",
        "codex",
        "contract",
        "context",
        "semantic_gate",
    ):
        assert getattr(after, name) == getattr(before, name)
    again = plan_config_v2(tmp_path)
    assert again.changed is False
    assert apply_config_v2(again) is False


def test_config_v2_apply_rejects_a_stale_plan(tmp_path: Path) -> None:
    path = _project(tmp_path)
    plan = plan_config_v2(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    with pytest.raises(JauntConfigError, match="changed after"):
        apply_config_v2(plan)


def test_config_v2_cli_plans_and_applies_without_a_model(tmp_path: Path, capsys) -> None:
    path = _project(tmp_path)

    assert main(["migrate", "--config-v2", "--root", str(tmp_path), "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["migration"] == "config-v2"
    assert preview["changed"] is True
    assert preview["applied"] is False
    assert "[target.py]" in preview["content"]
    assert path.read_text(encoding="utf-8").startswith("version = 1")

    assert (
        main(
            [
                "migrate",
                "--config-v2",
                "--apply",
                "--force",
                "--root",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True
    assert load_config(root=tmp_path).version == 2


def test_config_v2_and_merge_projects_are_mutually_exclusive(tmp_path: Path, capsys) -> None:
    _project(tmp_path)
    assert (
        main(
            [
                "migrate",
                "--config-v2",
                "--merge-projects",
                "--root",
                str(tmp_path),
                "--json",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert "separate migrations" in payload["error"]

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from jaunt import eval as jaunt_eval


def test_parse_compare_targets_requires_provider_model() -> None:
    with pytest.raises(ValueError, match="provider:model"):
        jaunt_eval.parse_compare_targets(["gpt-4o"])


def test_parse_compare_targets_dedupes() -> None:
    got = jaunt_eval.parse_compare_targets(
        [
            "openai:gpt-4o",
            "openai:gpt-4o",
            "anthropic:claude-sonnet-4-5-20250929",
        ]
    )
    assert [(t.provider, t.model) for t in got] == [
        ("openai", "gpt-4o"),
        ("anthropic", "claude-sonnet-4-5-20250929"),
    ]


def test_resolve_eval_targets_compare_wins() -> None:
    got = jaunt_eval.resolve_eval_targets(
        compare_values=["openai:gpt-4o", "anthropic:claude-sonnet-4-5-20250929"],
        provider_override="openai",
        model_override="ignored",
        config_provider="openai",
        config_model="gpt-5.2",
    )
    assert [t.label for t in got] == [
        "openai:gpt-4o",
        "anthropic:claude-sonnet-4-5-20250929",
    ]


def test_resolve_eval_targets_uses_overrides_then_config() -> None:
    got = jaunt_eval.resolve_eval_targets(
        compare_values=[],
        provider_override="anthropic",
        model_override="claude-sonnet-4-5-20250929",
        config_provider="openai",
        config_model="gpt-5.2",
    )
    assert [t.label for t in got] == ["anthropic:claude-sonnet-4-5-20250929"]


def test_load_cases_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown eval case"):
        jaunt_eval.load_cases(["does_not_exist"])


def test_make_run_dir_format(tmp_path: Path) -> None:
    out = jaunt_eval.make_run_dir(tmp_path)
    assert out.parent == tmp_path
    assert "T" in out.name
    assert out.name.endswith("Z")


def test_write_single_target_results_layout(tmp_path: Path) -> None:
    target = jaunt_eval.EvalTarget(provider="openai", model="gpt-4o")
    case = jaunt_eval.EvalCaseResult(
        case_id="simple_function",
        description="desc",
        status="passed",
        duration_sec=0.2,
        skip_reason=None,
        build=jaunt_eval.StepResult(ok=True, exit_code=0, stdout="", stderr="", duration_sec=0.1),
        assertions=jaunt_eval.StepResult(
            ok=True, exit_code=0, stdout="", stderr="", duration_sec=0.05
        ),
        typecheck=jaunt_eval.StepResult(
            ok=True, exit_code=0, stdout="", stderr="", duration_sec=0.05
        ),
        generated_sources={
            "src/app/__generated__/specs.py": "def add(a: int, b: int) -> int:\n    return a + b\n"
        },
    )
    suite = jaunt_eval.EvalSuiteResult(
        target=target,
        started_at="2026-02-15T10:00:00Z",
        finished_at="2026-02-15T10:00:01Z",
        duration_sec=1.0,
        cases=[case],
    )

    run_dir = tmp_path / "out"
    jaunt_eval.write_single_target_results(suite=suite, run_dir=run_dir)

    assert (run_dir / "summary.json").is_file()
    assert (run_dir / "cases" / "simple_function.json").is_file()


def test_write_compare_results_layout(tmp_path: Path) -> None:
    openai = jaunt_eval.EvalSuiteResult(
        target=jaunt_eval.EvalTarget(provider="openai", model="gpt-4o"),
        started_at="2026-02-15T10:00:00Z",
        finished_at="2026-02-15T10:00:05Z",
        duration_sec=5.0,
        cases=[],
    )
    anthropic = jaunt_eval.EvalSuiteResult(
        target=jaunt_eval.EvalTarget(provider="anthropic", model="claude-sonnet-4-5-20250929"),
        started_at="2026-02-15T10:00:00Z",
        finished_at="2026-02-15T10:00:05Z",
        duration_sec=5.0,
        cases=[],
    )
    compare = jaunt_eval.CompareResult(
        started_at="2026-02-15T10:00:00Z",
        finished_at="2026-02-15T10:00:05Z",
        duration_sec=5.0,
        targets=[openai, anthropic],
    )

    run_dir = tmp_path / "compare"
    jaunt_eval.write_compare_results(compare=compare, run_dir=run_dir)

    assert (run_dir / "summary.json").is_file()
    assert (run_dir / "comparison.json").is_file()
    assert (run_dir / "targets" / openai.target.slug / "summary.json").is_file()
    assert (run_dir / "targets" / anthropic.target.slug / "summary.json").is_file()


def test_run_eval_case_skips_when_required_package_missing(monkeypatch) -> None:
    case = jaunt_eval.load_cases(["external_library_pydantic"])[0]
    target = jaunt_eval.EvalTarget(provider="openai", model="gpt-4o")

    monkeypatch.setattr(jaunt_eval, "_module_missing", lambda module_name: True)

    result = jaunt_eval.run_eval_case(target=target, case=case)

    assert result.status == "skipped"
    assert result.skip_reason is not None
    assert "Missing required package" in result.skip_reason


def test_run_eval_case_skips_when_typechecker_missing(monkeypatch) -> None:
    case = jaunt_eval.load_cases(["simple_function"])[0]
    target = jaunt_eval.EvalTarget(provider="openai", model="gpt-4o")

    monkeypatch.setattr(jaunt_eval, "_module_missing", lambda module_name: False)
    monkeypatch.setattr(
        jaunt_eval,
        "_run_build",
        lambda project_root: jaunt_eval.StepResult(
            ok=True, exit_code=0, stdout="", stderr="", duration_sec=0.1
        ),
    )
    monkeypatch.setattr(
        jaunt_eval,
        "_run_assertions",
        lambda project_root, assertion_code: jaunt_eval.StepResult(
            ok=True, exit_code=0, stdout="", stderr="", duration_sec=0.1
        ),
    )
    monkeypatch.setattr(
        jaunt_eval,
        "_run_typecheck",
        lambda project_root: jaunt_eval.StepResult(
            ok=False,
            exit_code=127,
            stdout="",
            stderr="Type checker 'ty' is not installed or importable.",
            duration_sec=0.0,
        ),
    )

    result = jaunt_eval.run_eval_case(target=target, case=case)

    assert result.status == "skipped"
    assert result.skip_reason == "Type checker 'ty' is not installed or importable."
    assert result.build is not None and result.build.ok is True
    assert result.assertions is not None and result.assertions.ok is True
    assert result.typecheck is not None and result.typecheck.exit_code == 127


def test_run_subprocess_timeout_returns_exit_124(tmp_path: Path) -> None:
    result = jaunt_eval._run_subprocess(  # noqa: SLF001 - intentional direct helper coverage
        cmd=[sys.executable, "-c", "import time; time.sleep(0.2)"],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout_sec=0.01,
    )

    assert result.ok is False
    assert result.exit_code == 124
    assert "timed out" in result.stderr.lower()

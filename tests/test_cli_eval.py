from __future__ import annotations

import json
from pathlib import Path

import jaunt.cli
from jaunt import eval as jaunt_eval


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _write(
        root / "jaunt.toml",
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                'source_roots = ["src"]',
                'test_roots = ["tests"]',
                'generated_dir = "__generated__"',
                "",
                "[llm]",
                'provider = "openai"',
                'model = "gpt-5.2"',
                'api_key_env = "OPENAI_API_KEY"',
            ]
        )
        + "\n",
    )
    _write(root / "src" / "app" / "__init__.py", "")
    return root


def test_cmd_eval_single_target_json(monkeypatch, tmp_path: Path, capsys) -> None:
    root = _make_project(tmp_path)

    target = jaunt_eval.EvalTarget(provider="openai", model="gpt-4o")
    suite = jaunt_eval.EvalSuiteResult(
        target=target,
        started_at="2026-02-15T10:00:00Z",
        finished_at="2026-02-15T10:00:02Z",
        duration_sec=2.0,
        cases=[],
    )
    run_dir = tmp_path / "evals" / "2026-02-15T10-00-00Z"

    monkeypatch.setattr(jaunt_eval, "resolve_eval_targets", lambda **kwargs: [target])
    monkeypatch.setattr(jaunt_eval, "load_cases", lambda selected_case_ids: [])
    monkeypatch.setattr(jaunt_eval, "run_eval_suite", lambda **kwargs: suite)
    monkeypatch.setattr(jaunt_eval, "make_run_dir", lambda base_out: run_dir)

    wrote: dict[str, Path] = {}

    def _write_single_target_results(*, suite, run_dir):
        wrote["run_dir"] = run_dir

    monkeypatch.setattr(jaunt_eval, "write_single_target_results", _write_single_target_results)

    args = jaunt.cli.parse_args(["eval", "--root", str(root), "--json"])
    rc = jaunt.cli.cmd_eval(args)

    assert rc == jaunt.cli.EXIT_OK
    assert wrote["run_dir"] == run_dir

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["command"] == "eval"
    assert payload["mode"] == "single"
    assert payload["ok"] is True


def test_cmd_eval_compare_failure_exit_code(monkeypatch, tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    run_dir = tmp_path / "evals" / "2026-02-15T10-00-00Z"

    target_a = jaunt_eval.EvalTarget(provider="openai", model="gpt-4o")
    target_b = jaunt_eval.EvalTarget(provider="anthropic", model="claude-sonnet-4-5-20250929")

    bad_case = jaunt_eval.EvalCaseResult(
        case_id="simple_function",
        description="desc",
        status="failed",
        duration_sec=0.1,
        skip_reason=None,
        build=jaunt_eval.StepResult(ok=False, exit_code=1, stdout="", stderr="", duration_sec=0.1),
        assertions=None,
        typecheck=None,
        generated_sources={},
    )
    suite_a = jaunt_eval.EvalSuiteResult(
        target=target_a,
        started_at="2026-02-15T10:00:00Z",
        finished_at="2026-02-15T10:00:01Z",
        duration_sec=1.0,
        cases=[bad_case],
    )
    suite_b = jaunt_eval.EvalSuiteResult(
        target=target_b,
        started_at="2026-02-15T10:00:00Z",
        finished_at="2026-02-15T10:00:01Z",
        duration_sec=1.0,
        cases=[bad_case],
    )
    compare = jaunt_eval.CompareResult(
        started_at="2026-02-15T10:00:00Z",
        finished_at="2026-02-15T10:00:03Z",
        duration_sec=3.0,
        targets=[suite_a, suite_b],
    )

    monkeypatch.setattr(jaunt_eval, "resolve_eval_targets", lambda **kwargs: [target_a, target_b])
    monkeypatch.setattr(jaunt_eval, "load_cases", lambda selected_case_ids: [])
    monkeypatch.setattr(jaunt_eval, "run_compare", lambda **kwargs: compare)
    monkeypatch.setattr(jaunt_eval, "make_run_dir", lambda base_out: run_dir)
    monkeypatch.setattr(jaunt_eval, "write_compare_results", lambda **kwargs: None)

    args = jaunt.cli.parse_args(
        [
            "eval",
            "--root",
            str(root),
            "--compare",
            "openai:gpt-4o",
            "anthropic:claude-sonnet-4-5-20250929",
        ]
    )
    rc = jaunt.cli.cmd_eval(args)

    assert rc == jaunt.cli.EXIT_GENERATION_ERROR

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jaunt.eval_cases import BuiltinEvalCase, get_builtin_eval_cases

_BUILD_TIMEOUT_S = 300.0
_ASSERT_TIMEOUT_S = 120.0
_TYPECHECK_TIMEOUT_S = 120.0
_EXIT_TYPECHECK_MISSING = 127
_EXIT_TIMEOUT = 124


@dataclass(frozen=True, slots=True)
class EvalTarget:
    provider: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def slug(self) -> str:
        p = self.provider.replace("/", "-").replace(":", "-")
        m = self.model.replace("/", "-").replace(":", "-")
        return f"{p}__{m}"


@dataclass(frozen=True, slots=True)
class StepResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    case_id: str
    description: str
    status: str
    duration_sec: float
    skip_reason: str | None
    build: StepResult | None
    assertions: StepResult | None
    typecheck: StepResult | None
    generated_sources: dict[str, str]


@dataclass(frozen=True, slots=True)
class EvalSuiteResult:
    target: EvalTarget
    started_at: str
    finished_at: str
    duration_sec: float
    cases: list[EvalCaseResult]

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if c.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.cases if c.status == "skipped")

    @property
    def pass_rate(self) -> float:
        scored = self.passed + self.failed
        if scored == 0:
            return 0.0
        return self.passed / scored


@dataclass(frozen=True, slots=True)
class CompareResult:
    started_at: str
    finished_at: str
    duration_sec: float
    targets: list[EvalSuiteResult]

    @property
    def ok(self) -> bool:
        return all(t.failed == 0 for t in self.targets)


def parse_compare_targets(raw_values: list[str]) -> list[EvalTarget]:
    targets: list[EvalTarget] = []
    for value in raw_values:
        token = (value or "").strip()
        if not token:
            continue
        provider, sep, model = token.partition(":")
        if sep != ":" or not provider or not model:
            raise ValueError(f"Invalid --compare target {token!r}. Use explicit 'provider:model'.")
        targets.append(EvalTarget(provider=provider, model=model))

    if not targets:
        raise ValueError("No valid --compare targets provided.")

    deduped: list[EvalTarget] = []
    seen: set[tuple[str, str]] = set()
    for t in targets:
        key = (t.provider, t.model)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(t)
    return deduped


def resolve_eval_targets(
    *,
    compare_values: list[str],
    provider_override: str | None,
    model_override: str | None,
    config_provider: str,
    config_model: str,
) -> list[EvalTarget]:
    if compare_values:
        return parse_compare_targets(compare_values)

    provider = provider_override or config_provider
    model = model_override or config_model
    return [EvalTarget(provider=provider, model=model)]


def load_cases(selected_case_ids: list[str]) -> list[BuiltinEvalCase]:
    cases = get_builtin_eval_cases()
    if not selected_case_ids:
        return cases

    wanted = {case_id.strip() for case_id in selected_case_ids if case_id.strip()}
    by_id = {c.case_id: c for c in cases}
    missing = sorted(wanted - set(by_id))
    if missing:
        raise ValueError(
            "Unknown eval case(s): "
            + ", ".join(missing)
            + ". Use --case with one of: "
            + ", ".join(sorted(by_id))
        )

    return [by_id[c.case_id] for c in cases if c.case_id in wanted]


def run_eval_suite(
    *,
    target: EvalTarget,
    cases: list[BuiltinEvalCase],
) -> EvalSuiteResult:
    started = time.perf_counter()
    started_at = _iso_utc_now()

    results = [run_eval_case(target=target, case=case) for case in cases]

    finished_at = _iso_utc_now()
    duration_sec = round(time.perf_counter() - started, 3)
    return EvalSuiteResult(
        target=target,
        started_at=started_at,
        finished_at=finished_at,
        duration_sec=duration_sec,
        cases=results,
    )


def run_compare(
    *,
    targets: list[EvalTarget],
    cases: list[BuiltinEvalCase],
) -> CompareResult:
    started = time.perf_counter()
    started_at = _iso_utc_now()

    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        futures = [pool.submit(run_eval_suite, target=t, cases=cases) for t in targets]
        suites = [f.result() for f in futures]

    finished_at = _iso_utc_now()
    duration_sec = round(time.perf_counter() - started, 3)
    return CompareResult(
        started_at=started_at,
        finished_at=finished_at,
        duration_sec=duration_sec,
        targets=suites,
    )


def run_eval_case(*, target: EvalTarget, case: BuiltinEvalCase) -> EvalCaseResult:
    started = time.perf_counter()

    missing_pkgs = [pkg for pkg in case.required_packages if _module_missing(pkg)]
    if missing_pkgs:
        return EvalCaseResult(
            case_id=case.case_id,
            description=case.description,
            status="skipped",
            duration_sec=round(time.perf_counter() - started, 3),
            skip_reason=f"Missing required package(s): {', '.join(missing_pkgs)}",
            build=None,
            assertions=None,
            typecheck=None,
            generated_sources={},
        )

    with tempfile.TemporaryDirectory(prefix=f"jaunt-eval-{case.case_id}-") as tmp:
        project_root = Path(tmp).resolve()
        _materialize_case_project(project_root=project_root, target=target, case=case)

        build_step = _run_build(project_root)
        assertion_step: StepResult | None = None
        typecheck_step: StepResult | None = None

        if build_step.ok:
            assertion_step = _run_assertions(project_root, case.assertion_code)
        if build_step.ok and assertion_step is not None and assertion_step.ok:
            typecheck_step = _run_typecheck(project_root)

        generated_sources = _collect_generated_sources(project_root)

        status = "passed"
        skip_reason: str | None = None
        if not build_step.ok or (assertion_step is not None and not assertion_step.ok):
            status = "failed"
        elif typecheck_step is None:
            status = "failed"
        elif typecheck_step.exit_code == _EXIT_TYPECHECK_MISSING:
            status = "skipped"
            skip_reason = "Type checker 'ty' is not installed or importable."
        elif not typecheck_step.ok:
            status = "failed"

        return EvalCaseResult(
            case_id=case.case_id,
            description=case.description,
            status=status,
            duration_sec=round(time.perf_counter() - started, 3),
            skip_reason=skip_reason,
            build=build_step,
            assertions=assertion_step,
            typecheck=typecheck_step,
            generated_sources=generated_sources,
        )


def write_single_target_results(*, suite: EvalSuiteResult, run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    cases_dir = run_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    summary = _suite_summary_dict(suite)
    (run_dir / "summary.json").write_text(_json(summary), encoding="utf-8")

    for case in suite.cases:
        payload = _case_to_dict(case)
        (cases_dir / f"{case.case_id}.json").write_text(_json(payload), encoding="utf-8")


def write_compare_results(*, compare: CompareResult, run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    targets_dir = run_dir / "targets"
    targets_dir.mkdir(parents=True, exist_ok=True)

    for suite in compare.targets:
        target_dir = targets_dir / suite.target.slug
        write_single_target_results(suite=suite, run_dir=target_dir)

    comparison_rows = [_comparison_row(s) for s in compare.targets]
    comparison_payload = {
        "started_at": compare.started_at,
        "finished_at": compare.finished_at,
        "duration_sec": compare.duration_sec,
        "targets": comparison_rows,
    }
    (run_dir / "comparison.json").write_text(_json(comparison_payload), encoding="utf-8")

    summary_payload = {
        "mode": "compare",
        "ok": compare.ok,
        "started_at": compare.started_at,
        "finished_at": compare.finished_at,
        "duration_sec": compare.duration_sec,
        "targets": comparison_rows,
    }
    (run_dir / "summary.json").write_text(_json(summary_payload), encoding="utf-8")


def format_suite_table(suite: EvalSuiteResult) -> str:
    lines = [
        f"Eval target: {suite.target.label}",
        "Cases: "
        f"{suite.total}  Passed: {suite.passed}  Failed: {suite.failed}  Skipped: {suite.skipped}",
        "",
        "case_id                        status   duration_s",
        "--------------------------------------------------",
    ]
    for case in suite.cases:
        lines.append(f"{case.case_id:<30} {case.status:<8} {case.duration_sec:>10.3f}")
    return "\n".join(lines)


def format_compare_table(compare: CompareResult) -> str:
    lines = [
        "Model comparison",
        "",
        "target                              passed failed skipped total pass_rate",
        "--------------------------------------------------------------------------",
    ]
    for suite in compare.targets:
        lines.append(
            f"{suite.target.label:<35} "
            f"{suite.passed:>6} {suite.failed:>6} "
            f"{suite.skipped:>7} {suite.total:>5} {suite.pass_rate:>8.1%}"
        )
    return "\n".join(lines)


def make_run_dir(base_out: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return base_out / stamp


def suite_to_cli_json(*, suite: EvalSuiteResult, run_dir: Path) -> dict[str, Any]:
    return {
        "command": "eval",
        "ok": suite.failed == 0,
        "mode": "single",
        "run_dir": str(run_dir),
        "summary": _suite_summary_dict(suite),
        "cases": [_case_to_dict(c) for c in suite.cases],
    }


def compare_to_cli_json(*, compare: CompareResult, run_dir: Path) -> dict[str, Any]:
    return {
        "command": "eval",
        "ok": compare.ok,
        "mode": "compare",
        "run_dir": str(run_dir),
        "started_at": compare.started_at,
        "finished_at": compare.finished_at,
        "duration_sec": compare.duration_sec,
        "targets": [_comparison_row(s) for s in compare.targets],
    }


def _suite_summary_dict(suite: EvalSuiteResult) -> dict[str, Any]:
    return {
        "target": asdict(suite.target),
        "started_at": suite.started_at,
        "finished_at": suite.finished_at,
        "duration_sec": suite.duration_sec,
        "totals": {
            "total": suite.total,
            "passed": suite.passed,
            "failed": suite.failed,
            "skipped": suite.skipped,
            "pass_rate": suite.pass_rate,
        },
    }


def _case_to_dict(case: EvalCaseResult) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "description": case.description,
        "status": case.status,
        "duration_sec": case.duration_sec,
        "skip_reason": case.skip_reason,
        "build": asdict(case.build) if case.build is not None else None,
        "assertions": asdict(case.assertions) if case.assertions is not None else None,
        "typecheck": asdict(case.typecheck) if case.typecheck is not None else None,
        "generated_sources": case.generated_sources,
    }


def _comparison_row(suite: EvalSuiteResult) -> dict[str, Any]:
    return {
        "target": asdict(suite.target),
        "passed": suite.passed,
        "failed": suite.failed,
        "skipped": suite.skipped,
        "total": suite.total,
        "pass_rate": suite.pass_rate,
        "duration_sec": suite.duration_sec,
    }


def _materialize_case_project(
    *, project_root: Path, target: EvalTarget, case: BuiltinEvalCase
) -> None:
    _write_text(project_root / "jaunt.toml", _render_jaunt_toml(target))

    for relpath, content in case.files.items():
        _write_text(project_root / relpath, content)

    # Keep this importable for any assertion script that uses pytest helpers.
    _write_text(project_root / "tests" / "__init__.py", "")


def _render_jaunt_toml(target: EvalTarget) -> str:
    api_key_env = _default_api_key_env(target.provider)
    return "\n".join(
        [
            "version = 1",
            "",
            "[paths]",
            'source_roots = ["src"]',
            'test_roots = ["tests"]',
            'generated_dir = "__generated__"',
            "",
            "[llm]",
            f'provider = "{target.provider}"',
            f'model = "{target.model}"',
            f'api_key_env = "{api_key_env}"',
            "",
            "[build]",
            "jobs = 1",
            "infer_deps = true",
            "",
            "[test]",
            "jobs = 1",
            "infer_deps = true",
            'pytest_args = ["-q"]',
            "",
        ]
    )


def _default_api_key_env(provider: str) -> str:
    p = provider.strip().lower()
    if p == "openai":
        return "OPENAI_API_KEY"
    if p == "anthropic":
        return "ANTHROPIC_API_KEY"
    if p == "cerebras":
        return "CEREBRAS_API_KEY"
    return f"{provider.upper()}_API_KEY"


def _run_build(project_root: Path) -> StepResult:
    cmd = [
        sys.executable,
        "-m",
        "jaunt",
        "build",
        "--root",
        str(project_root),
        "--force",
        "--no-progress",
    ]
    return _run_subprocess(
        cmd=cmd, cwd=project_root, env=_build_env(project_root), timeout_sec=_BUILD_TIMEOUT_S
    )


def _run_assertions(project_root: Path, assertion_code: str) -> StepResult:
    cmd = [sys.executable, "-c", assertion_code]
    return _run_subprocess(
        cmd=cmd, cwd=project_root, env=_build_env(project_root), timeout_sec=_ASSERT_TIMEOUT_S
    )


def _run_typecheck(project_root: Path) -> StepResult:
    ty_cmd = _resolve_ty_cmd()
    if ty_cmd is None:
        return StepResult(
            ok=False,
            exit_code=_EXIT_TYPECHECK_MISSING,
            stdout="",
            stderr="Type checker 'ty' is not installed or importable.",
            duration_sec=0.0,
        )
    cmd = [*ty_cmd, "check", str(project_root / "src")]
    return _run_subprocess(
        cmd=cmd,
        cwd=project_root,
        env=_build_env(project_root),
        timeout_sec=_TYPECHECK_TIMEOUT_S,
    )


def _resolve_ty_cmd() -> list[str] | None:
    if shutil.which("ty"):
        return ["ty"]

    try:
        import ty  # noqa: F401

        return [sys.executable, "-m", "ty"]
    except Exception:
        return None


def _run_subprocess(
    *,
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_sec: float | None = None,
) -> StepResult:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_sec,
        )
        duration = round(time.perf_counter() - started, 3)
        return StepResult(
            ok=proc.returncode == 0,
            exit_code=int(proc.returncode),
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_sec=duration,
        )
    except subprocess.TimeoutExpired as exc:
        duration = round(time.perf_counter() - started, 3)
        timeout_note = f"Command timed out after {timeout_sec:.1f}s."
        stderr = _to_text(exc.stderr)
        stderr = f"{stderr.rstrip()}\n{timeout_note}\n" if stderr else timeout_note
        return StepResult(
            ok=False,
            exit_code=_EXIT_TIMEOUT,
            stdout=_to_text(exc.stdout),
            stderr=stderr,
            duration_sec=duration,
        )


def _build_env(project_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    repo_src = str(Path(__file__).resolve().parents[1])
    project_src = str(project_root / "src")
    project_dir = str(project_root)

    current = env.get("PYTHONPATH", "")
    pieces = [repo_src, project_src, project_dir]
    if current:
        pieces.extend(part for part in current.split(os.pathsep) if part)

    deduped: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        if piece in seen:
            continue
        deduped.append(piece)
        seen.add(piece)

    env["PYTHONPATH"] = os.pathsep.join(deduped)
    return env


def _collect_generated_sources(project_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for py in (project_root / "src").rglob("*.py"):
        if "__generated__" not in py.parts:
            continue
        rel = py.relative_to(project_root)
        try:
            out[str(rel)] = py.read_text(encoding="utf-8")
        except Exception:
            continue
    return out


def _module_missing(module_name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module_name) is None


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=False)


def _to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value

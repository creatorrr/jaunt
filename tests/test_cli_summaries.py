from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import jaunt.cli
from jaunt.errors import JauntQuotaGenerationError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext, TokenUsage
from test_regressions_review_fixes import (
    GoodBackend,
    _make_cli_test_project,
    _restore_modules,
    _write,
    _write_package_init,
)


class NeedsDepBackend(GeneratorBackend):
    """Emits valid code that inlines undeclared logic behind a JAUNT-NEEDS-DEP marker."""

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None]:
        lines: list[str] = []
        for name in ctx.expected_names:
            lines.append(
                f"def {name}() -> None:\n"
                "    # JAUNT-NEEDS-DEP: util.hashing:stable_hash — inlined a copy\n"
                "    assert True\n"
            )
        return "\n".join(lines).rstrip() + "\n", None


class QuotaAcrossCliPhasesBackend(GeneratorBackend):
    def __init__(self, quota_wait_minutes: float) -> None:
        self._quota_wait_minutes = quota_wait_minutes
        self.calls = {"build": 0, "test": 0}

    @property
    def quota_wait_minutes(self) -> float:
        return self._quota_wait_minutes

    async def generate_module(
        self,
        ctx: ModuleSpecContext,
        *,
        extra_error_context: list[str] | None = None,
    ) -> tuple[str, None]:
        del extra_error_context
        self.calls[ctx.kind] += 1
        if self.calls[ctx.kind] == 1:
            raise JauntQuotaGenerationError(f"{ctx.kind} usage limit")
        lines = [f"def {name}() -> None:\n    assert True\n" for name in ctx.expected_names]
        return "\n".join(lines).rstrip() + "\n", None


def _make_cli_build_project(root: Path) -> tuple[Path, str]:
    project = root / "proj"
    project.mkdir(parents=True, exist_ok=True)
    _write(
        project / "jaunt.toml",
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                'source_roots = ["src"]',
                'test_roots = ["tests"]',
                'generated_dir = "__generated__"',
                "",
            ]
        ),
    )
    _write_package_init(project, "src/app")
    _write(
        project / "src" / "app" / "specs.py",
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import jaunt",
                "",
                "@jaunt.magic()",
                "def generated_smoke() -> None:",
                '    """Generate a no-op smoke function."""',
                '    raise RuntimeError("spec stub")',
                "",
            ]
        ),
    )
    return project, "app"


def test_cli_build_no_specs_skips_auto_skills_and_backend(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project = tmp_path / "no-specs"
    project.mkdir()
    _write(
        project / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
    )
    _write_package_init(project, "src/no_specs_app")
    _write(
        project / "src" / "no_specs_app" / "regular.py",
        "def handwritten() -> int:\n    return 1\n",
    )
    calls = {"skills": 0, "backend": 0}

    async def unexpected_skills(**_kwargs) -> SimpleNamespace:
        calls["skills"] += 1
        raise AssertionError("auto-skills must stay behind the no-spec preflight")

    def unexpected_backend(_cfg) -> GeneratorBackend:
        calls["backend"] += 1
        raise AssertionError("backend must stay behind the no-spec preflight")

    monkeypatch.setattr("jaunt.skills_auto.ensure_pypi_skills", unexpected_skills)
    monkeypatch.setattr(jaunt.cli, "_build_backend", unexpected_backend)
    before = {name: sys.modules.get(name) for name in ("no_specs_app", "no_specs_app.regular")}
    orig_sys_path = list(sys.path)
    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules(["no_specs_app"], before=before)

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert payload["generated"] == []
    assert payload["failed"] == {}
    assert calls == {"skills": 0, "backend": 0}


def test_cli_build_dependency_cycle_skips_auto_skills_and_backend(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project = tmp_path / "cycle"
    project.mkdir()
    _write(
        project / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
    )
    _write_package_init(project, "src/cycle_app")
    _write(
        project / "src" / "cycle_app" / "a.py",
        "import jaunt\n\n"
        '@jaunt.magic(deps="cycle_app.b:b")\n'
        "def a() -> int:\n"
        '    """Return an integer."""\n'
        '    raise RuntimeError("spec stub")\n',
    )
    _write(
        project / "src" / "cycle_app" / "b.py",
        "import jaunt\n\n"
        '@jaunt.magic(deps="cycle_app.a:a")\n'
        "def b() -> int:\n"
        '    """Return an integer."""\n'
        '    raise RuntimeError("spec stub")\n',
    )
    calls = {"skills": 0, "backend": 0}

    async def unexpected_skills(**_kwargs) -> SimpleNamespace:
        calls["skills"] += 1
        raise AssertionError("auto-skills must stay behind cycle detection")

    def unexpected_backend(_cfg) -> GeneratorBackend:
        calls["backend"] += 1
        raise AssertionError("backend must stay behind cycle detection")

    monkeypatch.setattr("jaunt.skills_auto.ensure_pypi_skills", unexpected_skills)
    monkeypatch.setattr(jaunt.cli, "_build_backend", unexpected_backend)
    before = {name: sys.modules.get(name) for name in ("cycle_app", "cycle_app.a", "cycle_app.b")}
    orig_sys_path = list(sys.path)
    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules(["cycle_app"], before=before)

    captured = capsys.readouterr()
    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY
    assert "dependency cycle" in captured.err.casefold()
    assert calls == {"skills": 0, "backend": 0}


def test_cli_test_non_json_prints_generation_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_test_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs_mod": sys.modules.get(f"{prefix}.specs_mod"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["test", "--root", str(project), "--no-build", "--no-run"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    out = capsys.readouterr().out
    assert rc == jaunt.cli.EXIT_OK
    assert "Generated 1 test module(s), skipped 0." in out
    assert "test module(s), skipped" in out


def test_cli_test_json_does_not_print_generation_summary(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project, prefix = _make_cli_test_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs_mod": sys.modules.get(f"{prefix}.specs_mod"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["test", "--root", str(project), "--no-build", "--no-run", "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == jaunt.cli.EXIT_OK
    assert payload == {
        "command": "test",
        "ok": True,
        "exit_code": 0,
        "generation_failed": {},
        "refrozen": [],
    }
    assert "Generated" not in out
    assert "module(s), skipped" not in out


def test_cli_test_shares_remaining_quota_budget_with_build_phase(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project, test_prefix = _make_cli_test_project(tmp_path)
    _write_package_init(project, "src/app")
    _write(
        project / "src" / "app" / "specs.py",
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import jaunt",
                "",
                "@jaunt.magic()",
                "def generated_smoke() -> None:",
                '    """Generate a no-op smoke function."""',
                '    raise RuntimeError("spec stub")',
                "",
            ]
        ),
    )
    before = {
        test_prefix: sys.modules.get(test_prefix),
        f"{test_prefix}.specs_mod": sys.modules.get(f"{test_prefix}.specs_mod"),
        "app": sys.modules.get("app"),
        "app.specs": sys.modules.get("app.specs"),
    }
    orig_sys_path = list(sys.path)
    backends: list[QuotaAcrossCliPhasesBackend] = []
    sleeps: list[float] = []

    def backend_factory(cfg) -> QuotaAcrossCliPhasesBackend:
        backend = QuotaAcrossCliPhasesBackend(cfg.codex.quota_wait_minutes)
        backends.append(backend)
        return backend

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(jaunt.cli, "_build_backend", backend_factory)
    monkeypatch.setattr("jaunt.generate.base.asyncio.sleep", no_sleep)

    try:
        rc = jaunt.cli.main(
            [
                "test",
                "--root",
                str(project),
                "--no-run",
                "--no-cache",
                "--no-semantic-gate",
                "--quota-wait",
                "1.5",
            ]
        )
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([test_prefix, "app"], before=before)

    assert rc == jaunt.cli.EXIT_OK
    assert len(backends) == 1
    assert backends[0].calls == {"build": 2, "test": 2}
    assert sleeps == [60.0, 30.0]


def test_cli_build_shares_quota_budget_with_auto_skill_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    backends: list[QuotaAcrossCliPhasesBackend] = []
    sleeps: list[float] = []
    skill_calls = 0

    def backend_factory(cfg) -> QuotaAcrossCliPhasesBackend:
        backend = QuotaAcrossCliPhasesBackend(cfg.codex.quota_wait_minutes)
        backends.append(backend)
        return backend

    async def ensure_skills(**kwargs) -> SimpleNamespace:
        runner = kwargs["model_call_runner"]

        async def model_call() -> SimpleNamespace:
            nonlocal skill_calls
            skill_calls += 1
            if skill_calls == 1:
                raise JauntQuotaGenerationError("auto-skill usage limit")
            return SimpleNamespace(usage_input=1, usage_output=1, usage_cached=0)

        await runner(model_call)
        return SimpleNamespace(warnings=[])

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(jaunt.cli, "_build_backend", backend_factory)
    monkeypatch.setattr("jaunt.skills_auto.ensure_pypi_skills", ensure_skills)
    monkeypatch.setattr("jaunt.generate.base.asyncio.sleep", no_sleep)

    try:
        rc = jaunt.cli.main(
            [
                "build",
                "--root",
                str(project),
                "--no-cache",
                "--no-semantic-gate",
                "--quota-wait",
                "1.5",
            ]
        )
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    assert rc == jaunt.cli.EXIT_OK
    assert len(backends) == 1
    assert skill_calls == 2
    assert backends[0].calls["build"] == 2
    assert sleeps == [60.0, 30.0]


def test_cli_build_cost_summary_counts_auto_skill_and_generation_once(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)

    class UsageBackend(GoodBackend):
        @property
        def model_name(self) -> str:
            return "gpt-5.6-sol"

        async def generate_module(
            self,
            ctx: ModuleSpecContext,
            *,
            extra_error_context: list[str] | None = None,
        ):
            source, _usage = await super().generate_module(
                ctx,
                extra_error_context=extra_error_context,
            )
            return source, TokenUsage(5, 2, "gpt-5.6-sol", "codex")

    async def ensure_skills(**kwargs) -> SimpleNamespace:
        async def model_call() -> SimpleNamespace:
            return SimpleNamespace(usage_input=3, usage_output=1, usage_cached=0)

        await kwargs["model_call_runner"](model_call)
        return SimpleNamespace(warnings=[])

    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda _cfg: UsageBackend())
    monkeypatch.setattr("jaunt.skills_auto.ensure_pypi_skills", ensure_skills)

    try:
        rc = jaunt.cli.main(
            [
                "build",
                "--root",
                str(project),
                "--json",
                "--no-cache",
                "--no-semantic-gate",
            ]
        )
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert payload["cost"]["api_calls"] == 2
    assert payload["cost"]["prompt_tokens"] == 8
    assert payload["cost"]["completion_tokens"] == 3
    assert payload["cost_by_module"]["auto-skill"]["api_calls"] == 1
    assert payload["cost_by_module"]["app.specs"]["api_calls"] == 1


def test_cli_build_non_json_prints_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project)])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    out = capsys.readouterr().out
    assert rc == jaunt.cli.EXIT_OK
    assert "Built " in out
    assert "module(s), skipped" in out


def test_cli_build_json_with_explicit_plain_progress_keeps_stdout_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json", "--progress", "plain"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == jaunt.cli.EXIT_OK
    assert payload["ok"] is True
    assert "[build] " in captured.err
    assert "[build] app.specs: generating" in captured.err
    assert "[build] 1/1 ok=1 fail=0 app.specs" in captured.err


def test_cli_build_json_includes_context_stats(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert "context_stats" in payload
    assert "lazy-loadable" in payload["context_stats_note"]
    stats = payload["context_stats"]
    assert "app.specs" in stats
    blocks = stats["app.specs"]
    for name in (
        "preamble",
        "system",
        "module_contract",
        "deps",
        "package_context",
        "repo_map",
        "blueprint",
        "skills_workspace",
    ):
        assert name in blocks, name
        assert set(blocks[name]) == {"chars", "est_tokens"}
        assert blocks[name]["est_tokens"] == blocks[name]["chars"] // 4


def test_cli_build_json_includes_needs_deps(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: NeedsDepBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert "needs_deps" in payload
    assert "app.specs" in payload["needs_deps"]
    markers = payload["needs_deps"]["app.specs"]
    assert any("util.hashing:stable_hash" in m for m in markers)


def test_cli_build_json_omits_needs_deps_when_none(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert "needs_deps" not in payload


def test_cli_build_non_json_prints_context_line(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project)])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    out = capsys.readouterr().out
    assert rc == jaunt.cli.EXIT_OK
    assert "context:" in out
    assert "app.specs" in out


def test_context_summary_does_not_count_seeded_skills_as_prompt_tokens() -> None:
    line = jaunt.cli._context_stats_summary_line(
        "app.specs",
        {
            "module_contract": {"chars": 400, "est_tokens": 100},
            "skills_workspace_seeded": {"chars": 200_000, "est_tokens": 50_000},
            "skills_workspace": {"chars": 200_000, "est_tokens": 50_000},
        },
    )

    assert "context: 400 chars (~100 tok)" in line
    assert "skills seeded on disk: 200k chars" in line
    assert "not prompt tokens" in line


def test_cost_by_module_counts_retry_calls_without_double_counting_tokens() -> None:
    from jaunt.cost import CostTracker
    from jaunt.generate.base import TokenUsage

    tracker = CostTracker()
    tracker.record("pkg.a", TokenUsage(20, 10, "gpt-5.6-sol", "openai"))
    tracker.record("pkg.a", TokenUsage(0, 0, "gpt-5.6-sol", "openai"))
    tracker.record("pkg.b", TokenUsage(7, 3, "gpt-5.6-sol", "openai"))

    costs = jaunt.cli._cost_by_module(tracker)

    assert costs["pkg.a"]["api_calls"] == 2
    assert costs["pkg.a"]["total_tokens"] == 30
    assert costs["pkg.b"]["api_calls"] == 1
    assert costs["pkg.b"]["total_tokens"] == 10


def test_interrupted_build_json_reports_completed_provider_usage(monkeypatch, capsys) -> None:
    from jaunt.cost import CostTracker
    from jaunt.generate.base import TokenUsage

    async def interrupted(args):
        tracker = CostTracker()
        tracker.record("pkg.mod", TokenUsage(20, 5, "gpt-5.6-sol", "openai"))
        args._cost_trackers_py.append(tracker)
        raise KeyboardInterrupt

    monkeypatch.setattr(jaunt.cli, "_typescript_command_context", lambda _args: None)
    monkeypatch.setattr(jaunt.cli, "_cmd_build_async", interrupted)
    args = jaunt.cli.parse_args(["build", "--json"])

    rc = jaunt.cli.cmd_build(args)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 130
    assert payload["interrupted"] is True
    assert payload["cost"]["api_calls"] == 1
    assert payload["cost"]["total_tokens"] == 25
    assert "completed provider attempts" in payload["cost_note"]

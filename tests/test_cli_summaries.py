from __future__ import annotations

import json
import sys
from pathlib import Path

import jaunt.cli
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
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

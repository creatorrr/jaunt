from __future__ import annotations

import json
import sys
from pathlib import Path

import jaunt.cli
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from test_cli_summaries import _make_cli_build_project
from test_regressions_review_fixes import GoodBackend, _restore_modules

ADVISORY_TEXT = "spec of f is ambiguous about None"
MULTILINE_ADVISORY = "first line of a concern\n  spilling onto a second line"


class AdvisoryBackend(GeneratorBackend):
    """Emits valid code plus a per-module advisories tuple (3-tuple return)."""

    def __init__(self, advisories: tuple[str, ...] = (ADVISORY_TEXT,)) -> None:
        self._advisories = advisories

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None, tuple[str, ...]]:
        lines: list[str] = []
        for name in ctx.expected_names:
            lines.append(f"def {name}() -> None:\n    assert True\n")
        return "\n".join(lines).rstrip() + "\n", None, self._advisories


def _run_build(project: Path, prefix: str, backend: GeneratorBackend, *extra: str) -> str:
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    import jaunt.cli as _cli

    orig = _cli._build_backend
    _cli._build_backend = lambda cfg: backend  # type: ignore[assignment]
    try:
        rc = jaunt.cli.main(["build", "--root", str(project), *extra])
    finally:
        _cli._build_backend = orig
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)
    return str(rc)


def test_build_report_collects_advisories_per_module(tmp_path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: AdvisoryBackend())
    rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert payload["advisories"] == {"app.specs": [ADVISORY_TEXT]}


def test_skipped_module_reports_no_advisories(tmp_path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: AdvisoryBackend())
    # First build: generates, reports advisories.
    jaunt.cli.main(["build", "--root", str(project), "--json"])
    capsys.readouterr()
    # Second build: module is fresh -> skipped -> no advisories re-printed.
    rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert payload["skipped"] == ["app.specs"]
    assert "advisories" not in payload


def test_build_json_payload_has_advisories_key_only_when_nonempty(
    tmp_path, monkeypatch, capsys
) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())
    rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert "advisories" not in payload


def test_build_non_json_prints_advisories_section(tmp_path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: AdvisoryBackend())
    rc = jaunt.cli.main(["build", "--root", str(project)])
    out = capsys.readouterr().out
    assert rc == jaunt.cli.EXIT_OK
    assert "Advisories" in out
    assert f"app.specs: {ADVISORY_TEXT}" in out


def test_journal_gets_one_line_per_advisory_flattened(tmp_path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    # Opt into the journal by creating JAUNT_LOG.
    (project / "JAUNT_LOG").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        jaunt.cli, "_build_backend", lambda cfg: AdvisoryBackend((MULTILINE_ADVISORY,))
    )
    rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    assert rc == jaunt.cli.EXIT_OK
    log = (project / "JAUNT_LOG").read_text(encoding="utf-8")
    advisory_lines = [ln for ln in log.splitlines() if "advisory" in ln]
    assert len(advisory_lines) == 1
    flattened = " ".join(MULTILINE_ADVISORY.split())
    assert flattened in advisory_lines[0]
    assert "\n" not in advisory_lines[0]


def test_job_record_roundtrips_advisories_json(tmp_path) -> None:
    from jaunt import jobs as jobs_mod

    job = jobs_mod.JobRecord.new(
        module="app.specs", spec_digest="deadbeef", base_commit="abc123", branch="main"
    )
    job = jobs_mod.mark(tmp_path, job, jobs_mod.GREEN, advisories=json.dumps(["a", "b"]))
    reloaded = jobs_mod.load_job(tmp_path, job.id)
    assert reloaded is not None
    assert json.loads(reloaded.advisories) == ["a", "b"]

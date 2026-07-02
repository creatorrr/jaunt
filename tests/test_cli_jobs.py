from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import jaunt.cli
from jaunt import jobs
from jaunt.cli import main
from test_regressions_review_fixes import _restore_modules, _write, _write_package_init


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def scaffolded_project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".jaunt/\n", encoding="utf-8")
    (root / "jaunt.toml").write_text(
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n',
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    return root


def _patch_for(repo: Path, relpath: str, content: str) -> tuple[str, str, list[str]]:
    base = _git(repo, "rev-parse", "HEAD")
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", relpath)
    patch = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--binary", base],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    _git(repo, "reset", "--hard", base)
    _git(repo, "clean", "-fd", "--", relpath)
    return patch, base, [relpath]


def _park_job(
    root: Path,
    patch: str,
    paths: list[str],
    *,
    module: str = "app",
    spec_digest: str = "d",
) -> jobs.JobRecord:
    job = jobs.JobRecord.new(
        module=module,
        spec_digest=spec_digest,
        base_commit=_git(root, "rev-parse", "HEAD"),
        branch="main",
    )
    patch_file = jobs.jobs_dir(root) / f"{job.id}.patch"
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(patch, encoding="utf-8")
    return jobs.mark(root, job, jobs.PARKED, patch_paths=json.dumps(paths))


def _make_magic_project(root: Path, *, package: str = "retry_app") -> tuple[Path, str]:
    project = root / "magic_repo"
    project.mkdir()
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.email", "t@example.com")
    _git(project, "config", "user.name", "T")
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
    _write(project / ".gitignore", ".jaunt/\n")
    _write_package_init(project, f"src/{package}")
    _write(
        project / "src" / package / "specs.py",
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
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "init")
    return project, f"{package}.specs"


def _status_digest(project: Path, module: str, capsys) -> str:
    before = {
        "retry_app": sys.modules.get("retry_app"),
        "retry_app.specs": sys.modules.get("retry_app.specs"),
    }
    orig_sys_path = list(sys.path)
    try:
        rc = main(["status", "--json", "--magic-only", "--root", str(project)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        return str(payload["digests"][module])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules(["retry_app"], before=before)


def test_parse_jobs_defaults() -> None:
    ns = jaunt.cli.parse_args(["jobs"])
    assert ns.command == "jobs"
    assert ns.jobs_command is None
    assert ns.json_output is False


def test_main_dispatches_jobs(monkeypatch) -> None:
    monkeypatch.setattr(jaunt.cli, "cmd_jobs", lambda args: 0)
    assert jaunt.cli.main(["jobs"]) == 0


def test_jobs_list_empty(capsys, monkeypatch, scaffolded_project: Path) -> None:
    monkeypatch.chdir(scaffolded_project)
    rc = main(["jobs", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "jobs"
    assert payload["ok"] is True
    assert payload["jobs"] == []
    assert payload["would_rebuild"] == {}


def test_jobs_list_and_show_print_and_emit_phase(
    capsys, monkeypatch, scaffolded_project: Path
) -> None:
    monkeypatch.chdir(scaffolded_project)
    job = jobs.JobRecord.new(module="app", spec_digest="d", base_commit="c", branch="main")
    jobs.save_job(scaffolded_project, job)
    running = jobs.mark(
        scaffolded_project,
        job,
        jobs.RUNNING,
        phase="[build] app: generating (calling codex)",
    )

    assert main(["jobs"]) == 0
    out = capsys.readouterr().out
    assert f"- {running.id} app: running — [build] app: generating (calling codex)" in out

    assert main(["jobs", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["jobs"][0]["phase"] == running.phase

    assert main(["jobs", "show", running.id]) == 0
    out = capsys.readouterr().out
    assert "state: running — [build] app: generating (calling codex)" in out

    assert main(["jobs", "show", running.id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["job"]["phase"] == running.phase


def test_jobs_show_full_reads_detail_log(capsys, monkeypatch, scaffolded_project: Path) -> None:
    monkeypatch.chdir(scaffolded_project)
    root = Path(scaffolded_project)
    job = jobs.JobRecord.new(module="app", spec_digest="d", base_commit="c", branch="main")
    detail = jobs.jobs_dir(root) / f"{job.id}.log"
    detail.parent.mkdir(parents=True, exist_ok=True)
    detail.write_text("full assertion diff here\n", encoding="utf-8")
    jobs.save_job(
        root,
        jobs.mark(root, job, jobs.FAILED, error="battery 45/47", detail_log=str(detail)),
    )

    rc = main(["jobs", "show", job.id, "--full"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "battery 45/47" in out
    assert "full assertion diff here" in out


def test_jobs_show_preserves_parent_root_from_other_cwd(
    capsys, monkeypatch, scaffolded_project: Path, tmp_path: Path
) -> None:
    root = scaffolded_project
    patch, _, paths = _patch_for(root, "src/__generated__/app.py", "y = 2\n")
    job = _park_job(root, patch, paths)
    other = tmp_path / "other"
    other.mkdir()

    monkeypatch.chdir(other)
    rc = main(["jobs", "--root", str(root), "show", job.id])
    out = capsys.readouterr().out

    assert rc == 0
    assert job.id in out


def test_jobs_retry_preserves_parent_root_from_other_cwd(
    capsys, monkeypatch, scaffolded_project: Path, tmp_path: Path
) -> None:
    root = scaffolded_project
    patch, _, paths = _patch_for(root, "src/__generated__/app.py", "y = 2\n")
    job = _park_job(root, patch, paths)
    other = tmp_path / "other"
    other.mkdir()

    monkeypatch.chdir(other)
    rc = main(["jobs", "--root", str(root), "retry", job.id, "--force"])

    assert rc == 0
    assert capsys.readouterr().out.strip()
    reloaded = jobs.load_job(root, job.id)
    assert reloaded is not None
    assert reloaded.state == jobs.LANDED


def test_jobs_retry_lands_parked_patch(capsys, monkeypatch, scaffolded_project: Path) -> None:
    root = scaffolded_project
    patch, _, paths = _patch_for(root, "src/__generated__/app.py", "y = 2\n")
    job = _park_job(root, patch, paths)

    monkeypatch.chdir(root)
    rc = main(["jobs", "retry", job.id, "--force"])

    assert rc == 0
    assert capsys.readouterr().out.strip()
    reloaded = jobs.load_job(root, job.id)
    assert reloaded is not None
    assert reloaded.state == jobs.LANDED
    assert reloaded.landed_commit
    assert (root / "src" / "__generated__" / "app.py").read_text(encoding="utf-8") == "y = 2\n"


def test_jobs_retry_keeps_parked_on_conflict(capsys, monkeypatch, scaffolded_project: Path) -> None:
    root = scaffolded_project
    patch, _, paths = _patch_for(root, "src/__generated__/app.py", "y = 6\n")
    job = _park_job(root, patch, paths)
    (root / "src" / "__generated__").mkdir(exist_ok=True)
    (root / "src" / "__generated__" / "app.py").write_text(
        "conflicting committed content\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "conflicting")

    monkeypatch.chdir(root)
    rc = main(["jobs", "retry", job.id, "--force"])

    assert rc == 4
    assert "parked" in capsys.readouterr().err.lower()
    reloaded = jobs.load_job(root, job.id)
    assert reloaded is not None
    assert reloaded.state == jobs.PARKED


def test_jobs_retry_lands_when_spec_digest_matches(capsys, monkeypatch, tmp_path: Path) -> None:
    project, module = _make_magic_project(tmp_path)
    digest = _status_digest(project, module, capsys)
    patch, _, paths = _patch_for(project, "src/__generated__/specs.py", "generated = True\n")
    job = _park_job(project, patch, paths, module=module, spec_digest=digest)

    monkeypatch.chdir(project)
    rc = main(["jobs", "retry", job.id, "--root", str(project)])

    assert rc == 0
    assert capsys.readouterr().out.strip()
    reloaded = jobs.load_job(project, job.id)
    assert reloaded is not None
    assert reloaded.state == jobs.LANDED
    assert (project / "src" / "__generated__" / "specs.py").read_text(encoding="utf-8") == (
        "generated = True\n"
    )


def test_jobs_retry_refuses_stale_spec_digest_then_force_lands(
    capsys, monkeypatch, scaffolded_project: Path
) -> None:
    root = scaffolded_project
    patch, _, paths = _patch_for(root, "src/__generated__/app.py", "y = 3\n")
    job = _park_job(root, patch, paths)

    monkeypatch.chdir(root)
    rc = main(["jobs", "retry", job.id])
    captured = capsys.readouterr()

    assert rc == 4
    assert "app" in captured.err
    assert (
        "spec changed since this job parked; the daemon will rebuild it -- use --force to land "
        "anyway"
    ) in captured.err
    assert not (root / "src" / "__generated__" / "app.py").exists()
    reloaded = jobs.load_job(root, job.id)
    assert reloaded is not None
    assert reloaded.state == jobs.PARKED

    rc = main(["jobs", "retry", job.id, "--force"])

    assert rc == 0
    assert capsys.readouterr().out.strip()
    reloaded = jobs.load_job(root, job.id)
    assert reloaded is not None
    assert reloaded.state == jobs.LANDED
    assert (root / "src" / "__generated__" / "app.py").read_text(encoding="utf-8") == "y = 3\n"

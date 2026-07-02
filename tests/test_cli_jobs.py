from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import jaunt.cli
from jaunt import jobs
from jaunt.cli import main


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


def _park_job(root: Path, patch: str, paths: list[str]) -> jobs.JobRecord:
    job = jobs.JobRecord.new(
        module="app",
        spec_digest="d",
        base_commit=_git(root, "rev-parse", "HEAD"),
        branch="main",
    )
    patch_file = jobs.jobs_dir(root) / f"{job.id}.patch"
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(patch, encoding="utf-8")
    return jobs.mark(root, job, jobs.PARKED, patch_paths=json.dumps(paths))


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


def test_jobs_retry_lands_parked_patch(capsys, monkeypatch, scaffolded_project: Path) -> None:
    root = scaffolded_project
    patch, _, paths = _patch_for(root, "src/__generated__/app.py", "y = 2\n")
    job = _park_job(root, patch, paths)

    monkeypatch.chdir(root)
    rc = main(["jobs", "retry", job.id])

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
    rc = main(["jobs", "retry", job.id])

    assert rc == 4
    assert "parked" in capsys.readouterr().err.lower()
    reloaded = jobs.load_job(root, job.id)
    assert reloaded is not None
    assert reloaded.state == jobs.PARKED

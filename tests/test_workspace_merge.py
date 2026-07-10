from __future__ import annotations

import subprocess
from pathlib import Path

from jaunt import workspace_merge


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(root: Path, name: str) -> None:
    _write(root / "pyproject.toml", f"[project]\nname={name!r}\nversion='1'\n")
    _write(root / "src" / name / "__init__.py")
    _write(
        root / "src" / name / "spec.py",
        "import jaunt\n@jaunt.magic()\ndef value() -> int:\n    ...\n",
    )
    _write(root / "tests" / "test_spec.py")
    _write(
        root / "jaunt.toml",
        'version = 1\n[paths]\nsource_roots=["src"]\ntest_roots=["tests"]\n',
    )


def _workspace(tmp_path: Path) -> Path:
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n# keep this comment\n[paths]\nsource_roots=["root_src"]\n'
        "test_roots=[]\n\n[build]\njobs=8\n",
    )
    (tmp_path / "root_src").mkdir()
    _project(tmp_path / "packages/a", "pkg_a")
    _project(tmp_path / "packages/b", "pkg_b")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "jaunt.toml", "packages/*/jaunt.toml"], cwd=tmp_path, check=True)
    return tmp_path


def test_merge_plan_preserves_routes_and_orders_root_entries_first(
    tmp_path: Path, monkeypatch
) -> None:
    root = _workspace(tmp_path)
    monkeypatch.setattr(workspace_merge, "_freshness_conflicts", lambda *_args: [])

    plan = workspace_merge.plan_merge(root)

    assert plan.neutral
    assert plan.source_roots == (
        "root_src",
        "packages/a/src",
        "packages/b/src",
    )
    assert plan.test_roots == ("packages/a/tests", "packages/b/tests")
    assert {route["module"] for route in plan.module_routes} == {
        "pkg_a.spec",
        "pkg_b.spec",
    }
    assert all(route["neutral"] == "true" for route in plan.module_routes)


def test_apply_merge_preserves_root_text_and_deletes_only_child_configs(
    tmp_path: Path, monkeypatch
) -> None:
    root = _workspace(tmp_path)
    monkeypatch.setattr(workspace_merge, "_freshness_conflicts", lambda *_args: [])
    plan = workspace_merge.plan_merge(root)
    real_run = workspace_merge.subprocess.run

    def fake_run(cmd, **kwargs):
        if len(cmd) >= 3 and cmd[1:3] == ["-m", "jaunt"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(workspace_merge.subprocess, "run", fake_run)
    ok, error = workspace_merge.apply_merge(root, plan)

    assert ok, error
    merged = (root / "jaunt.toml").read_text(encoding="utf-8")
    assert "# keep this comment" in merged
    assert '"packages/a/src"' in merged
    assert "[build]\njobs=8" in merged
    assert not (root / "packages/a/jaunt.toml").exists()
    assert not (root / "packages/b/jaunt.toml").exists()


def test_apply_merge_rolls_back_when_post_merge_check_fails(tmp_path: Path, monkeypatch) -> None:
    root = _workspace(tmp_path)
    monkeypatch.setattr(workspace_merge, "_freshness_conflicts", lambda *_args: [])
    plan = workspace_merge.plan_merge(root)
    before = (root / "jaunt.toml").read_bytes()

    monkeypatch.setattr(
        workspace_merge.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 4, "blocked", ""),
    )
    ok, error = workspace_merge.apply_merge(root, plan)

    assert not ok
    assert "blocked" in error
    assert (root / "jaunt.toml").read_bytes() == before
    assert (root / "packages/a/jaunt.toml").exists()
    assert (root / "packages/b/jaunt.toml").exists()

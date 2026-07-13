from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import pytest

import jaunt.cli
from jaunt.cli import cmd_tree
from jaunt.repo_context import tree as tree_mod
from jaunt.repo_context.describe import ast_describe, describe_dir
from jaunt.repo_context.digests import TreeCache


def _typescript_project(root: Path, *, generated_dir: str = "__generated__") -> Path:
    (root / "src" / "tokens").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "jaunt.toml").write_text(
        f'''\
version = 2

[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
generated_dir = "{generated_dir}"
''',
        encoding="utf-8",
    )
    (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    return root


def test_phase8_daemon_status_accepts_configured_typescript_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _typescript_project(tmp_path)
    monkeypatch.chdir(root)

    assert jaunt.cli.main(["daemon", "status", "--json", "--root", str(root)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "daemon-status"
    assert payload["ok"] is True


def test_phase8_jobs_reports_qualified_typescript_staleness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _typescript_project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        jaunt.cli,
        "_jobs_would_rebuild",
        lambda _root, _args: {"ts:src/tokens/index": "unbuilt"},
    )

    assert jaunt.cli.main(["jobs", "--json", "--root", str(root)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["would_rebuild"] == {"ts:src/tokens/index": "unbuilt"}


@pytest.mark.parametrize("artifact", ["index.ts", "index.api.ts", "index.jaunt.json"])
def test_guard_maps_typescript_artifacts_to_private_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    artifact: str,
) -> None:
    root = _typescript_project(tmp_path, generated_dir="machine")
    spec = root / "src" / "tokens" / "index.jaunt.ts"
    spec.write_text("export {};\n", encoding="utf-8")
    payload = {
        "cwd": str(root),
        "tool_name": "Edit",
        "tool_input": {"file_path": f"src/tokens/machine/{artifact}"},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert jaunt.cli.main(["guard"]) == 0

    output = json.loads(capsys.readouterr().out)
    decision = output["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert "src/tokens/index.jaunt.ts" in decision["permissionDecisionReason"]
    assert "generated TypeScript" in decision["permissionDecisionReason"]


def test_guard_allows_authored_typescript_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _typescript_project(tmp_path, generated_dir="machine")
    payload = {
        "cwd": str(root),
        "tool_name": "Edit",
        "tool_input": {"file_path": "src/tokens/index.jaunt.ts"},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert jaunt.cli.main(["guard"]) == 0
    assert capsys.readouterr().out == ""


def test_repo_tree_includes_typescript_and_excludes_machine_outputs(tmp_path: Path) -> None:
    root = tmp_path
    source = root / "src"
    (source / "ui").mkdir(parents=True)
    (source / "__generated__").mkdir()
    (source / "dist").mkdir()
    (source / "node_modules" / "dep").mkdir(parents=True)
    (source / "coverage").mkdir()
    (source / "api.py").write_text('"""Python API."""\n', encoding="utf-8")
    (source / "service.ts").write_text("export function serve(): void {}\n", encoding="utf-8")
    (source / "ui" / "view.tsx").write_text(
        "/** Renders the main view. */\nexport function View() { return null; }\n",
        encoding="utf-8",
    )
    (source / "types.d.ts").write_text("export interface Hidden {}\n", encoding="utf-8")
    (source / "__generated__" / "machine.ts").write_text("export {};\n", encoding="utf-8")
    (source / "dist" / "bundle.ts").write_text("export {};\n", encoding="utf-8")
    (source / "node_modules" / "dep" / "index.ts").write_text("export {};\n", encoding="utf-8")
    (source / "coverage" / "report.ts").write_text("export {};\n", encoding="utf-8")

    doc, result = tree_mod.sync(
        repo_root=root,
        source_roots=[source],
        generated_dir=("__generated__", "machine"),
        cache=TreeCache(root / ".jaunt" / "tree-cache.json"),
        project_name="mixed",
        project_version="2",
        today="2026-07-13",
    )

    assert set(result.added) == {"src/api.py", "src/service.ts", "src/ui/view.tsx"}
    assert {"src/api.py", "src/service.ts", "src/ui/view.tsx"} <= doc.paths()
    assert all("node_modules" not in path for path in doc.paths())
    assert ast_describe(source / "service.ts") == "defines serve"
    assert ast_describe(source / "ui" / "view.tsx") == "Renders the main view."
    declaration_only = source / "declaration-only"
    declaration_only.mkdir()
    (declaration_only / "index.d.ts").write_text("export interface Hidden {}\n", encoding="utf-8")
    assert describe_dir(declaration_only) == "declaration-only package"


def test_tree_command_uses_typescript_source_roots(tmp_path: Path) -> None:
    root = _typescript_project(tmp_path)
    (root / "src" / "tokens" / "index.jaunt.ts").write_text(
        'import * as jaunt from "@usejaunt/ts/spec";\n'
        "export function token(): string { return jaunt.magic(); }\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        root=str(root),
        config=None,
        json_output=False,
        force=False,
        enrich=False,
        no_enrich=False,
        check=False,
    )

    assert cmd_tree(args) == 0
    tree_text = (root / "treedocs.yaml").read_text(encoding="utf-8")
    assert "index.jaunt.ts" in tree_text
    assert "specs: token" in tree_text


def test_release_stages_npm_and_lifecycle_smokes_both_python_distributions() -> None:
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert 'if [[ "$candidate_tag" == "latest" ]]' in workflow
    assert 'npm publish "$tarball" --access public --tag "$candidate_tag"' in workflow
    assert "published_integrity=" in workflow
    assert "candidate_integrity=" in workflow
    assert "sdist=\"$(find release/python -maxdepth 1 -name '*.tar.gz'" in workflow
    assert 'prepare_venv "$sdist_venv" "$sdist" false' in workflow
    assert (
        'smoke_lifecycle "$sdist_venv/bin/jaunt" "$sdist_project" "$sdist_init_project"' in workflow
    )

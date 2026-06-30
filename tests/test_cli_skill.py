from __future__ import annotations

import json
from pathlib import Path

import jaunt.builder
import jaunt.tester
from jaunt.cli import main, parse_args
from jaunt.skills_builtin import DEFAULT_BUILTIN_SKILLS


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_tiny_project(root: Path) -> None:
    _write(
        root / "jaunt.toml",
        """
version = 1

[paths]
source_roots = ["src"]
test_roots = ["tests"]
generated_dir = "__generated__"

[skills]
auto = false
""".lstrip(),
    )
    _write(
        root / "src/app.py",
        '''
import jaunt


@jaunt.magic()
def greet(name: str) -> str:
    """Return a friendly greeting for name."""
    raise RuntimeError("spec stub")
'''.lstrip(),
    )


def _write_tiny_test_project(root: Path) -> None:
    _write_tiny_project(root)
    _write(
        root / "tests/test_app.py",
        '''
import jaunt


@jaunt.test()
def test_greet() -> None:
    """Assert greet("Ada") returns a friendly greeting containing Ada."""
    raise AssertionError("test stub")
'''.lstrip(),
    )


# --- Parse tests ---


def test_parse_skill_list() -> None:
    args = parse_args(["skill", "list"])
    assert args.command == "skill"
    assert args.skill_command == "list"


def test_parse_skill_via_skills_alias() -> None:
    args = parse_args(["skills", "list"])
    assert args.command == "skills"
    assert args.skill_command == "list"


def test_parse_skill_add() -> None:
    args = parse_args(["skill", "add", "my-tool"])
    assert args.command == "skill"
    assert args.skill_command == "add"
    assert args.name == "my-tool"


def test_parse_skill_add_with_description() -> None:
    args = parse_args(["skill", "add", "my-tool", "--description", "HTTP client"])
    assert args.description == "HTTP client"


def test_parse_skill_add_with_lib() -> None:
    args = parse_args(["skill", "add", "my-tool", "-l", "requests", "-l", "httpx"])
    assert args.libs == ["requests", "httpx"]


def test_parse_skill_remove() -> None:
    args = parse_args(["skill", "remove", "my-tool"])
    assert args.skill_command == "remove"
    assert args.name == "my-tool"


def test_parse_skill_rm() -> None:
    args = parse_args(["skill", "rm", "my-tool"])
    assert args.skill_command == "rm"
    assert args.name == "my-tool"


def test_parse_skill_rm_force() -> None:
    args = parse_args(["skill", "rm", "my-tool", "-f"])
    assert args.force is True


def test_parse_skill_show() -> None:
    args = parse_args(["skill", "show", "my-tool"])
    assert args.skill_command == "show"
    assert args.name == "my-tool"


def test_parse_skill_refresh() -> None:
    args = parse_args(["skill", "refresh", "--force"])
    assert args.skill_command == "refresh"
    assert args.force is True


def test_parse_skill_import() -> None:
    args = parse_args(["skill", "import", "rich", "--from", "/some/dir", "--dry-run"])
    assert args.skill_command == "import"
    assert args.names == ["rich"]
    assert args.from_dir == "/some/dir"
    assert args.dry_run is True


def test_parse_skill_import_all() -> None:
    args = parse_args(["skill", "import", "--all"])
    assert args.skill_command == "import"
    assert args.import_all is True


def test_parse_skill_build() -> None:
    args = parse_args(["skill", "build", "my-tool"])
    assert args.skill_command == "build"
    assert args.name == "my-tool"


def test_cmd_build_threads_default_builtin_skills(tmp_path: Path, capsys, monkeypatch) -> None:
    project = tmp_path / "project"
    _write_tiny_project(project)
    captured: dict[str, object] = {}

    async def fake_run_build(**kwargs):
        captured.update(kwargs)
        return jaunt.builder.BuildReport(generated={"app"}, skipped=set(), failed={})

    monkeypatch.setattr(jaunt.builder, "run_build", fake_run_build)

    rc = main(["build", "--root", str(project), "--json"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert captured["project_root"] == project
    assert captured["builtin_skill_names"] == DEFAULT_BUILTIN_SKILLS
    assert isinstance(captured["skills_digest"], str)
    assert captured["skills_digest"]


def test_cmd_build_threads_empty_builtin_skills_with_flag(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = tmp_path / "project"
    _write_tiny_project(project)
    captured: dict[str, object] = {}

    async def fake_run_build(**kwargs):
        captured.update(kwargs)
        return jaunt.builder.BuildReport(generated={"app"}, skipped=set(), failed={})

    monkeypatch.setattr(jaunt.builder, "run_build", fake_run_build)

    rc = main(["build", "--root", str(project), "--json", "--no-builtin-skills"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert captured["project_root"] == project
    assert captured["builtin_skill_names"] == ()
    assert isinstance(captured["skills_digest"], str)
    assert captured["skills_digest"]


def test_cmd_test_threads_empty_builtin_skills_with_flag(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = tmp_path / "project"
    _write_tiny_test_project(project)
    captured: dict[str, object] = {}

    def fake_run_tests(**kwargs):
        captured.update(kwargs)
        return jaunt.tester.PytestResult(
            exit_code=0,
            passed=True,
            failed=False,
            failures=[],
            generated={"tests.test_app"},
            skipped=set(),
        )

    monkeypatch.setattr(jaunt.tester, "run_tests", fake_run_tests)

    rc = main(
        [
            "test",
            "--root",
            str(project),
            "--no-build",
            "--no-run",
            "--json",
            "--no-builtin-skills",
        ]
    )

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert captured["project_root"] == project
    assert captured["builtin_skill_names"] == ()
    assert isinstance(captured["skills_digest"], str)
    assert captured["skills_digest"]
    repair_context = captured["repair_build_context"]
    assert isinstance(repair_context, jaunt.tester.RepairBuildContext)
    assert repair_context.project_root == project
    assert repair_context.builtin_skill_names == ()
    assert repair_context.skills_digest == captured["skills_digest"]


# --- cmd_skill dispatch tests ---


def test_cmd_skill_list_json(tmp_path: Path, capsys) -> None:
    _write(
        tmp_path / ".agents/skills/my-tool/SKILL.md",
        "# my-tool\nContent\n",
    )
    rc = main(["skill", "list", "--root", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert len(out["skills"]) == 1
    assert out["skills"][0]["name"] == "my-tool"


def test_cmd_skill_list_via_skills_alias(tmp_path: Path, capsys) -> None:
    _write(tmp_path / ".agents/skills/x/SKILL.md", "# x\n")
    rc = main(["skills", "list", "--root", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True


def test_cmd_skill_add_json(tmp_path: Path, capsys) -> None:
    rc = main(["skill", "add", "new-skill", "--root", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert (tmp_path / ".agents/skills/new-skill/SKILL.md").exists()


def test_cmd_skill_add_duplicate(tmp_path: Path, capsys) -> None:
    main(["skill", "add", "dup", "--root", str(tmp_path)])
    capsys.readouterr()  # clear first call's output
    rc = main(["skill", "add", "dup", "--root", str(tmp_path), "--json"])
    assert rc != 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False


def test_cmd_skill_add_lib_relative_to_root(tmp_path: Path, capsys) -> None:
    """--lib paths should resolve relative to --root, not CWD."""
    project = tmp_path / "myproject"
    project.mkdir()
    lib_dir = project / "src" / "pkg"
    lib_dir.mkdir(parents=True)
    (lib_dir / "__init__.py").write_text("")
    rc = main(
        [
            "skill",
            "add",
            "my-tool",
            "--root",
            str(project),
            "--lib",
            "src/pkg",
            "--json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert (project / ".agents/skills/my-tool/SKILL.md").exists()


def test_cmd_skill_remove_without_force(tmp_path: Path, capsys) -> None:
    """Without -f, shows info but does NOT delete."""
    _write(tmp_path / ".agents/skills/to-rm/SKILL.md", "x\n")
    rc = main(["skill", "remove", "to-rm", "--root", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / ".agents/skills/to-rm/SKILL.md").exists()  # NOT deleted
    out = capsys.readouterr().out
    assert "Rerun with -f" in out


def test_cmd_skill_remove_with_force(tmp_path: Path, capsys) -> None:
    _write(tmp_path / ".agents/skills/to-rm/SKILL.md", "x\n")
    rc = main(["skill", "remove", "to-rm", "-f", "--root", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert not (tmp_path / ".agents/skills/to-rm").exists()


def test_cmd_skill_rm_alias(tmp_path: Path, capsys) -> None:
    """rm behaves exactly like remove."""
    _write(tmp_path / ".agents/skills/to-rm/SKILL.md", "x\n")
    rc = main(["skill", "rm", "to-rm", "-f", "--root", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert not (tmp_path / ".agents/skills/to-rm").exists()


def test_cmd_skill_show(tmp_path: Path, capsys) -> None:
    _write(tmp_path / ".agents/skills/s/SKILL.md", "Hello\n")
    rc = main(["skill", "show", "s", "--root", str(tmp_path)])
    assert rc == 0
    assert "Hello" in capsys.readouterr().out


def test_cmd_skill_import_json(tmp_path: Path, capsys) -> None:
    source = tmp_path / "ext"
    _write(source / "ext-tool/SKILL.md", "ext content\n")
    project = tmp_path / "proj"
    project.mkdir()
    rc = main(
        [
            "skill",
            "import",
            "ext-tool",
            "--root",
            str(project),
            "--from",
            str(source),
            "--json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert len(out["results"]) == 1
    assert out["results"][0]["status"] == "imported"


def test_cmd_skill_import_all_json(tmp_path: Path, capsys) -> None:
    source = tmp_path / "ext"
    _write(source / "ext-tool/SKILL.md", "ext content\n")
    project = tmp_path / "proj"
    project.mkdir()
    rc = main(
        [
            "skill",
            "import",
            "--all",
            "--root",
            str(project),
            "--from",
            str(source),
            "--json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["selected"] == ["ext-tool"]


def test_cmd_skill_import_requires_selection(tmp_path: Path, capsys) -> None:
    source = tmp_path / "ext"
    _write(source / "ext-tool/SKILL.md", "ext content\n")
    project = tmp_path / "proj"
    project.mkdir()
    rc = main(
        [
            "skill",
            "import",
            "--root",
            str(project),
            "--from",
            str(source),
            "--json",
        ]
    )
    assert rc != 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["available"] == ["ext-tool"]


def test_cmd_skill_build_no_meta(tmp_path: Path, capsys) -> None:
    """Fails cleanly when META.json missing."""
    _write(tmp_path / ".agents/skills/s/SKILL.md", "# s\n")
    rc = main(["skill", "build", "s", "--root", str(tmp_path), "--json"])
    assert rc != 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "No library references" in out["error"]


def test_cmd_skill_build_empty_libs(tmp_path: Path, capsys) -> None:
    """Fails cleanly when metadata has no libs."""
    _write(tmp_path / ".agents/skills/s/SKILL.md", "# s\n")
    _write(
        tmp_path / ".agents/skills/s/META.json",
        json.dumps({"libs": [], "description": None}),
    )
    rc = main(["skill", "build", "s", "--root", str(tmp_path), "--json"])
    assert rc != 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "No library references" in out["error"]

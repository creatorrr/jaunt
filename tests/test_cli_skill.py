from __future__ import annotations

import json
from pathlib import Path

from jaunt.cli import main, parse_args


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- Parse tests ---


def test_parse_skill_list() -> None:
    args = parse_args(["skill", "list"])
    assert args.command == "skill"
    assert args.skill_command == "list"


def test_parse_skill_add() -> None:
    args = parse_args(["skill", "add", "my-tool"])
    assert args.command == "skill"
    assert args.skill_command == "add"
    assert args.name == "my-tool"


def test_parse_skill_remove() -> None:
    args = parse_args(["skill", "remove", "my-tool"])
    assert args.skill_command == "remove"
    assert args.name == "my-tool"


def test_parse_skill_show() -> None:
    args = parse_args(["skill", "show", "my-tool"])
    assert args.skill_command == "show"
    assert args.name == "my-tool"


def test_parse_skill_refresh() -> None:
    args = parse_args(["skill", "refresh", "--force"])
    assert args.skill_command == "refresh"
    assert args.force is True


def test_parse_skill_import() -> None:
    args = parse_args(["skill", "import", "--from", "/some/dir", "--dry-run"])
    assert args.skill_command == "import"
    assert args.from_dir == "/some/dir"
    assert args.dry_run is True


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


def test_cmd_skill_remove_json(tmp_path: Path, capsys) -> None:
    _write(tmp_path / ".agents/skills/to-rm/SKILL.md", "x\n")
    rc = main(["skill", "remove", "to-rm", "--root", str(tmp_path), "--json"])
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

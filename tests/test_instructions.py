"""Tests for `jaunt instructions` and the `jaunt.instructions` primer module."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import jaunt.cli
import jaunt.status_core as status_core
from jaunt import instructions
from jaunt.config import load_config


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_project(
    tmp_path: Path,
    *,
    generated_dir: str = "__generated__",
    model: str = "gpt-5.5",
    pkg: str = "ipkg",
) -> None:
    _write(
        tmp_path / "jaunt.toml",
        (
            "version = 1\n\n"
            '[paths]\nsource_roots = ["src"]\n'
            f'generated_dir = "{generated_dir}"\n\n'
            f'[codex]\nmodel = "{model}"\n'
        ),
    )
    _write(tmp_path / "src" / pkg / "__init__.py", "")
    _write(
        tmp_path / "src" / pkg / "specs.py",
        (
            "import jaunt\n\n"
            "@jaunt.magic()\n"
            "def greet(name: str) -> str:\n"
            '    """Say hello."""\n'
            '    raise RuntimeError("stub")\n'
        ),
    )


# --------------------------------------------------------------------------- #
# Primer rendering
# --------------------------------------------------------------------------- #


def test_primer_contains_hard_rules_and_both_modes() -> None:
    text = instructions.render(project=None)
    assert "__generated__" in text
    assert "@jaunt.magic" in text
    assert "@jaunt.contract" in text  # both authoring modes documented
    for cmd in ("jaunt build", "jaunt test", "jaunt status"):
        assert cmd in text
    # placeholders must be substituted
    assert "{{" not in text and "}}" not in text


def test_command_and_exit_tables_render() -> None:
    text = instructions.render(project=None)
    assert "| Command | What it does |" in text
    assert "`jaunt instructions`" in text
    assert "`jaunt jobs wait`" in text
    assert "| Code | Meaning |" in text
    assert (
        "| 4 | Pytest failure, contract `check`/`reconcile` block, or daemon job failed/parked. |"
    ) in text
    assert "| 5 | Timeout while waiting for daemon jobs. |" in text
    assert "`git commit … && jaunt jobs wait --timeout 1800`" in text
    assert "`--progress {auto,rich,plain,none}`" in text


def test_exit_code_docs_include_jobs_wait_failed_parked() -> None:
    root = Path(__file__).resolve().parents[1]
    docs = (root / "DOCS.md").read_text(encoding="utf-8")
    claude = (root / "CLAUDE.md").read_text(encoding="utf-8")

    assert (
        "`4`: pytest failure, contract `check`/`reconcile` block, "
        "or daemon job failed/parked while waiting"
    ) in docs
    assert (
        "| 4    | Pytest failure, contract `check`/`reconcile` block, "
        "or daemon job failed/parked while waiting |"
    ) in claude


def test_render_no_project_includes_init_note() -> None:
    note = instructions.no_project_note("Missing jaunt.toml at: /x/jaunt.toml")
    text = instructions.render(project=None, note=note)
    assert "jaunt init" in text


def test_no_project_note_distinguishes_missing_from_malformed() -> None:
    assert "jaunt init" in instructions.no_project_note("Could not find jaunt.toml ...")
    assert "jaunt init" in instructions.no_project_note("Missing jaunt.toml at: /x")
    malformed = instructions.no_project_note("Invalid value for paths.source_roots")
    assert "could not be loaded" in malformed
    assert "jaunt init" not in malformed


# --------------------------------------------------------------------------- #
# Live project section
# --------------------------------------------------------------------------- #


def test_project_section_reflects_config(tmp_path: Path, monkeypatch) -> None:
    _make_project(tmp_path, generated_dir="__gen__", model="gpt-5.5-mini")
    monkeypatch.chdir(tmp_path)
    cfg = load_config(root=tmp_path)
    section = instructions.project_section(tmp_path, cfg)
    assert section["paths"]["generated_dir"] == "__gen__"
    assert section["model"] == "gpt-5.5-mini"
    assert section["engine"] == "codex"
    # never built -> exactly one stale module
    assert section["freshness"]["total"] == 1
    assert section["freshness"]["stale"] == 1
    assert section["freshness"]["fresh"] == 0
    # and it renders into the markdown
    text = instructions.render(project=section)
    assert "Your project right now" in text
    assert "`__gen__`" in text
    assert "1 stale" in text


def test_freshness_degrades_to_none_on_probe_failure(tmp_path: Path, monkeypatch) -> None:
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = load_config(root=tmp_path)

    def boom(**_kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(status_core, "compute_magic_status", boom)
    section = instructions.project_section(tmp_path, cfg)
    assert section["freshness"] is None
    text = instructions.render(project=section)
    assert "run `jaunt status`" in text


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_parse_instructions_flags() -> None:
    ns = jaunt.cli.parse_args(["instructions", "--json", "--root", "/tmp"])
    assert ns.command == "instructions"
    assert ns.json_output is True
    assert ns.root == "/tmp"


def test_main_dispatches_instructions(monkeypatch) -> None:
    monkeypatch.setattr(jaunt.cli, "cmd_instructions", lambda args: 0)
    assert jaunt.cli.main(["instructions"]) == 0


def test_cmd_instructions_in_project(tmp_path: Path, monkeypatch, capsys) -> None:
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    ns = jaunt.cli.parse_args(["instructions"])
    rc = jaunt.cli.cmd_instructions(ns)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Jaunt — agent primer" in out
    assert "Your project right now" in out


def test_cmd_instructions_json_in_project(tmp_path: Path, monkeypatch, capsys) -> None:
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    ns = jaunt.cli.parse_args(["instructions", "--json"])
    rc = jaunt.cli.cmd_instructions(ns)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "instructions"
    assert payload["ok"] is True
    assert isinstance(payload["text"], str) and payload["text"]
    assert payload["project"]["engine"] == "codex"


def test_cmd_instructions_no_project_succeeds(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)  # no jaunt.toml here
    ns = jaunt.cli.parse_args(["instructions", "--json", "--root", str(tmp_path)])
    rc = jaunt.cli.cmd_instructions(ns)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["project"] is None
    assert "jaunt init" in payload["text"]


# --------------------------------------------------------------------------- #
# Drift guard: the curated table must stay in sync with the real CLI
# --------------------------------------------------------------------------- #


def test_command_table_matches_real_subcommands() -> None:
    parser = jaunt.cli._build_parser()
    sub_actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert sub_actions, "expected a subparsers action"
    real = set(sub_actions[0].choices.keys())  # includes aliases

    listed = {name.split()[0] for name, _ in instructions.COMMANDS}
    # No typos / removed commands in the curated table.
    assert listed <= real, f"primer lists unknown commands: {listed - real}"
    # No silent gaps: every real subcommand is listed or explicitly omitted.
    assert real == listed | set(instructions.OMITTED_COMMANDS), {
        "missing_from_primer": real - listed - set(instructions.OMITTED_COMMANDS),
        "stale_omitted": set(instructions.OMITTED_COMMANDS) - real,
    }
    ns = jaunt.cli.parse_args(["jobs", "wait"])
    assert ns.command == "jobs"
    assert ns.jobs_command == "wait"


def test_instructions_no_project_prints_schema() -> None:
    text = instructions.render(project=None)
    assert "## jaunt.toml schema" in text
    assert "version = 1" in text
    assert "[paths]" in text

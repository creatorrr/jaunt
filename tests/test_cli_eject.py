from __future__ import annotations

from pathlib import Path

from jaunt import cli
from jaunt.contract.battery import de_jaunt_battery, parse_battery
from jaunt.contract.strength import parse_strength


def test_parse_strength() -> None:
    assert parse_strength("7/8") == (7, 8)
    assert parse_strength("0/0") == (0, 0)


def test_de_jaunt_removes_header_keeps_tests() -> None:
    src = (
        "# This file is maintained by jaunt (contract mode). Review like any test.\n"
        "# jaunt:contract=1\n# jaunt:derived-from=demo:shout\n"
        "# jaunt:prose-digest=sha256:aa\n# jaunt:signature=sha256:bb\n"
        "# jaunt:body-digest=sha256:cc\n# jaunt:strength=2/2\n# jaunt:tool_version=0.4.4\n"
        "import pytest\nfrom demo import shout\n\n"
        "# >>> jaunt:derived examples\n"
        "def test_examples():\n    assert shout('a') == 'A'\n"
        "# <<< jaunt:derived examples\n"
    )
    out = de_jaunt_battery(src, provenance="was demo:shout")
    assert "jaunt:contract" not in out
    assert ">>> jaunt:derived" not in out
    assert "def test_examples" in out
    assert "from demo import shout" in out
    assert out.lstrip().startswith("#")  # provenance comment
    assert parse_battery(out).header is None  # no longer a jaunt battery


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(
        "import jaunt\n\n\n"
        "@jaunt.contract\n"
        "def shout(text: str) -> str:\n"
        '    """Uppercase. Examples:\n    - "hi" -> "HI"\n    """\n'
        "    return text.upper()\n",
        encoding="utf-8",
    )
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
        encoding="utf-8",
    )
    return tmp_path


def test_eject_removes_marker_and_dejaunts_battery(tmp_path: Path) -> None:
    root = _project(tmp_path)
    assert cli.cmd_reconcile(cli.parse_args(["reconcile", "--root", str(root)])) == cli.EXIT_OK
    assert (
        cli.cmd_eject(cli.parse_args(["eject", "demo:shout", "--root", str(root)])) == cli.EXIT_OK
    )

    src = (root / "src" / "demo.py").read_text(encoding="utf-8")
    assert "@jaunt.contract" not in src
    battery = (root / "tests" / "contract" / "demo" / "test_shout.py").read_text(encoding="utf-8")
    assert "jaunt:contract" not in battery
    assert "def test_examples" in battery  # tests survive as plain pytest

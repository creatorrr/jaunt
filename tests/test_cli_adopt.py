from __future__ import annotations

from pathlib import Path

from jaunt import cli
from jaunt.contract.edits import add_contract_marker

PLAIN = '''\
def shout(text: str) -> str:
    """
    Uppercase a non-empty string.

    Examples:
    - "hi" -> "HI"
    """
    return text.upper()
'''

PLAIN_BAD = PLAIN.replace("return text.upper()", "return text")


def test_add_contract_marker_inserts_decorator_and_import() -> None:
    out = add_contract_marker(PLAIN, "shout")
    assert "import jaunt" in out
    assert "@jaunt.contract" in out
    # Idempotent.
    assert add_contract_marker(out, "shout").count("@jaunt.contract") == 1


def _project(tmp_path: Path, src: str) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(src, encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
        encoding="utf-8",
    )
    return tmp_path


def test_adopt_adds_marker_and_writes_battery(tmp_path: Path) -> None:
    root = _project(tmp_path, PLAIN)
    args = cli.parse_args(["adopt", "demo:shout", "--root", str(root)])
    assert cli.cmd_adopt(args) == cli.EXIT_OK
    src = (root / "src" / "demo.py").read_text(encoding="utf-8")
    assert "@jaunt.contract" in src
    assert (root / "tests" / "contract" / "demo" / "test_shout.py").is_file()


def test_adopt_surfaces_body_contract_disagreement(tmp_path: Path) -> None:
    root = _project(tmp_path, PLAIN_BAD)
    args = cli.parse_args(["adopt", "demo:shout", "--root", str(root)])
    assert cli.cmd_adopt(args) == cli.EXIT_PYTEST_FAILURE
    # Battery is not written when the body disagrees with its own docstring.
    assert not (root / "tests" / "contract" / "demo" / "test_shout.py").exists()

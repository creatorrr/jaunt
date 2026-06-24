from __future__ import annotations

from pathlib import Path

from jaunt import cli
from jaunt.contract.battery import parse_battery

GOOD = '''
import jaunt


@jaunt.contract
def shout(text: str) -> str:
    """
    Uppercase a non-empty string.

    Examples:
    - "hi" -> "HI"

    Raises:
    - "" raises ValueError
    """
    if not text:
        raise ValueError("empty")
    return text.upper()
'''

BAD = GOOD.replace("return text.upper()", "return text")  # violates the contract


def _project(tmp_path: Path, src: str) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(src, encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
        encoding="utf-8",
    )
    return tmp_path


def test_reconcile_writes_battery_when_body_passes(tmp_path: Path) -> None:
    root = _project(tmp_path, GOOD)
    args = cli.parse_args(["reconcile", "--root", str(root)])
    assert cli.cmd_reconcile(args) == cli.EXIT_OK
    battery = root / "tests" / "contract" / "demo" / "test_shout.py"
    assert battery.is_file()
    parsed = parse_battery(battery.read_text(encoding="utf-8"))
    assert parsed.header is not None
    assert parsed.header["derived-from"] == "demo:shout"
    assert "test_examples" in parsed.regions["examples"]
    # A subsequent check passes.
    assert cli.cmd_check(cli.parse_args(["check", "--root", str(root)])) == cli.EXIT_OK


def test_reconcile_fails_and_does_not_write_when_body_violates_contract(tmp_path: Path) -> None:
    root = _project(tmp_path, BAD)
    args = cli.parse_args(["reconcile", "--root", str(root)])
    assert cli.cmd_reconcile(args) == cli.EXIT_PYTEST_FAILURE
    battery = root / "tests" / "contract" / "demo" / "test_shout.py"
    assert not battery.exists()

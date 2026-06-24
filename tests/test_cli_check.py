from __future__ import annotations

from pathlib import Path

from jaunt import cli
from jaunt.contract.battery import render_battery
from jaunt.digest import contract_digests

SRC = '''
import jaunt


@jaunt.contract
def shout(text: str) -> str:
    """
    Uppercase a string.

    Examples:
    - "hi" -> "HI"

    Raises:
    - "" raises ValueError
    """
    if not text:
        raise ValueError("empty")
    return text.upper()
'''


def _project(tmp_path: Path, *, prose_digest_override: str | None = None) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(SRC, encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
        encoding="utf-8",
    )
    digs = contract_digests(str(tmp_path / "src" / "demo.py"), "shout")
    battery_dir = tmp_path / "tests" / "contract" / "demo"
    battery_dir.mkdir(parents=True)
    region_examples = (
        '@pytest.mark.parametrize("arg,want", [("hi", "HI")])\n'
        "def test_examples(arg, want):  # derived from: Examples\n"
        "    assert shout(arg) == want"
    )
    region_errors = (
        '@pytest.mark.parametrize("arg", [""])\n'
        "def test_raises_valueerror(arg):  # derived from: Raises\n"
        "    with pytest.raises(ValueError):\n"
        "        shout(arg)"
    )
    from jaunt.contract.battery import DerivedRegion

    text = render_battery(
        import_module="demo",
        func_name="shout",
        regions=[
            DerivedRegion("examples", region_examples),
            DerivedRegion("errors", region_errors),
        ],
        header_fields={
            "derived_from": "demo:shout",
            "prose_digest": prose_digest_override or digs.prose,
            "signature": digs.signature,
            "body_digest": digs.body,
            "strength": "3/3",
            "tool_version": "0.4.4",
        },
    )
    (battery_dir / "test_shout.py").write_text(text, encoding="utf-8")
    return tmp_path


def test_check_passes_when_in_sync(tmp_path: Path, capsys, monkeypatch) -> None:
    root = _project(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = cli.parse_args(["check", "--root", str(root)])
    assert cli.cmd_check(args) == cli.EXIT_OK


def test_check_blocks_on_stale_prose(tmp_path: Path) -> None:
    root = _project(tmp_path, prose_digest_override="sha256:deadbeef")
    args = cli.parse_args(["check", "--root", str(root)])
    assert cli.cmd_check(args) == cli.EXIT_PYTEST_FAILURE


def test_check_blocks_when_unbuilt(tmp_path: Path) -> None:
    root = _project(tmp_path)
    # Remove the battery -> unbuilt.
    (root / "tests" / "contract" / "demo" / "test_shout.py").unlink()
    args = cli.parse_args(["check", "--root", str(root)])
    assert cli.cmd_check(args) == cli.EXIT_PYTEST_FAILURE

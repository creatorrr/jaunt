from __future__ import annotations

from pathlib import Path

from jaunt.tester import run_pytest


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_run_pytest_passing_file(tmp_path: Path) -> None:
    p = tmp_path / "test_ok.py"
    _write(p, "def test_ok():\n    assert True\n")
    assert run_pytest([p], pytest_args=["-q"]) == 0


def test_run_pytest_failing_file(tmp_path: Path) -> None:
    p = tmp_path / "test_fail.py"
    _write(p, "def test_nope():\n    assert False\n")
    assert run_pytest([p], pytest_args=["-q"]) != 0


def test_run_pytest_multiple_files(tmp_path: Path) -> None:
    p1 = tmp_path / "test_a.py"
    p2 = tmp_path / "test_b.py"
    _write(p1, "def test_a():\n    assert 1 + 1 == 2\n")
    _write(p2, "def test_b():\n    assert 'x'.upper() == 'X'\n")
    assert run_pytest([p1, p2], pytest_args=["-q"]) == 0


def test_run_pytest_honors_pythonpath_and_cwd(tmp_path: Path) -> None:
    (tmp_path / "src" / "dice_demo").mkdir(parents=True, exist_ok=True)
    _write(tmp_path / "src" / "dice_demo" / "__init__.py", "VALUE = 1\n")

    test_file = tmp_path / "tests" / "test_import.py"
    _write(
        test_file,
        "\n".join(
            [
                "from dice_demo import VALUE",
                "",
                "def test_value() -> None:",
                "    assert VALUE == 1",
                "",
            ]
        ),
    )

    assert (
        run_pytest(
            [test_file],
            pytest_args=["-q"],
            pythonpath=[tmp_path / "src"],
            cwd=tmp_path,
        )
        == 0
    )


def test_run_pytest_empty_list_is_ok(tmp_path: Path) -> None:
    # This should be a no-op and not accidentally collect repo tests.
    assert run_pytest([], pytest_args=["-q"]) == 0

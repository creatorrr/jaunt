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


def test_run_pytest_empty_list_is_ok(tmp_path: Path) -> None:
    # This should be a no-op and not accidentally collect repo tests.
    assert run_pytest([], pytest_args=["-q"]) == 0


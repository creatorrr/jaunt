"""reconcile_entry over the call-plan IR: async functions + fixture split."""

from __future__ import annotations

from pathlib import Path

from jaunt.contract.runner import reconcile_entry
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _project(tmp_path: Path, module_src: str) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(module_src, encoding="utf-8")
    return tmp_path


def _entry(tmp_path: Path, qualname: str, obj) -> SpecEntry:
    return SpecEntry(
        kind="contract",
        spec_ref=normalize_spec_ref(f"mod:{qualname}"),
        module="mod",
        qualname=qualname,
        source_file=str(tmp_path / "src" / "mod.py"),
        obj=obj,
        decorator_kwargs={},
    )


ASYNC_SRC = '''
async def double(x: int) -> int:
    """Double it.

    Examples:
        - double(2) == 4
    """
    return x * 2
'''


def test_async_function_reconciles_and_battery_is_async(tmp_path: Path) -> None:
    root = _project(tmp_path, ASYNC_SRC)

    async def double(x: int) -> int:
        return x * 2

    res = reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        False,
        _entry(root, "double", double),
        module_namespace={"double": double},
        tool_version="t",
    )
    assert res.ok, res.failures
    text = res.battery_path.read_text(encoding="utf-8")
    assert "async def test_examples():" in text
    assert "assert await double(2) == 4" in text


FIXTURE_SRC = '''
def lookup(db, key: str) -> str:
    """Look up.

    Examples:
        - lookup(db, 'a') == 'A'

    Fixtures: db
    """
    return db[key]
'''


def test_fixture_case_validated_via_pytest_and_written(tmp_path: Path) -> None:
    root = _project(tmp_path, FIXTURE_SRC)
    conftest_dir = root / "tests" / "contract" / "mod"
    conftest_dir.mkdir(parents=True)
    (root / "tests" / "contract" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef db():\n    return {'a': 'A'}\n",
        encoding="utf-8",
    )

    def lookup(db, key):
        return db[key]

    res = reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        False,
        _entry(root, "lookup", lookup),
        module_namespace={"lookup": lookup},
        tool_version="t",
    )
    assert res.ok, res.failures
    text = res.battery_path.read_text(encoding="utf-8")
    assert "def test_examples(db):" in text
    # No validation temp file left behind.
    leftovers = list(res.battery_path.parent.glob("_jaunt_validate_*"))
    assert leftovers == []


def test_fixture_case_failure_writes_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path, FIXTURE_SRC)
    (root / "tests" / "contract").mkdir(parents=True)
    (root / "tests" / "contract" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef db():\n    return {'a': 'WRONG'}\n",
        encoding="utf-8",
    )

    def lookup(db, key):
        return db[key]

    res = reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        False,
        _entry(root, "lookup", lookup),
        module_namespace={"lookup": lookup},
        tool_version="t",
    )
    assert res.ok is False
    assert not res.battery_path.exists()
    assert list(res.battery_path.parent.glob("_jaunt_validate_*")) == []


def test_case_parse_error_reports_line(tmp_path: Path) -> None:
    src = (
        'def f(x):\n    """F.\n\n    Examples:\n        - f(mystery) == 1\n    """\n    return x\n'
    )
    root = _project(tmp_path, src)
    res = reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        False,
        _entry(root, "f", lambda x: x),
        module_namespace={"f": lambda x: x},
        tool_version="t",
    )
    assert res.ok is False
    assert any("mystery" in f for f in res.failures)
    assert not res.battery_path.exists()


def test_strength_excluded_in_header(tmp_path: Path) -> None:
    root = _project(tmp_path, FIXTURE_SRC)
    (root / "tests" / "contract").mkdir(parents=True)
    (root / "tests" / "contract" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef db():\n    return {'a': 'A'}\n",
        encoding="utf-8",
    )

    def lookup(db, key):
        return db[key]

    res = reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        True,  # strength enabled
        _entry(root, "lookup", lookup),
        module_namespace={"lookup": lookup},
        tool_version="t",
    )
    assert res.ok, res.failures
    assert "# jaunt:strength-excluded=1" in res.battery_path.read_text(encoding="utf-8")

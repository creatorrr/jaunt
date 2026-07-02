"""Whole-class contract mode: reconcile, drift, adopt/eject round-trip."""

from __future__ import annotations

from pathlib import Path

from jaunt.contract.runner import evaluate_entry, reconcile_entry, run_battery_file
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef

CLASS_SRC = '''
class Counter:
    """Counts things.

    Examples:
        - Counter(start=1).peek() == 1
    """

    def __init__(self, start: int = 0) -> None:
        self.n = start

    def peek(self) -> int:
        """Current value."""
        return self.n

    def increment(self, by: int) -> int:
        """Bump and return.

        Examples:
            - Counter().increment(2) == 2

        Raises:
            - Counter().increment(-1) raises ValueError
        """
        if by < 0:
            raise ValueError("negative")
        self.n += by
        return self.n
'''


class Counter:
    def __init__(self, start: int = 0) -> None:
        self.n = start

    def peek(self) -> int:
        return self.n

    def increment(self, by: int) -> int:
        if by < 0:
            raise ValueError("negative")
        self.n += by
        return self.n


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(CLASS_SRC, encoding="utf-8")
    return tmp_path


def _entry(root: Path) -> SpecEntry:
    return SpecEntry(
        kind="contract",
        spec_ref=SpecRef("mod:Counter"),
        module="mod",
        qualname="Counter",
        source_file=str(root / "src" / "mod.py"),
        obj=Counter,
        decorator_kwargs={},
    )


def _reconcile(root: Path, strength: bool = False):
    return reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        strength,
        _entry(root),
        module_namespace={"Counter": Counter},
        tool_version="t",
    )


def test_class_reconcile_writes_per_method_regions(tmp_path: Path) -> None:
    root = _project(tmp_path)
    res = _reconcile(root)
    assert res.ok, res.failures
    text = res.battery_path.read_text(encoding="utf-8")
    assert res.battery_path.name == "test_Counter.py"
    assert "from mod import Counter" in text
    assert "# >>> jaunt:derived examples" in text  # class-level block
    assert "# >>> jaunt:derived examples-increment" in text
    assert "# >>> jaunt:derived errors-increment" in text
    assert "assert Counter(start=1).peek() == 1" in text
    assert "assert Counter().increment(2) == 2" in text


def test_class_battery_actually_passes_pytest(tmp_path: Path) -> None:
    root = _project(tmp_path)
    res = _reconcile(root)
    assert run_battery_file(res.battery_path, root=root, source_roots=["src"]) is True


def test_class_reconcile_catches_bad_example(tmp_path: Path) -> None:
    root = _project(tmp_path)
    bad = CLASS_SRC.replace("- Counter().increment(2) == 2", "- Counter().increment(2) == 99")
    (root / "src" / "mod.py").write_text(bad, encoding="utf-8")
    res = _reconcile(root)
    assert res.ok is False
    assert not res.battery_path.exists()


def test_class_strength_namespace_includes_module_names(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path)
    (root / "src" / "mod.py").write_text("LIMIT = 10\n" + CLASS_SRC, encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_compute_case_strength(source, target, blocks, namespace):
        captured.update(namespace)
        return (0, 0, 0)

    monkeypatch.setattr(
        "jaunt.contract.strength.compute_case_strength",
        fake_compute_case_strength,
    )
    res = reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        True,
        _entry(root),
        module_namespace={"Counter": Counter, "LIMIT": 10},
        tool_version="t",
    )
    assert res.ok, res.failures
    assert captured["LIMIT"] == 10


class TestClassDriftMatrix:
    def _evaluated(self, root: Path):
        return evaluate_entry(
            root,
            "tests/contract",
            ["examples", "errors"],
            _entry(root),
            run_battery=lambda p: run_battery_file(p, root=root, source_roots=["src"]),
        )

    def test_in_sync_after_reconcile(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _reconcile(root)
        assert self._evaluated(root).state.value == "in-sync"

    def test_method_docstring_edit_is_stale_prose(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _reconcile(root)
        edited = CLASS_SRC.replace("Bump and return.", "Bump twice and return.")
        (root / "src" / "mod.py").write_text(edited, encoding="utf-8")
        assert self._evaluated(root).state.value == "stale-prose"

    def test_method_resignature_is_signature_drift(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _reconcile(root)
        edited = CLASS_SRC.replace(
            "def peek(self) -> int:", "def peek(self, default: int = 0) -> int:"
        )
        (root / "src" / "mod.py").write_text(edited, encoding="utf-8")
        assert self._evaluated(root).state.value == "signature-drift"

    def test_body_only_edit_is_refactored(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _reconcile(root)
        edited = CLASS_SRC.replace("self.n += by", "self.n = self.n + by")
        (root / "src" / "mod.py").write_text(edited, encoding="utf-8")
        assert self._evaluated(root).state.value == "refactored"


def test_adopt_rejects_method_ref(tmp_path: Path, monkeypatch, capsys) -> None:
    import jaunt.cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n', encoding="utf-8"
    )
    _project(tmp_path)
    code = jaunt.cli.main(["adopt", "mod:Counter.increment", "--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "whole class" in err

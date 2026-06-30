from __future__ import annotations

import json
from pathlib import Path

import pytest

from jaunt import heldout

pytest_plugins = ["pytester"]


def test_plugin_classifies_tiers_with_real_pytest_run(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "heldout-report.json"
    monkeypatch.setenv(heldout.REPORT_ENV, str(report_path))
    pytester.makepyfile(
        test_tiers="""
        import pytest


        @pytest.mark.jaunt_tier("example")
        def test_example_passes():
            assert True


        @pytest.mark.jaunt_tier("derived")
        def test_derived_fails():
            assert 41 == 42


        def test_unmarked_fails():
            assert 41 == 42


        @pytest.mark.jaunt_tier("weird")
        def test_weird_fails():
            assert 41 == 42
        """,
    )

    result = pytester.runpytest_subprocess("-p", "jaunt.heldout", "-q")
    result.assert_outcomes(passed=1, failed=3)
    report = heldout.load_report(report_path)

    records_by_nodeid: dict[str, list[dict]] = {}
    for item in report["items"]:
        records_by_nodeid.setdefault(item["nodeid"], []).append(item)

    def tiers_for(test_name: str) -> set[str]:
        matches = [
            item
            for nodeid, records in records_by_nodeid.items()
            if nodeid.endswith(f"::{test_name}")
            for item in records
        ]
        assert matches
        return {str(item["tier"]) for item in matches}

    assert tiers_for("test_example_passes") == {"example"}
    assert tiers_for("test_unmarked_fails") == {"derived"}
    assert tiers_for("test_weird_fails") == {"derived"}


def test_assign_opaque_ids_is_stable_and_sorted() -> None:
    report = {
        "items": [
            {
                "nodeid": "t.py::test_b",
                "tier": "derived",
                "outcome": "failed",
                "exception_class": "AssertionError",
            },
            {
                "nodeid": "t.py::test_a",
                "tier": "derived",
                "outcome": "failed",
                "exception_class": "ValueError",
            },
            {
                "nodeid": "t.py::test_ex",
                "tier": "example",
                "outcome": "failed",
                "exception_class": "AssertionError",
            },
        ],
        "collection_errors": [],
    }

    expected = {"t.py::test_a": "derived#1", "t.py::test_b": "derived#2"}
    assert heldout.assign_opaque_ids(report) == expected
    assert heldout.assign_opaque_ids(report) == expected


def test_build_repair_feedback_keeps_example_detail() -> None:
    report = {
        "items": [
            {
                "nodeid": "t.py::test_example",
                "tier": "example",
                "outcome": "failed",
                "exception_class": "AssertionError",
                "longrepr": "E       assert 41 == 42",
                "capstdout": "stdout-here",
                "capstderr": "",
                "warnings": [],
                "phase": "call",
            }
        ],
        "collection_errors": [],
    }

    joined = "\n".join(heldout.build_repair_feedback(report))
    assert "t.py::test_example" in joined
    assert "AssertionError" in joined
    assert "assert 41 == 42" in joined
    assert "stdout-here" in joined


def test_build_repair_feedback_redacts_derived_detail() -> None:
    report = {
        "items": [
            {
                "nodeid": "t.py::test_derived_01",
                "tier": "derived",
                "outcome": "failed",
                "exception_class": "AssertionError",
                "longrepr": "E   assert 41 == 42",
                "capstdout": "secret-stdout",
                "capstderr": "",
                "warnings": [],
                "phase": "call",
            }
        ],
        "collection_errors": [],
    }

    lines = heldout.build_repair_feedback(report)
    assert lines == ["derived#1: AssertionError"]
    joined = "\n".join(lines)
    for leaked in (
        "41",
        "42",
        "assert",
        "==",
        "longrepr",
        "secret-stdout",
        "test_derived_01",
        "Traceback",
    ):
        assert leaked not in joined


def test_build_repair_feedback_treats_missing_and_unknown_tiers_as_derived() -> None:
    report = {
        "items": [
            {
                "nodeid": "t.py::test_missing_tier",
                "outcome": "failed",
                "exception_class": "ValueError",
                "longrepr": "E   assert secret_missing == expected",
                "capstdout": "",
                "capstderr": "",
                "warnings": [],
                "phase": "call",
            },
            {
                "nodeid": "t.py::test_weird_tier",
                "tier": "weird",
                "outcome": "failed",
                "exception_class": "AssertionError",
                "longrepr": "E   assert secret_weird == expected",
                "capstdout": "",
                "capstderr": "",
                "warnings": [],
                "phase": "call",
            },
        ],
        "collection_errors": [],
    }

    lines = heldout.build_repair_feedback(report)
    assert lines == ["derived#1: ValueError", "derived#2: AssertionError"]
    joined = "\n".join(lines)
    assert "secret_missing" not in joined
    assert "secret_weird" not in joined
    assert "expected" not in joined


def test_build_repair_feedback_redacts_collection_errors() -> None:
    report = {
        "items": [],
        "collection_errors": [
            {
                "module": "t.py",
                "tier": "derived",
                "exception_class": "ImportError",
                "longrepr": "Traceback ... secret-import-detail",
                "outcome": "failed",
            }
        ],
    }

    lines = heldout.build_repair_feedback(report)
    assert lines == ["collection error in t.py: ImportError"]
    joined = "\n".join(lines)
    assert "secret-import-detail" not in joined
    assert "Traceback" not in joined


def test_build_repair_feedback_no_redaction_keeps_derived_detail() -> None:
    report = {
        "items": [
            {
                "nodeid": "t.py::test_derived",
                "tier": "derived",
                "outcome": "failed",
                "exception_class": "AssertionError",
                "longrepr": "E   assert 41 == 42",
                "capstdout": "",
                "capstderr": "",
                "warnings": [],
                "phase": "call",
            }
        ],
        "collection_errors": [],
    }

    joined = "\n".join(heldout.build_repair_feedback(report, redact=False))
    assert "assert 41 == 42" in joined


def test_build_repair_feedback_empty_or_non_failed_report_uses_redacted_fallback() -> None:
    fallback = ["tests failed; details withheld (held-out tier)"]
    assert heldout.build_repair_feedback({}) == fallback
    assert (
        heldout.build_repair_feedback(
            {
                "items": [
                    {
                        "nodeid": "t.py::test_passes",
                        "tier": "derived",
                        "outcome": "passed",
                        "exception_class": None,
                    }
                ],
                "collection_errors": [],
            }
        )
        == fallback
    )
    for raw_pytest_marker in ("FAILED", "assert", "====", "short test summary"):
        assert raw_pytest_marker not in fallback[0]


def test_load_report_returns_empty_dict_for_unusable_input(tmp_path: Path) -> None:
    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{not json", encoding="utf-8")
    assert heldout.load_report(invalid_json) == {}
    assert heldout.load_report(tmp_path / "missing.json") == {}

    non_dict_json = tmp_path / "list.json"
    non_dict_json.write_text(json.dumps([]), encoding="utf-8")
    assert heldout.load_report(non_dict_json) == {}

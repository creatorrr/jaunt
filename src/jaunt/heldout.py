"""Held-out pytest reporting and tiered repair-feedback redaction."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

JAUNT_TIER_MARK = "jaunt_tier"
TIER_EXAMPLE = "example"
TIER_DERIVED = "derived"
REPORT_ENV = "JAUNT_HELDOUT_REPORT"

_ITEM_RECORDS: list[dict[str, Any]] = []
_COLLECTION_ERRORS: list[dict[str, Any]] = []
_ITEM_TIERS: dict[str, str] = {}
_EXCEPTION_CLASSES: dict[tuple[str, str], str] = {}


def _hookimpl(*, hookwrapper: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        func.pytest_impl = {  # type: ignore[attr-defined]
            "hookwrapper": hookwrapper,
            "wrapper": False,
            "optionalhook": False,
            "tryfirst": False,
            "trylast": False,
            "specname": None,
        }
        return func

    return decorate


def _normalize_tier(value: object) -> str:
    if value == TIER_EXAMPLE:
        return TIER_EXAMPLE
    return TIER_DERIVED


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _string_or_empty(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _warning_lines_from_sections(sections: object) -> list[str]:
    warnings: list[str] = []
    if not isinstance(sections, (list, tuple)):
        return warnings
    for section in sections:
        if not isinstance(section, (list, tuple)) or len(section) < 2:
            continue
        name, content = section[0], section[1]
        if not isinstance(name, str) or "warning" not in name.lower():
            continue
        if not isinstance(content, str):
            continue
        warnings.extend(line.strip() for line in content.splitlines() if line.strip())
    return warnings


def _exception_class_from_text(text: str | None) -> str | None:
    if not text:
        return None
    suffixes = ("Error", "Exception")
    exact_names = {"AssertionError", "Failed", "KeyboardInterrupt", "SystemExit"}
    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("E   "):
            line = line[4:].strip()
        head = line.split(":", 1)[0].strip()
        token = head.rsplit(".", 1)[-1]
        if token in exact_names or token.endswith(suffixes):
            return token
    return None


def _safe_exception_class(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    return "error"


def _item_records(report: dict) -> list[dict[str, Any]]:
    items = report.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _collection_error_records(report: dict) -> list[dict[str, Any]]:
    collection_errors = report.get("collection_errors")
    if not isinstance(collection_errors, list):
        return []
    return [error for error in collection_errors if isinstance(error, dict)]


def _full_item_feedback(record: dict[str, Any]) -> list[str]:
    nodeid = _string_or_empty(record.get("nodeid")) or "<unknown test>"
    exception_class = _safe_exception_class(record.get("exception_class"))
    longrepr = _string_or_none(record.get("longrepr"))
    capstdout = _string_or_none(record.get("capstdout"))
    capstderr = _string_or_none(record.get("capstderr"))

    lines = [f"{nodeid}: {exception_class}: {longrepr or 'failed'}"]
    if capstdout:
        lines.append(f"{nodeid}: captured stdout:\n{capstdout}")
    if capstderr:
        lines.append(f"{nodeid}: captured stderr:\n{capstderr}")
    return lines


def _full_collection_feedback(record: dict[str, Any]) -> list[str]:
    module = _string_or_empty(record.get("module")) or "<unknown module>"
    exception_class = _safe_exception_class(record.get("exception_class"))
    longrepr = _string_or_none(record.get("longrepr"))
    return [f"collection error in {module}: {exception_class}: {longrepr or 'failed'}"]


def _assert_no_derived_detail_leaks(report: dict, lines: list[str]) -> None:
    sensitive: list[str] = []
    for record in _item_records(report):
        if _normalize_tier(record.get("tier")) != TIER_DERIVED:
            continue
        for key in ("longrepr", "capstdout", "capstderr"):
            value = _string_or_none(record.get(key))
            if value:
                sensitive.append(value)
    for record in _collection_error_records(report):
        value = _string_or_none(record.get("longrepr"))
        if value:
            sensitive.append(value)

    assert not any(secret in line for secret in sensitive for line in lines), (
        "derived held-out failure detail leaked into repair feedback"
    )


def pytest_configure(config: Any) -> None:
    """Register held-out markers and reset per-session report state."""
    _ITEM_RECORDS.clear()
    _COLLECTION_ERRORS.clear()
    _ITEM_TIERS.clear()
    _EXCEPTION_CLASSES.clear()
    config.addinivalue_line(
        "markers",
        "jaunt_tier(name): mark a test as a Jaunt held-out tier: example or derived",
    )


@_hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Any:
    """Capture item marker tier and exception class while pytest still has the item."""
    marker = item.get_closest_marker(JAUNT_TIER_MARK)
    tier = TIER_DERIVED
    if marker is not None and marker.args:
        tier = _normalize_tier(marker.args[0])
    nodeid = str(item.nodeid)
    when = str(call.when)
    _ITEM_TIERS[nodeid] = tier
    if call.excinfo is not None:
        _EXCEPTION_CLASSES[(nodeid, when)] = call.excinfo.type.__name__

    outcome = yield
    return outcome


def pytest_runtest_logreport(report: Any) -> None:
    """Append one structured record for each item phase report."""
    nodeid = str(report.nodeid)
    phase = str(report.when)
    longrepr = str(report.longrepr) if report.longrepr else None
    _ITEM_RECORDS.append(
        {
            "nodeid": nodeid,
            "tier": _ITEM_TIERS.get(nodeid, TIER_DERIVED),
            "outcome": str(report.outcome),
            "exception_class": _EXCEPTION_CLASSES.get((nodeid, phase)),
            "longrepr": longrepr,
            "capstdout": getattr(report, "capstdout", "") or "",
            "capstderr": getattr(report, "capstderr", "") or "",
            "warnings": _warning_lines_from_sections(getattr(report, "sections", [])),
            "phase": phase,
        }
    )


def pytest_collectreport(report: Any) -> None:
    """Capture collection and import failures as derived-tier records."""
    if not report.failed:
        return
    longrepr = str(report.longrepr) if report.longrepr else None
    module = getattr(report, "nodeid", None) or getattr(report, "fspath", None) or ""
    _COLLECTION_ERRORS.append(
        {
            "module": str(module),
            "tier": TIER_DERIVED,
            "exception_class": _exception_class_from_text(longrepr),
            "longrepr": longrepr,
            "outcome": "failed",
        }
    )


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """Write the held-out JSON report when JAUNT_HELDOUT_REPORT is set."""
    del session, exitstatus
    path = os.environ.get(REPORT_ENV)
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": _ITEM_RECORDS, "collection_errors": _COLLECTION_ERRORS}
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_report(path: str | Path) -> dict:
    """Load a held-out JSON report, returning an empty dict for unusable input."""
    try:
        content = Path(path).read_text(encoding="utf-8")
        if not content.strip():
            return {}
        parsed = json.loads(content)
    except (OSError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def assign_opaque_ids(report: dict) -> dict[str, str]:
    """Assign stable derived#N identifiers to derived-tier item nodeids."""
    nodeids = {
        item["nodeid"]
        for item in _item_records(report)
        if isinstance(item.get("nodeid"), str) and _normalize_tier(item.get("tier")) == TIER_DERIVED
    }
    return {nodeid: f"derived#{index}" for index, nodeid in enumerate(sorted(nodeids), start=1)}


def build_repair_feedback(report: dict, *, redact: bool = True) -> list[str]:
    """Build human-readable repair feedback, redacting derived-tier details by default."""
    opaque_ids = assign_opaque_ids(report)
    lines: list[str] = []
    emitted_derived: set[str] = set()

    for record in _item_records(report):
        if record.get("outcome") != "failed":
            continue
        if not redact:
            lines.extend(_full_item_feedback(record))
            continue

        tier = _normalize_tier(record.get("tier"))
        nodeid = _string_or_empty(record.get("nodeid"))
        if tier == TIER_EXAMPLE:
            lines.extend(_full_item_feedback(record))
            continue
        if nodeid in emitted_derived:
            continue
        emitted_derived.add(nodeid)
        opaque_id = opaque_ids.get(nodeid, "derived#unknown")
        exception_class = _safe_exception_class(record.get("exception_class"))
        lines.append(f"{opaque_id}: {exception_class}")

    for record in _collection_error_records(report):
        if not redact:
            lines.extend(_full_collection_feedback(record))
            continue
        module = _string_or_empty(record.get("module")) or "<unknown module>"
        exception_class = _safe_exception_class(record.get("exception_class"))
        lines.append(f"collection error in {module}: {exception_class}")

    if not lines:
        lines = ["tests failed; details withheld (held-out tier)"]

    if redact:
        _assert_no_derived_detail_leaks(report, lines)

    return lines

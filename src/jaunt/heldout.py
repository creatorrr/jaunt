"""Held-out pytest reporting and tiered repair-feedback redaction."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import jaunt

jaunt.magic_module(
    __name__,
    prompt=(
        "This module classifies pytest failure records into two tiers — "
        '"example" and "derived" (the module constants TIER_EXAMPLE and '
        "TIER_DERIVED) — and produces repair feedback that withholds all "
        "detail for derived-tier failures (the held-out barrier). The governed "
        "functions reuse handwritten module-level helpers that live in this same "
        "source module (jaunt.heldout); reach them exactly as the guard module "
        "does — import the source module (import_module('jaunt.heldout')) and read "
        "the helper off it — do not reimplement them. Those handwritten helpers "
        "are: _item_records(report) -> list[dict] (report['items'] with non-dicts "
        "dropped, [] if items is not a list), _collection_error_records(report) -> "
        "list[dict] (same for report['collection_errors']), _normalize_tier(value) "
        "-> str (returns 'example' only when value == 'example', else 'derived'), "
        "_string_or_empty(value) -> str (value if it is a str, else ''), "
        "_string_or_none(value) -> str | None (value if it is a non-empty str, "
        "else None), _safe_exception_class(value) -> str (value if it is a "
        "non-empty str, else 'error'), _full_item_feedback(record) -> list[str] "
        "(unredacted per-item feedback lines), _full_collection_feedback(record) -> "
        "list[str] (unredacted collection-error feedback lines), and "
        "_assert_no_derived_detail_leaks(report, lines) -> None (asserts no derived "
        "longrepr/capstdout/capstderr/collection-longrepr substring appears in "
        "lines). Never weaken or skip the leak assertion when redacting."
    ),
)

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
    import json
    import os

    del session, exitstatus
    path = os.environ.get(REPORT_ENV)
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": _ITEM_RECORDS, "collection_errors": _COLLECTION_ERRORS}
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_report(path: str | Path) -> dict:
    """Load a held-out JSON report from ``path``, returning ``{}`` for unusable input.

    Signature is fixed: ``load_report(path: str | Path) -> dict``.

    Procedure:

    1. Read the file text at ``path`` (coerce through ``pathlib.Path`` first) using
       UTF-8 encoding.
    2. If the text is empty or contains only whitespace, return ``{}``.
    3. Otherwise parse it as JSON.
    4. If the parsed value is not a ``dict`` (e.g. a JSON list, string, or number),
       return ``{}``. Otherwise return the parsed dict unchanged.

    This function never raises for bad input. Any ``OSError`` (missing/unreadable
    file) or ``ValueError`` (which includes ``json.JSONDecodeError``) raised while
    reading or parsing is caught and turned into a ``{}`` return.

    ``path`` is read at call time; nothing here depends on module-level mutable state.

    Examples:
    - A path to a file containing ``{"items": []}`` returns ``{"items": []}``.
    - A path to a file containing ``"{not json"`` returns ``{}``.
    - A path to a missing file returns ``{}``.
    - A path to a file containing ``[]`` (a JSON list, not a dict) returns ``{}``.
    - A path to an empty or whitespace-only file returns ``{}``.
    """
    raise NotImplementedError


def assign_opaque_ids(report: dict) -> dict[str, str]:
    """Assign stable ``derived#N`` identifiers to derived-tier item nodeids.

    Signature is fixed: ``assign_opaque_ids(report: dict) -> dict[str, str]``.

    Collect the set of distinct ``nodeid`` values from the report's item records —
    obtained via the handwritten helper ``_item_records(report)`` — keeping only
    records where ``record.get("nodeid")`` is a ``str`` AND the record's tier
    (``_normalize_tier(record.get("tier"))``) is the derived tier (the module
    constant ``TIER_DERIVED`` == ``"derived"``). Example-tier items and items with a
    non-string nodeid are excluded entirely.

    Sort those nodeids in ascending (lexicographic) order and enumerate them
    starting at 1. Return a dict mapping each nodeid to ``f"derived#{index}"``.
    Because a ``set`` is used, duplicate nodeids collapse to one entry, and the
    result is deterministic and stable across repeated calls with the same report.

    ``report`` is read at call time; nothing here depends on module-level mutable
    state.

    Examples:
    - A report whose derived-tier items have nodeids ``"t.py::test_b"`` and
      ``"t.py::test_a"`` (plus an ``example``-tier ``"t.py::test_ex"``) returns
      ``{"t.py::test_a": "derived#1", "t.py::test_b": "derived#2"}`` — sorted, and
      the example-tier item is omitted.
    - An empty or item-less report returns ``{}``.
    """
    raise NotImplementedError


def build_repair_feedback(report: dict, *, redact: bool = True) -> list[str]:
    """Build human-readable repair feedback, redacting derived-tier detail by default.

    Signature is fixed:
    ``build_repair_feedback(report: dict, *, redact: bool = True) -> list[str]``.

    This is the held-out barrier: with ``redact=True`` (the default), full failure
    detail is emitted only for ``example``-tier failures, while ``derived``-tier
    failures (the held-out battery) are collapsed to an opaque
    ``derived#N: <ExceptionClass>`` line that leaks none of their longrepr / captured
    output. With ``redact=False`` every failure gets its full detail (debug mode).

    First compute ``opaque_ids = assign_opaque_ids(report)`` (the derived-tier
    nodeid -> ``derived#N`` mapping). Build an ordered list of feedback ``lines`` and
    track a set of derived nodeids already emitted (``emitted_derived``).

    Item records (iterate ``_item_records(report)`` in order):

    - Skip any record whose ``record.get("outcome")`` is not exactly ``"failed"``.
    - If NOT redacting: append ``_full_item_feedback(record)`` (the handwritten
      helper producing the full unredacted lines) and move on.
    - If redacting: compute ``tier = _normalize_tier(record.get("tier"))`` and
      ``nodeid = _string_or_empty(record.get("nodeid"))``.
      - If the tier is the example tier (``TIER_EXAMPLE`` == ``"example"``), append
        ``_full_item_feedback(record)`` (example failures are shown in full).
      - Otherwise (derived tier): if this ``nodeid`` is already in
        ``emitted_derived``, skip it (one opaque line per derived nodeid, so the
        setup/call/teardown phases of one test do not each emit a line). Otherwise
        add it to ``emitted_derived`` and append a single line
        ``f"{opaque_id}: {exception_class}"`` where ``opaque_id`` is
        ``opaque_ids.get(nodeid, "derived#unknown")`` and ``exception_class`` is
        ``_safe_exception_class(record.get("exception_class"))``.

    Collection-error records (iterate ``_collection_error_records(report)`` in
    order, after all item records):

    - If NOT redacting: append ``_full_collection_feedback(record)``.
    - If redacting: append a single line
      ``f"collection error in {module}: {exception_class}"`` where ``module`` is
      ``_string_or_empty(record.get("module")) or "<unknown module>"`` and
      ``exception_class`` is ``_safe_exception_class(record.get("exception_class"))``.

    If after all of the above ``lines`` is empty (no failures at all), set it to the
    single redacted fallback line ``["tests failed; details withheld (held-out
    tier)"]``.

    Finally, when redacting, call ``_assert_no_derived_detail_leaks(report, lines)``
    as a defensive check that no derived-tier longrepr/captured-output substring
    leaked into the returned lines (never skip or weaken this). Return ``lines``.

    ``report`` is read at call time; nothing here depends on module-level mutable
    state.

    Examples:
    - An ``example``-tier failed item with longrepr ``"E       assert 41 == 42"``
      and ``capstdout="stdout-here"`` yields lines that contain its full nodeid,
      exception class, the ``assert 41 == 42`` text, and ``stdout-here``.
    - A single ``derived``-tier failed item (nodeid ``"t.py::test_derived_01"``,
      ``AssertionError``, longrepr ``"E   assert 41 == 42"``,
      ``capstdout="secret-stdout"``) yields exactly ``["derived#1:
      AssertionError"]`` — none of ``41``, ``42``, ``assert``, ``secret-stdout``, or
      the raw nodeid appear.
    - Items with a missing tier and with tier ``"weird"`` are both treated as
      derived, yielding e.g. ``["derived#1: ValueError", "derived#2:
      AssertionError"]`` with no longrepr text leaked.
    - A redacted collection error (module ``"t.py"``, ``ImportError``, longrepr
      ``"Traceback ... secret-import-detail"``) yields exactly ``["collection error
      in t.py: ImportError"]`` — no ``Traceback`` or ``secret-import-detail``.
    - The same single derived item with ``redact=False`` yields lines that DO
      contain the full ``assert 41 == 42`` detail.
    - An empty report ``{}`` or a report with only passed (non-``"failed"``) items
      returns ``["tests failed; details withheld (held-out tier)"]``.
    """
    raise NotImplementedError

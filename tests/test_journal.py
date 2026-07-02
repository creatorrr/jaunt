from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jaunt import journal


def _event(**kw: Any) -> journal.JournalEvent:
    defaults: dict[str, Any] = dict(
        action="build",
        module="recall.compress",
        detail="prose change (gate: MEANINGFUL); battery 47/47",
        job_id="a1b2c3d4",
        when=datetime(2026, 7, 1, 14, 32, tzinfo=UTC),
    )
    defaults.update(kw)
    return journal.JournalEvent(**defaults)


def test_format_line_layout():
    line = journal.format_line(_event())
    assert line == (
        "2026-07-01 14:32Z build    recall.compress — "
        "prose change (gate: MEANINGFUL); battery 47/47; job a1b2c3d4"
    )


def test_format_line_without_job_id():
    line = journal.format_line(
        _event(action="refreeze", job_id=None, detail="cosmetic (gate: EQUIVALENT)")
    )
    assert line.endswith("recall.compress — cosmetic (gate: EQUIVALENT)")
    assert "job" not in line


def test_append_requires_existing_file_unless_create(tmp_path: Path):
    assert journal.append_events(tmp_path, [_event()]) is False
    assert not (tmp_path / journal.JOURNAL_FILE).exists()
    assert journal.append_events(tmp_path, [_event()], create=True) is True
    assert journal.append_events(tmp_path, [_event(action="adopt")]) is True
    text = (tmp_path / journal.JOURNAL_FILE).read_text(encoding="utf-8")
    assert text.count("\n") == 2


def test_append_rejects_newlines_in_detail(tmp_path: Path):
    with pytest.raises(ValueError):
        journal.append_events(tmp_path, [_event(detail="two\nlines")], create=True)


def test_read_lines_tail_and_module_filter(tmp_path: Path):
    events = [
        _event(module="recall.rank", detail="d1"),
        _event(module="record.plan", detail="d2"),
        _event(module="recall.rank", detail="d3"),
    ]
    journal.append_events(tmp_path, events, create=True)
    assert len(journal.read_lines(tmp_path, limit=2)) == 2
    ranked = journal.read_lines(tmp_path, module="recall.rank")
    assert len(ranked) == 2
    assert all("recall.rank" in ln for ln in ranked)


def test_read_lines_sorts_by_timestamp_before_limit(tmp_path: Path):
    events = [
        _event(
            module="recall.rank", detail="newest", when=datetime(2026, 7, 1, 14, 40, tzinfo=UTC)
        ),
        _event(
            module="recall.rank", detail="oldest", when=datetime(2026, 7, 1, 14, 20, tzinfo=UTC)
        ),
        _event(
            module="recall.rank", detail="middle", when=datetime(2026, 7, 1, 14, 30, tzinfo=UTC)
        ),
    ]
    journal.append_events(tmp_path, events, create=True)

    lines = journal.read_lines(tmp_path, limit=2)

    assert "middle" in lines[0]
    assert "newest" in lines[1]


def test_ensure_union_merge_attribute(tmp_path: Path):
    assert journal.ensure_union_merge_attribute(tmp_path) is True
    attrs = (tmp_path / ".gitattributes").read_text(encoding="utf-8")
    assert "JAUNT_LOG merge=union" in attrs
    assert journal.ensure_union_merge_attribute(tmp_path) is False  # idempotent

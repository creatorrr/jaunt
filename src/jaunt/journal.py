"""Committed JAUNT_LOG change journal: terse, append-only, one line per event."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

JOURNAL_FILE = "JAUNT_LOG"
_ATTR_LINE = "JAUNT_LOG merge=union"
_ACTION_WIDTH = 8
_TIMESTAMP_WIDTH = len("YYYY-MM-DD HH:MMZ")


@dataclass(frozen=True)
class JournalEvent:
    action: str
    module: str
    detail: str
    job_id: str | None = None
    when: datetime | None = None


def format_line(event: JournalEvent) -> str:
    when = event.when or datetime.now(tz=UTC)
    stamp = when.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")
    line = f"{stamp} {event.action:<{_ACTION_WIDTH}} {event.module} — {event.detail}"
    if event.job_id:
        line += f"; job {event.job_id}"
    return line


def append_events(root: Path, events: Sequence[JournalEvent], *, create: bool = False) -> bool:
    """Append one line per event. Opt-in via file presence unless create=True."""
    path = root / JOURNAL_FILE
    if not path.exists() and not create:
        return False
    lines = []
    for event in events:
        for field in (event.action, event.module, event.detail):
            if "\n" in field or "\r" in field:
                raise ValueError(f"journal fields must be single-line: {field!r}")
        lines.append(format_line(event))
    if not lines:
        return path.exists()
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return True


def read_lines(root: Path, *, limit: int = 20, module: str | None = None) -> list[str]:
    path = root / JOURNAL_FILE
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    lines = sorted(lines, key=lambda ln: ln[:_TIMESTAMP_WIDTH])
    if module is not None:
        lines = [ln for ln in lines if f" {module} — " in ln]
    return lines[-limit:] if limit else lines


def ensure_union_merge_attribute(root: Path) -> bool:
    """Add `JAUNT_LOG merge=union` to .gitattributes if missing. Returns True if added."""
    path = root / ".gitattributes"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if _ATTR_LINE in existing.splitlines():
        return False
    joiner = "" if (not existing or existing.endswith("\n")) else "\n"
    path.write_text(existing + joiner + _ATTR_LINE + "\n", encoding="utf-8")
    return True

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
    if create and not path.exists():
        path.touch()
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


_DAEMON_ACTIONS = frozenset(
    {
        "build",
        "refreeze",
        "job-fail",
        "job-park",
        "job-supersede",
        "job-propose",
        "job-discard",
        "probe-fail",
    }
)


def _is_daemon_addition(line: str) -> bool:
    """True if a unified-diff `+` line is a well-formed daemon-authored journal entry."""
    if line.startswith("+++"):
        return False
    if not line.startswith("+"):
        return False
    parts = line[1:].split(maxsplit=3)
    if len(parts) < 4:
        return False
    date, timestamp, action, _rest = parts
    if len(date) != 10 or date[4] != "-" or date[7] != "-":
        return False
    if len(timestamp) != 6 or timestamp[2] != ":" or timestamp[-1] != "Z":
        return False
    return action in _DAEMON_ACTIONS


def user_dirty(root: Path) -> bool:
    """True if JAUNT_LOG has uncommitted changes other than daemon-authored additions.

    Both the daemon auto-commit path and the ``jaunt jobs land`` CLI use this to
    classify pending journal edits identically: appended daemon lines (build,
    refreeze, job-*) are safe to sweep into a provenance commit, but any user edit
    (deletions, modifications, non-daemon additions, or an untracked file) means we
    must refuse rather than commit the user's unrelated work.
    """
    from jaunt import landing

    status = landing.git_out(root, "status", "--porcelain", "--", JOURNAL_FILE).strip()
    if not status:
        return False
    if status.startswith("??"):
        return True

    has_daemon_addition = False
    for diff_args in (("diff", "--unified=0"), ("diff", "--cached", "--unified=0")):
        diff = landing.git_out(root, *diff_args, "--", JOURNAL_FILE)
        for line in diff.splitlines():
            if line.startswith(("diff --git", "index ", "@@ ", "--- ", "+++ ")):
                continue
            if _is_daemon_addition(line):
                has_daemon_addition = True
                continue
            if line.startswith(("+", "-")):
                return True
    return not has_daemon_addition


def ensure_union_merge_attribute(root: Path) -> bool:
    """Add `JAUNT_LOG merge=union` to .gitattributes if missing. Returns True if added."""
    path = root / ".gitattributes"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if _ATTR_LINE in existing.splitlines():
        return False
    joiner = "" if (not existing or existing.endswith("\n")) else "\n"
    path.write_text(existing + joiner + _ATTR_LINE + "\n", encoding="utf-8")
    return True

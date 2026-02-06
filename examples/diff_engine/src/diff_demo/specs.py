"""
Diff / Patch Engine — Jaunt Example

A line-oriented text diff engine that computes minimal edit scripts,
formats them as unified diffs, and can apply patches to reconstruct
the target text.  Uses a standard LCS-based (longest common
subsequence) algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

import jaunt


# ── Data types ───────────────────────────────────────────────────────


class DiffOp(Enum):
    """The kind of edit operation for a single line."""

    EQUAL = auto()  # line is unchanged
    INSERT = auto()  # line was added
    DELETE = auto()  # line was removed


@dataclass(frozen=True)
class DiffLine:
    """
    One line of a diff result.

    Attributes:
        op:          the edit operation
        content:     the text of the line (without trailing newline)
        old_lineno:  1-based line number in the old text, or None for INSERTs
        new_lineno:  1-based line number in the new text, or None for DELETEs
    """

    op: DiffOp
    content: str
    old_lineno: int | None
    new_lineno: int | None


# ── Core diff ────────────────────────────────────────────────────────


@jaunt.magic()
def compute_diff(old: str, new: str) -> list[DiffLine]:
    """
    Compute a line-level diff between two texts.

    Algorithm:
    1. Split each text on "\\n" into lines.
       - An empty string produces a single empty-string line: [""].
    2. Compute the longest common subsequence (LCS) of the two line
       lists.  Use the standard dynamic-programming O(m*n) algorithm.
    3. Walk both line lists and the LCS to emit DiffLine entries:
       - Lines in both old and LCS → EQUAL
       - Lines in old but not in LCS → DELETE
       - Lines in new but not in LCS → INSERT
       Group operations so that DELETEs for a changed region appear
       before INSERTs (matching unified-diff convention).

    Line numbering:
    - old_lineno counts from 1 for old lines, None for INSERTs.
    - new_lineno counts from 1 for new lines, None for DELETEs.
    - EQUAL lines have both old_lineno and new_lineno set.

    Edge cases:
    - If old == new, every line is EQUAL.
    - If old is empty string and new is non-empty (or vice-versa), the
      result is all INSERTs (or all DELETEs) respectively.
    """
    raise RuntimeError("spec stub")


# ── Unified format ───────────────────────────────────────────────────


@jaunt.magic(deps=[compute_diff])
def format_unified(
    old: str,
    new: str,
    *,
    old_name: str = "a",
    new_name: str = "b",
    context: int = 3,
) -> str:
    """
    Produce a unified-diff formatted string (like ``diff -u``).

    Structure:
    1. Header:
         --- {old_name}
         +++ {new_name}
    2. One or more hunks.  Each hunk:
       - Starts with @@ -{old_start},{old_count} +{new_start},{new_count} @@
       - Contains context, added, and removed lines prefixed by ' ', '+', '-'

    Hunk generation rules:
    - A hunk is triggered by any INSERT or DELETE line.
    - Include `context` unchanged lines before and after each change.
    - If two change regions are separated by ≤ 2*context unchanged
      lines, merge them into a single hunk.
    - old_start / new_start are 1-based.
    - old_count is the number of lines from the old file in this hunk
      (context lines + deleted lines).
    - new_count is the number of lines from the new file in this hunk
      (context lines + inserted lines).

    Edge cases:
    - If old == new, return an empty string (no diff to show).
    - context must be >= 0; raise ValueError if negative.

    The output should end with a trailing newline.
    """
    raise RuntimeError("spec stub")


# ── Patch application ────────────────────────────────────────────────


@jaunt.magic(deps=[compute_diff])
def apply_patch(old: str, diff_lines: list[DiffLine]) -> str:
    """
    Reconstruct the new text by applying diff_lines to old.

    Algorithm:
    - Walk through diff_lines in order.
    - For EQUAL lines: copy the line from old (verify content matches).
    - For DELETE lines: skip the line in old (verify content matches).
    - For INSERT lines: emit the DiffLine content into the output.
    - After processing all diff_lines, there should be no remaining
      old lines.

    Errors:
    - Raise ValueError("patch conflict at line {n}") if an EQUAL or
      DELETE line's content does not match the corresponding old line
      (use 1-based numbering of the old text for {n}).
    - Raise ValueError("patch incomplete") if diff_lines are exhausted
      but old lines remain, or vice-versa.

    Return the reconstructed text as a single string (lines joined by
    "\\n").
    """
    raise RuntimeError("spec stub")


# ── Statistics ───────────────────────────────────────────────────────


@jaunt.magic(deps=[compute_diff])
def diff_stats(old: str, new: str) -> dict[str, int]:
    """
    Return summary statistics for a diff.

    Keys in the returned dict:
      "insertions" — number of INSERT lines
      "deletions"  — number of DELETE lines
      "unchanged"  — number of EQUAL lines
      "total_old"  — total lines in old text
      "total_new"  — total lines in new text

    If both texts are empty strings, all counts should be consistent:
    insertions=0, deletions=0, unchanged=1 (the single empty line),
    total_old=1, total_new=1.
    """
    raise RuntimeError("spec stub")

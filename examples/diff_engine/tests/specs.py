"""Tests for the diff / patch engine."""

from __future__ import annotations

import jaunt


@jaunt.test()
def test_identical_texts():
    """
    compute_diff on two identical multi-line texts should produce only
    EQUAL DiffLines.  Every line should have both old_lineno and
    new_lineno set and matching.
    """


@jaunt.test()
def test_completely_different_texts():
    """
    old = "aaa\\nbbb"
    new = "xxx\\nyyy\\nzzz"

    compute_diff should produce:
    - 2 DELETEs (aaa, bbb)
    - 3 INSERTs (xxx, yyy, zzz)
    - DELETEs should appear before INSERTs
    - old_lineno is set for DELETEs (1,2), None for INSERTs
    - new_lineno is None for DELETEs, set for INSERTs (1,2,3)
    """


@jaunt.test()
def test_single_line_change():
    """
    old = "alpha\\nbeta\\ngamma"
    new = "alpha\\nBETA\\ngamma"

    Diff should show:
    - EQUAL "alpha" (old=1, new=1)
    - DELETE "beta" (old=2, new=None)
    - INSERT "BETA" (old=None, new=2)
    - EQUAL "gamma" (old=3, new=3)
    """


@jaunt.test()
def test_insertion_at_end():
    """
    old = "line1\\nline2"
    new = "line1\\nline2\\nline3\\nline4"

    Diff should have:
    - 2 EQUALs for line1, line2
    - 2 INSERTs for line3, line4
    """


@jaunt.test()
def test_deletion_at_beginning():
    """
    old = "header\\nalpha\\nbeta"
    new = "alpha\\nbeta"

    Diff should have:
    - 1 DELETE for "header"
    - 2 EQUALs for alpha, beta
    """


@jaunt.test()
def test_empty_to_content():
    """
    old = ""
    new = "hello\\nworld"

    Should produce DELETEs for the empty old line and INSERTs for hello, world.
    OR all INSERTs depending on LCS — the key check is that
    apply_patch(old, compute_diff(old, new)) == new.
    """


@jaunt.test()
def test_format_unified_header():
    """
    old = "aaa\\nbbb"
    new = "aaa\\nccc"

    format_unified(old, new, old_name="file1.txt", new_name="file2.txt")
    should start with:
      --- file1.txt
      +++ file2.txt
    and contain a @@ hunk header.
    """


@jaunt.test()
def test_format_unified_no_diff():
    """
    format_unified on identical texts returns an empty string.
    """


@jaunt.test()
def test_format_unified_context_lines():
    """
    Create old and new texts where a change is surrounded by 5+
    unchanged lines on each side.  With context=3 (default), the
    output hunk should show exactly 3 context lines before and 3 after
    the change — not all 5.
    """


@jaunt.test()
def test_format_unified_negative_context_raises():
    """
    format_unified(old, new, context=-1) should raise ValueError.
    """


@jaunt.test()
def test_apply_patch_roundtrip():
    """
    For several pairs (old, new):
      old = "one\\ntwo\\nthree\\nfour\\nfive"
      new = "one\\nTWO\\nthree\\nfour\\nfive\\nsix"

    Verify: apply_patch(old, compute_diff(old, new)) == new
    """


@jaunt.test()
def test_apply_patch_conflict():
    """
    Compute diff between old="aaa\\nbbb" and new="aaa\\nccc".
    Then try to apply that diff to a different base "aaa\\nxxx".
    Should raise ValueError containing "patch conflict".
    """


@jaunt.test()
def test_diff_stats_counts():
    """
    old = "a\\nb\\nc"
    new = "a\\nX\\nc\\nd"

    diff_stats should return:
    - insertions: 2 (X, d)
    - deletions: 1 (b)
    - unchanged: 2 (a, c)
    - total_old: 3
    - total_new: 4
    """


@jaunt.test()
def test_diff_stats_identical():
    """
    For identical texts "hello\\nworld":
    - insertions: 0
    - deletions: 0
    - unchanged: 2
    - total_old: 2
    - total_new: 2
    """

"""Tests for jaunt.progress.ProgressBar."""

from __future__ import annotations

import io

from jaunt.progress import ProgressBar


def test_progressbar_advance_and_finish():
    """ProgressBar tracks ok/fail counts and writes to stream."""
    stream = io.StringIO()
    pb = ProgressBar(label="build", total=3, enabled=True, stream=stream, min_interval_s=0)
    pb.advance("mod_a", ok=True)
    pb.advance("mod_b", ok=False)
    pb.advance("mod_c", ok=True)
    pb.finish()

    output = stream.getvalue()
    # Should contain the label, counts, and a newline at the end from finish()
    assert "build" in output
    assert "ok=2" in output
    assert "fail=1" in output
    assert "3/3" in output
    assert output.endswith("\n")


def test_progressbar_disabled_writes_nothing():
    """Disabled ProgressBar should not write to stream."""
    stream = io.StringIO()
    pb = ProgressBar(label="test", total=2, enabled=False, stream=stream)
    pb.advance("item", ok=True)
    pb.finish()
    assert stream.getvalue() == ""


def test_progressbar_finish_is_idempotent():
    """Calling finish() multiple times should not write extra newlines."""
    stream = io.StringIO()
    pb = ProgressBar(label="build", total=1, enabled=True, stream=stream, min_interval_s=0)
    pb.advance("a", ok=True)
    pb.finish()
    output_after_first = stream.getvalue()
    pb.finish()
    assert stream.getvalue() == output_after_first


def test_progressbar_advance_after_finish_is_noop():
    """Advancing after finish should not change output."""
    stream = io.StringIO()
    pb = ProgressBar(label="build", total=2, enabled=True, stream=stream, min_interval_s=0)
    pb.advance("a", ok=True)
    pb.finish()
    output_after_finish = stream.getvalue()
    pb.advance("b", ok=True)
    assert stream.getvalue() == output_after_finish


def test_progressbar_zero_total():
    """ProgressBar with total=0 should not crash."""
    stream = io.StringIO()
    pb = ProgressBar(label="empty", total=0, enabled=True, stream=stream, min_interval_s=0)
    pb.finish()
    output = stream.getvalue()
    assert "0/0" in output


def test_progressbar_broken_stream_disables():
    """ProgressBar should disable itself if the stream raises."""

    class BadStream:
        def write(self, s: str) -> None:
            raise OSError("broken pipe")

        def flush(self) -> None:
            raise OSError("broken pipe")

    pb = ProgressBar(label="build", total=1, enabled=True, stream=BadStream(), min_interval_s=0)
    # After __post_init__ tries to render and fails, it should be disabled
    assert not pb.enabled

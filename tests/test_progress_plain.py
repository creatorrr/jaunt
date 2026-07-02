from __future__ import annotations

import argparse
import io

import pytest

import jaunt.cli
from jaunt.progress import ProgressBar


class _Stderr(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _args(*, progress: str = "auto", no_progress: bool = False) -> argparse.Namespace:
    return argparse.Namespace(progress=progress, no_progress=no_progress)


def test_plain_progress_emits_exact_line_formats() -> None:
    stream = io.StringIO()
    bar = ProgressBar(
        label="build",
        total=3,
        stream=stream,
        mode="plain",
        min_interval_s=999,
    )

    bar.phase("pkg.alpha", "generating", "calling codex")
    bar.phase("pkg.alpha", "validating")
    bar.advance("pkg.alpha", ok=True)
    bar.advance("pkg.beta", ok=False)
    bar.finish()

    assert stream.getvalue() == (
        "[build] pkg.alpha: generating (calling codex)\n"
        "[build] pkg.alpha: validating\n"
        "[build] 1/3 ok=1 fail=0 pkg.alpha\n"
        "[build] 2/3 ok=1 fail=1 pkg.beta\n"
        "[build] done 2/3 ok=1 fail=1\n"
    )
    assert "\r" not in stream.getvalue()
    assert "\x1b" not in stream.getvalue()


def test_plain_progress_does_not_throttle_rapid_advances() -> None:
    stream = io.StringIO()
    bar = ProgressBar(
        label="test",
        total=4,
        stream=stream,
        mode="plain",
        min_interval_s=999,
    )

    for item in ("a", "b", "c", "d"):
        bar.advance(item, ok=True)
    bar.finish()

    assert stream.getvalue().splitlines() == [
        "[test] 1/4 ok=1 fail=0 a",
        "[test] 2/4 ok=2 fail=0 b",
        "[test] 3/4 ok=3 fail=0 c",
        "[test] 4/4 ok=4 fail=0 d",
        "[test] done 4/4 ok=4 fail=0",
    ]


def test_plain_progress_disables_on_first_write_failure() -> None:
    class BrokenStream:
        def write(self, s: str) -> None:
            raise OSError("broken pipe")

        def flush(self) -> None:
            raise AssertionError("flush should not run after write failure")

    bar = ProgressBar(label="build", total=1, stream=BrokenStream(), mode="plain")

    bar.phase("pkg.alpha", "generating")

    assert bar.enabled is False


@pytest.mark.parametrize(
    ("tty", "requested", "json_mode", "expected_mode"),
    [
        (True, "auto", False, "rich"),
        (False, "auto", False, "plain"),
        (True, "rich", False, "rich"),
        (False, "rich", False, "rich"),
        (True, "plain", False, "plain"),
        (False, "plain", False, "plain"),
        (True, "plain", True, "plain"),
        (True, "rich", True, "rich"),
    ],
)
def test_make_progress_resolves_modes(
    monkeypatch: pytest.MonkeyPatch,
    tty: bool,
    requested: str,
    json_mode: bool,
    expected_mode: str,
) -> None:
    monkeypatch.setattr(jaunt.cli.sys, "stderr", _Stderr(tty=tty))

    progress = jaunt.cli._make_progress(
        _args(progress=requested),
        label="build",
        total=1,
        json_mode=json_mode,
    )

    assert progress is not None
    assert progress.mode == expected_mode


@pytest.mark.parametrize(
    ("requested", "json_mode", "no_progress", "total"),
    [
        ("auto", True, False, 1),
        ("none", False, False, 1),
        ("plain", False, True, 1),
        ("plain", False, False, 0),
    ],
)
def test_make_progress_resolves_to_none(
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    json_mode: bool,
    no_progress: bool,
    total: int,
) -> None:
    monkeypatch.setattr(jaunt.cli.sys, "stderr", _Stderr(tty=True))

    progress = jaunt.cli._make_progress(
        _args(progress=requested, no_progress=no_progress),
        label="build",
        total=total,
        json_mode=json_mode,
    )

    assert progress is None

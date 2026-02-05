from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from shutil import get_terminal_size


@dataclass(slots=True)
class ProgressBar:
    label: str
    total: int
    enabled: bool = True
    stream: object = sys.stderr
    width: int = 28
    min_interval_s: float = 0.08

    def __post_init__(self) -> None:
        self._done = 0
        self._ok = 0
        self._fail = 0
        self._last_render = 0.0
        self._finished = False
        self._render("")  # initial line

    def advance(self, item: str, *, ok: bool) -> None:
        if self._finished:
            return
        self._done += 1
        if ok:
            self._ok += 1
        else:
            self._fail += 1
        self._render(item)

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._render("done", force=True)
        self._write("\n")

    def _write(self, s: str) -> None:
        if not self.enabled:
            return
        try:
            self.stream.write(s)  # type: ignore[attr-defined]
            self.stream.flush()  # type: ignore[attr-defined]
        except Exception:
            # Progress is best-effort; never fail the CLI because of rendering.
            self.enabled = False

    def _render(self, item: str, *, force: bool = False) -> None:
        if not self.enabled:
            return

        now = time.time()
        if (not force) and (now - self._last_render) < float(self.min_interval_s):
            return
        self._last_render = now

        total = max(0, int(self.total))
        done = min(max(0, int(self._done)), total) if total else int(self._done)
        frac = (done / total) if total else 1.0

        fill = int(round(self.width * frac))
        fill = min(max(0, fill), self.width)
        bar = "#" * fill + "-" * (self.width - fill)

        cols = get_terminal_size(fallback=(80, 20)).columns
        msg = f"{self.label} [{bar}] {done}/{total} ok={self._ok} fail={self._fail}"
        if item:
            msg += f"  {item}"
        msg = msg[: max(0, cols - 1)]

        self._write("\r" + msg)


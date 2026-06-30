"""colgrep (LightOn next-plaid) wrapper. Every failure degrades to no hits."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Hit:
    file: str
    snippet: str
    score: float


def available() -> bool:
    return shutil.which("colgrep") is not None


def ensure_index(root: Path) -> bool:
    if not available():
        return False
    try:
        subprocess.run(
            ["colgrep", "init", str(root)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def query(text: str, *, root: Path, max_hits: int = 8, timeout: float = 5.0) -> list[Hit]:
    if not available() or not text.strip():
        return []
    try:
        cp = subprocess.run(
            ["colgrep", "--json", "--k", str(max_hits), text],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if cp.returncode != 0 or not cp.stdout.strip():
        return []
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return []
    rows = data if isinstance(data, list) else data.get("results", [])
    hits: list[Hit] = []
    for row in rows:
        unit = row.get("unit", row) if isinstance(row, dict) else {}
        file = str(unit.get("file", "")) if isinstance(unit, dict) else ""
        snippet = str(unit.get("snippet", "")) if isinstance(unit, dict) else ""
        score = float(row.get("score", 0.0)) if isinstance(row, dict) else 0.0
        if file:
            hits.append(Hit(file=file, snippet=snippet, score=score))
    # Deterministic ordering: score desc, then file asc.
    hits.sort(key=lambda h: (-h.score, h.file))
    return hits[:max_hits]


def render_relevant_block(hits: list[Hit]) -> str:
    if not hits:
        return ""
    return "Read `_context/relevant_*.py` for related existing code in the repository."

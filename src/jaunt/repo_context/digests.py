"""Source-content digests and the gitignored treedocs sidecar cache.

The treedocs.yaml `signature` only covers descriptions (manual-edit drift).
Detecting "the file changed, its description is now stale" needs a per-file
source-content digest, kept here in .jaunt/tree-cache.json.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


def source_digest(path: Path) -> str:
    """SHA-256 over a file's raw bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheRecord:
    source_digest: str
    description: str
    enriched: bool


class TreeCache:
    """Path -> CacheRecord sidecar persisted as JSON under .jaunt/."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: dict[str, CacheRecord] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
            for rel, rec in (raw.get("entries") or {}).items():
                try:
                    self._records[rel] = CacheRecord(
                        source_digest=str(rec["source_digest"]),
                        description=str(rec["description"]),
                        enriched=bool(rec.get("enriched", False)),
                    )
                except (KeyError, TypeError):
                    continue

    def get(self, relpath: str) -> CacheRecord | None:
        return self._records.get(relpath)

    def set(self, relpath: str, *, source_digest: str, description: str, enriched: bool) -> None:
        self._records[relpath] = CacheRecord(source_digest, description, enriched)

    def prune(self, keep: builtins.set[str]) -> None:
        for rel in list(self._records):
            if rel not in keep:
                del self._records[rel]

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": {
                rel: {
                    "source_digest": r.source_digest,
                    "description": r.description,
                    "enriched": r.enriched,
                }
                for rel, r in sorted(self._records.items())
            }
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)

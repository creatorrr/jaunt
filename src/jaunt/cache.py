"""LLM response cache.

Caches raw LLM responses on disk keyed by a SHA-256 hash of the generation
context (spec sources, dependency context, model, provider). Cache entries are
stored as JSON files in ``.jaunt/cache/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from jaunt.generate.base import ModuleSpecContext

logger = logging.getLogger("jaunt.cache")


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A single cached LLM response."""

    source: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    provider: str
    cached_at: float


def cache_key_from_context(
    ctx: ModuleSpecContext,
    *,
    model: str,
    provider: str,
) -> str:
    """Compute deterministic SHA-256 cache key from generation context."""
    h = hashlib.sha256()
    h.update(provider.encode())
    h.update(b"\x00")
    h.update(model.encode())
    h.update(b"\x00")
    h.update(ctx.kind.encode())
    h.update(b"\x00")
    h.update(ctx.spec_module.encode())
    h.update(b"\x00")
    h.update(ctx.generated_module.encode())
    h.update(b"\x00")
    h.update(json.dumps(ctx.expected_names, sort_keys=True).encode())
    h.update(b"\x00")
    h.update(
        json.dumps(
            {str(k): v for k, v in sorted(ctx.spec_sources.items(), key=lambda x: str(x[0]))},
            sort_keys=True,
        ).encode()
    )
    h.update(b"\x00")
    h.update(
        json.dumps(
            {str(k): v for k, v in sorted(ctx.decorator_prompts.items(), key=lambda x: str(x[0]))},
            sort_keys=True,
        ).encode()
    )
    h.update(b"\x00")
    h.update(
        json.dumps(
            {str(k): v for k, v in sorted(ctx.dependency_apis.items(), key=lambda x: str(x[0]))},
            sort_keys=True,
        ).encode()
    )
    h.update(b"\x00")
    h.update(
        json.dumps(dict(sorted(ctx.dependency_generated_modules.items())), sort_keys=True).encode()
    )
    h.update(b"\x00")
    h.update((ctx.skills_block or "").encode())
    return h.hexdigest()


class ResponseCache:
    """File-backed LLM response cache with in-process memo."""

    def __init__(self, cache_dir: Path, *, enabled: bool = True) -> None:
        self._cache_dir = cache_dir
        self._enabled = enabled
        self._memo: dict[str, CacheEntry] = {}
        self._hits = 0
        self._misses = 0

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def _entry_path(self, key: str) -> Path:
        return self._cache_dir / key[:2] / f"{key}.json"

    def get(self, key: str) -> CacheEntry | None:
        if not self._enabled:
            self._misses += 1
            return None

        if key in self._memo:
            self._hits += 1
            return self._memo[key]

        path = self._entry_path(key)
        if not path.exists():
            self._misses += 1
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = CacheEntry(
                source=data["source"],
                prompt_tokens=data.get("prompt_tokens", 0),
                completion_tokens=data.get("completion_tokens", 0),
                model=data.get("model", ""),
                provider=data.get("provider", ""),
                cached_at=data.get("cached_at", 0.0),
            )
            self._memo[key] = entry
            self._hits += 1
            return entry
        except Exception:
            logger.debug("Cache read failed for key %s", key[:12])
            self._misses += 1
            return None

    def put(self, key: str, entry: CacheEntry) -> None:
        if not self._enabled:
            return
        self._memo[key] = entry
        path = self._entry_path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "source": entry.source,
                "prompt_tokens": entry.prompt_tokens,
                "completion_tokens": entry.completion_tokens,
                "model": entry.model,
                "provider": entry.provider,
                "cached_at": entry.cached_at,
            }
            raw = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
            path.write_text(raw, encoding="utf-8")
        except Exception:
            logger.debug("Cache write failed for key %s", key[:12])

    def info(self) -> dict[str, object]:
        """Return cache statistics."""
        if not self._cache_dir.exists():
            return {"entries": 0, "size_bytes": 0, "path": str(self._cache_dir)}

        count = 0
        total_size = 0
        for p in self._cache_dir.rglob("*.json"):
            count += 1
            try:
                total_size += p.stat().st_size
            except OSError:
                pass
        return {"entries": count, "size_bytes": total_size, "path": str(self._cache_dir)}

    def clear_all(self) -> int:
        """Remove all cache entries. Returns count of removed entries."""
        import shutil

        self._memo.clear()
        if not self._cache_dir.exists():
            return 0
        count = sum(1 for _ in self._cache_dir.rglob("*.json"))
        shutil.rmtree(self._cache_dir, ignore_errors=True)
        return count

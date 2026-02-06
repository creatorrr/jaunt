"""Persistent AST parse cache.

Caches parsed AST trees on disk to avoid re-parsing unchanged source files
across CLI invocations.  Cache key is ``(path, mtime_ns, size)``.
"""

from __future__ import annotations

import ast
import hashlib
import os
import pickle
import sys
from pathlib import Path


class ParseCache:
    """File-backed cache for ``(source, ast.Module)`` tuples.

    Entries are stored as pickle files in *cache_dir*.  A cache hit requires
    that the file's ``st_mtime_ns`` and ``st_size`` match what was recorded at
    cache-write time **and** that the Python version matches (pickled ASTs are
    not portable across Python versions).
    """

    _PY_TAG = f"py{sys.version_info.major}{sys.version_info.minor}"

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        # In-process memo so a single CLI run never reads the same pickle twice.
        self._memo: dict[str, tuple[str, ast.Module]] = {}

    def _cache_path(self, file_path: str) -> Path:
        key = hashlib.sha256(file_path.encode()).hexdigest()
        return self._cache_dir / f"{key}_{self._PY_TAG}.pickle"

    def parse(self, file_path: str) -> tuple[str, ast.Module] | None:
        """Return ``(source, tree)`` for *file_path*, using cache when valid."""

        # Fast path: in-process memo.
        if file_path in self._memo:
            # Verify file hasn't changed since we memoised.
            try:
                st = os.stat(file_path)
            except OSError:
                return None
            cached_source, cached_tree = self._memo[file_path]
            # If source length still matches (cheap check), return memo.
            if len(cached_source.encode("utf-8")) == st.st_size:
                return cached_source, cached_tree

        try:
            st = os.stat(file_path)
        except OSError:
            return None

        mtime_ns = st.st_mtime_ns
        size = st.st_size

        # Try disk cache.
        cache_path = self._cache_path(file_path)
        if cache_path.exists():
            try:
                data = pickle.loads(cache_path.read_bytes())  # noqa: S301
                if data.get("mtime_ns") == mtime_ns and data.get("size") == size:
                    source = data["source"]
                    tree = data["tree"]
                    self._memo[file_path] = (source, tree)
                    return source, tree
            except Exception:
                pass

        # Cache miss: parse from disk.
        try:
            source = Path(file_path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=file_path)
        except Exception:
            return None

        # Write to disk cache.
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "mtime_ns": mtime_ns,
                "size": size,
                "source": source,
                "tree": tree,
            }
            cache_path.write_bytes(pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))
        except Exception:
            pass

        self._memo[file_path] = (source, tree)
        return source, tree

    def clear(self) -> None:
        """Remove all cache entries."""
        self._memo.clear()
        if self._cache_dir.exists():
            for p in self._cache_dir.glob("*.pickle"):
                try:
                    p.unlink()
                except OSError:
                    pass

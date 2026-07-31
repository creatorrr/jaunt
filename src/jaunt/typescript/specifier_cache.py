"""Persistent content-addressed cache for runtime module specifiers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def specifier_cache_key(
    content: bytes,
    *,
    suffix: str,
    tolerate_unsupported_loader_flows: bool,
) -> str:
    """Return a tokenizer-cache key for exact source bytes and parser policy."""

    digest = hashlib.sha256(content).hexdigest()
    tolerance = "tolerant" if tolerate_unsupported_loader_flows else "strict"
    return f"{digest}:{suffix.casefold()}:{tolerance}"


class SpecifierCache:
    """Best-effort persistent cache of raw runtime module specifiers."""

    def __init__(self, path: Path | None, *, version: str) -> None:
        self._path = path
        self._version = version
        self._entries: dict[str, tuple[str, ...]] = {}
        self._loaded = False
        self._dirty = False
        self._hits = 0
        self._misses = 0

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path is None:
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        try:
            version = payload["version"]
            raw_entries = payload["entries"]
        except (KeyError, TypeError, ValueError):
            return
        if not isinstance(version, str) or version != self._version:
            return
        if not isinstance(raw_entries, dict):
            return
        for key, specifiers in raw_entries.items():
            try:
                if not isinstance(key, str):
                    raise TypeError
                if not isinstance(specifiers, list) or not all(
                    isinstance(specifier, str) for specifier in specifiers
                ):
                    raise ValueError
                self._entries[key] = tuple(specifiers)
            except (KeyError, TypeError, ValueError):
                continue

    def get(self, key: str) -> tuple[str, ...] | None:
        if self._path is None:
            self._misses += 1
            return None
        self._load()
        value = self._entries.get(key)
        if value is None:
            self._misses += 1
            return None
        self._hits += 1
        return value

    def put(self, key: str, specifiers: tuple[str, ...]) -> None:
        if self._path is None:
            return
        self._load()
        if self._entries.get(key) == specifiers:
            return
        self._entries[key] = specifiers
        self._dirty = True

    def save(self) -> None:
        if self._path is None or not self._dirty:
            return
        content = (
            json.dumps(
                {
                    "version": self._version,
                    "entries": {key: list(value) for key, value in self._entries.items()},
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        temporary: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                text=True,
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            _fsync_directory(self._path.parent)
            self._dirty = False
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry replacements where the host supports it."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)

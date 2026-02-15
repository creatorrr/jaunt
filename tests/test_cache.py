"""Tests for jaunt.cache module."""

from __future__ import annotations

from pathlib import Path

from jaunt.cache import CacheEntry, ResponseCache, cache_key_from_context
from jaunt.generate.base import ModuleSpecContext


def _make_ctx(**overrides: object) -> ModuleSpecContext:    return ModuleSpecContext(
        kind=overrides.get("kind", "build"),  # type: ignore[arg-type]
        spec_module=overrides.get("spec_module", "pkg.specs"),  # type: ignore[arg-type]
        generated_module=overrides.get("generated_module", "pkg.__generated__.specs"),  # type: ignore[arg-type]
        expected_names=overrides.get("expected_names", ["foo"]),  # type: ignore[arg-type]
        spec_sources=overrides.get("spec_sources", {}),  # type: ignore[arg-type]
        decorator_prompts=overrides.get("decorator_prompts", {}),  # type: ignore[arg-type]
        dependency_apis=overrides.get("dependency_apis", {}),  # type: ignore[arg-type]
        dependency_generated_modules=overrides.get("dependency_generated_modules", {}),  # type: ignore[arg-type]
    )


def _make_entry(**overrides: object) -> CacheEntry:    return CacheEntry(
        source=overrides.get("source", "def foo(): pass\n"),  # type: ignore[arg-type]
        prompt_tokens=overrides.get("prompt_tokens", 100),  # type: ignore[arg-type]
        completion_tokens=overrides.get("completion_tokens", 50),  # type: ignore[arg-type]
        model=overrides.get("model", "gpt-test"),  # type: ignore[arg-type]
        provider=overrides.get("provider", "openai"),  # type: ignore[arg-type]
        cached_at=overrides.get("cached_at", 1000.0),  # type: ignore[arg-type]
    )


def test_cache_miss_returns_none(tmp_path: Path) -> None:
    rc = ResponseCache(tmp_path / "cache")
    assert rc.get("nonexistent") is None
    assert rc.misses == 1
    assert rc.hits == 0


def test_cache_put_then_get(tmp_path: Path) -> None:
    rc = ResponseCache(tmp_path / "cache")
    entry = _make_entry()
    rc.put("abc123", entry)
    result = rc.get("abc123")
    assert result is not None
    assert result.source == "def foo(): pass\n"
    assert result.prompt_tokens == 100
    assert rc.hits == 1


def test_cache_disabled_always_misses(tmp_path: Path) -> None:
    rc = ResponseCache(tmp_path / "cache", enabled=False)
    entry = _make_entry()
    rc.put("abc123", entry)
    assert rc.get("abc123") is None
    assert rc.misses == 1


def test_cache_get_from_disk(tmp_path: Path) -> None:
    """A second cache instance can read entries written by the first."""
    cache_dir = tmp_path / "cache"
    rc1 = ResponseCache(cache_dir)
    rc1.put("abc123", _make_entry())

    rc2 = ResponseCache(cache_dir)
    result = rc2.get("abc123")
    assert result is not None
    assert result.source == "def foo(): pass\n"


def test_cache_info_empty(tmp_path: Path) -> None:
    rc = ResponseCache(tmp_path / "cache")
    info = rc.info()
    assert info["entries"] == 0
    assert info["size_bytes"] == 0


def test_cache_info_with_entries(tmp_path: Path) -> None:
    rc = ResponseCache(tmp_path / "cache")
    rc.put("aaa111", _make_entry())
    rc.put("bbb222", _make_entry(source="def bar(): pass\n"))
    info = rc.info()
    assert info["entries"] == 2
    assert int(info["size_bytes"]) > 0  # type: ignore[arg-type]


def test_cache_clear(tmp_path: Path) -> None:
    rc = ResponseCache(tmp_path / "cache")
    rc.put("aaa111", _make_entry())
    rc.put("bbb222", _make_entry())
    count = rc.clear_all()
    assert count == 2
    assert rc.get("aaa111") is None


def test_cache_clear_empty(tmp_path: Path) -> None:
    rc = ResponseCache(tmp_path / "cache")
    assert rc.clear_all() == 0


def test_cache_key_deterministic() -> None:
    ctx = _make_ctx()
    k1 = cache_key_from_context(ctx, model="gpt-test", provider="openai")
    k2 = cache_key_from_context(ctx, model="gpt-test", provider="openai")
    assert k1 == k2


def test_cache_key_differs_by_model() -> None:
    ctx = _make_ctx()
    k1 = cache_key_from_context(ctx, model="gpt-a", provider="openai")
    k2 = cache_key_from_context(ctx, model="gpt-b", provider="openai")
    assert k1 != k2


def test_cache_key_differs_by_provider() -> None:
    ctx = _make_ctx()
    k1 = cache_key_from_context(ctx, model="model", provider="openai")
    k2 = cache_key_from_context(ctx, model="model", provider="anthropic")
    assert k1 != k2


def test_cache_key_differs_by_context() -> None:
    ctx1 = _make_ctx(spec_module="pkg.a")
    ctx2 = _make_ctx(spec_module="pkg.b")
    k1 = cache_key_from_context(ctx1, model="m", provider="p")
    k2 = cache_key_from_context(ctx2, model="m", provider="p")
    assert k1 != k2


def test_cache_corrupt_file_returns_none(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    rc = ResponseCache(cache_dir)
    # Write a corrupt file at the expected path.
    key = "deadbeef" + "0" * 56
    path = cache_dir / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json", encoding="utf-8")
    assert rc.get(key) is None
    assert rc.misses == 1

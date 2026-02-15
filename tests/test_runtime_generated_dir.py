"""Tests for runtime generated_dir env-var support."""

from __future__ import annotations

from jaunt.runtime import _get_generated_dir


def test_get_generated_dir_default() -> None:
    """Without env var, falls back to __generated__."""
    import os

    os.environ.pop("JAUNT_GENERATED_DIR", None)
    assert _get_generated_dir() == "__generated__"


def test_get_generated_dir_from_env(monkeypatch) -> None:
    """Respects JAUNT_GENERATED_DIR env var."""
    monkeypatch.setenv("JAUNT_GENERATED_DIR", "custom_gen")
    assert _get_generated_dir() == "custom_gen"

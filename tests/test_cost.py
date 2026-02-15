"""Tests for jaunt.cost module."""

from __future__ import annotations

import pytest

from jaunt.cost import CostTracker, _estimate_cost
from jaunt.errors import JauntGenerationError
from jaunt.generate.base import TokenUsage


def test_empty_tracker() -> None:
    ct = CostTracker()
    assert ct.api_calls == 0
    assert ct.cache_hits == 0
    assert ct.total_tokens == 0
    assert ct.estimated_cost == 0.0


def test_record_accumulates() -> None:
    ct = CostTracker()
    ct.record("mod_a", TokenUsage(100, 50, "gpt-5", "openai"))
    ct.record("mod_b", TokenUsage(200, 100, "gpt-5", "openai"))
    assert ct.api_calls == 2
    assert ct.total_prompt_tokens == 300
    assert ct.total_completion_tokens == 150
    assert ct.total_tokens == 450


def test_cache_hits_tracked() -> None:
    ct = CostTracker()
    ct.record_cache_hit()
    ct.record_cache_hit()
    assert ct.cache_hits == 2


def test_estimated_cost_known_model() -> None:
    cost = _estimate_cost("gpt-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(10.0)  # 2.00 + 8.00


def test_estimated_cost_unknown_model() -> None:
    assert _estimate_cost("unknown-model-xyz", 1000, 1000) == 0.0


def test_check_budget_passes() -> None:
    ct = CostTracker(max_cost=10.0)
    ct.record("mod_a", TokenUsage(100, 50, "gpt-5", "openai"))
    ct.check_budget()  # Should not raise.


def test_check_budget_raises() -> None:
    ct = CostTracker(max_cost=0.0001)
    ct.record("mod_a", TokenUsage(1_000_000, 1_000_000, "gpt-5", "openai"))
    with pytest.raises(JauntGenerationError, match="exceeds budget"):
        ct.check_budget()


def test_check_budget_none_unlimited() -> None:
    ct = CostTracker(max_cost=None)
    ct.record("mod_a", TokenUsage(1_000_000, 1_000_000, "gpt-5", "openai"))
    ct.check_budget()  # Should not raise.


def test_summary_dict_keys() -> None:
    ct = CostTracker()
    ct.record("mod", TokenUsage(100, 50, "gpt-5", "openai"))
    d = ct.summary_dict()
    assert set(d.keys()) == {
        "api_calls",
        "cache_hits",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "estimated_cost_usd",
    }


def test_format_summary_output() -> None:
    ct = CostTracker(max_cost=1.0)
    ct.record("mod", TokenUsage(100, 50, "gpt-5", "openai"))
    ct.record_cache_hit()
    text = ct.format_summary()
    assert "1 API call(s)" in text
    assert "1 cache hit(s)" in text
    assert "Budget limit" in text


def test_longest_prefix_match() -> None:
    """gpt-4.1-mini should match the mini rate, not the gpt-4.1 rate."""
    cost_mini = _estimate_cost("gpt-4.1-mini", 1_000_000, 1_000_000)
    cost_base = _estimate_cost("gpt-4.1", 1_000_000, 1_000_000)
    # mini should be cheaper
    assert cost_mini < cost_base

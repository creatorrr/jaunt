"""Token usage tracking and cost estimation."""

from __future__ import annotations

from dataclasses import dataclass, field

from jaunt.generate.base import TokenUsage

# Estimated cost per 1M tokens (input, output) by model prefix.
_COST_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-5": (2.00, 8.00),
    "o3": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "claude-sonnet": (3.00, 15.00),
    "claude-opus": (15.00, 75.00),
    "claude-haiku": (0.25, 1.25),
    "llama-4": (0.60, 0.60),
    "llama3.3-70b": (0.60, 0.60),
    "llama3.1-8b": (0.10, 0.10),
}


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Return estimated cost in USD. Returns 0.0 for unknown models."""
    # Try longest prefix match first so "gpt-4.1-mini" beats "gpt-4.1".
    prefixes: list[str] = list(_COST_TABLE.keys())
    prefixes.sort(key=len, reverse=True)
    for prefix in prefixes:
        if model.startswith(prefix):
            inp_rate, out_rate = _COST_TABLE[prefix]
            return (prompt_tokens * inp_rate + completion_tokens * out_rate) / 1_000_000
    return 0.0


@dataclass
class CostTracker:
    """Accumulates token usage across a build/test run."""

    max_cost: float | None = None
    _records: list[tuple[str, TokenUsage]] = field(default_factory=list)
    _cache_hits: int = 0

    def record(self, module_name: str, usage: TokenUsage) -> None:
        self._records.append((module_name, usage))

    def record_cache_hit(self) -> None:
        self._cache_hits += 1

    @property
    def total_prompt_tokens(self) -> int:
        return sum(u.prompt_tokens for _, u in self._records)

    @property
    def total_completion_tokens(self) -> int:
        return sum(u.completion_tokens for _, u in self._records)

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens

    @property
    def estimated_cost(self) -> float:
        return sum(
            _estimate_cost(u.model, u.prompt_tokens, u.completion_tokens) for _, u in self._records
        )

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    @property
    def api_calls(self) -> int:
        return len(self._records)

    def check_budget(self) -> None:
        """Raise JauntGenerationError if estimated cost exceeds max_cost."""
        if self.max_cost is not None and self.estimated_cost > self.max_cost:
            from jaunt.errors import JauntGenerationError

            raise JauntGenerationError(
                f"Build cost ${self.estimated_cost:.4f} exceeds budget "
                f"limit ${self.max_cost:.4f}. Aborting."
            )

    def summary_dict(self) -> dict[str, object]:
        """Return a JSON-serializable cost summary."""
        return {
            "api_calls": self.api_calls,
            "cache_hits": self.cache_hits,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost, 6),
        }

    def format_summary(self) -> str:
        """Return a human-readable cost summary for stderr."""
        lines = [
            f"Cost: {self.api_calls} API call(s), {self.cache_hits} cache hit(s)",
            f"  Tokens: {self.total_prompt_tokens:,} prompt"
            f" + {self.total_completion_tokens:,} completion"
            f" = {self.total_tokens:,} total",
            f"  Estimated cost: ${self.estimated_cost:.4f}",
        ]
        if self.max_cost is not None:
            lines.append(f"  Budget limit: ${self.max_cost:.4f}")
        return "\n".join(lines)

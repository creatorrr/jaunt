"""Tests for OpenAI backend retry logic."""

from __future__ import annotations

import asyncio

import pytest

from jaunt.generate.openai_backend import _is_retryable


class _FakeRateLimitError(Exception):
    pass


_FakeRateLimitError.__name__ = "RateLimitError"


class _FakeAPITimeoutError(Exception):
    pass


_FakeAPITimeoutError.__name__ = "APITimeoutError"


class _FakeAPIConnectionError(Exception):
    pass


_FakeAPIConnectionError.__name__ = "APIConnectionError"


class _FakeAPIStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


_FakeAPIStatusError.__name__ = "APIStatusError"


def test_is_retryable_rate_limit() -> None:
    assert _is_retryable(_FakeRateLimitError()) is True


def test_is_retryable_timeout() -> None:
    assert _is_retryable(_FakeAPITimeoutError()) is True


def test_is_retryable_connection() -> None:
    assert _is_retryable(_FakeAPIConnectionError()) is True


def test_is_retryable_500() -> None:
    assert _is_retryable(_FakeAPIStatusError(500)) is True
    assert _is_retryable(_FakeAPIStatusError(502)) is True
    assert _is_retryable(_FakeAPIStatusError(503)) is True


def test_is_retryable_400_not_retryable() -> None:
    assert _is_retryable(_FakeAPIStatusError(400)) is False
    assert _is_retryable(_FakeAPIStatusError(401)) is False


def test_is_retryable_generic_errors() -> None:
    assert _is_retryable(TimeoutError()) is True
    assert _is_retryable(ConnectionError()) is True
    assert _is_retryable(ValueError("nope")) is False
    assert _is_retryable(RuntimeError("nope")) is False


def test_call_openai_retries_on_retryable_error(monkeypatch) -> None:
    from jaunt.config import LLMConfig
    from jaunt.generate.openai_backend import OpenAIBackend

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )

    calls = []

    class _FakeResp:
        class _Choice:
            class _Message:
                content = "def foo(): pass"

            message = _Message()

        choices = [_Choice()]

    class _FakeClient:
        class completions:
            @staticmethod
            async def create(**kwargs):
                calls.append(kwargs)
                if len(calls) < 3:
                    raise _FakeRateLimitError("rate limited")
                return _FakeResp()

    # Patch internal state to use our fake client and reduce backoff.
    monkeypatch.setattr(backend, "_client", type("C", (), {"chat": _FakeClient})())
    import jaunt.generate.openai_backend as mod

    monkeypatch.setattr(mod, "_BASE_BACKOFF_S", 0.001)

    source, usage = asyncio.run(backend._call_openai([{"role": "user", "content": "hi"}]))
    assert source == "def foo(): pass"
    assert len(calls) == 3


def test_call_openai_raises_non_retryable_immediately(monkeypatch) -> None:
    from jaunt.config import LLMConfig
    from jaunt.generate.openai_backend import OpenAIBackend

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )

    calls = []

    class _FakeClient:
        class completions:
            @staticmethod
            async def create(**kwargs):
                calls.append(kwargs)
                raise ValueError("bad input")

    monkeypatch.setattr(backend, "_client", type("C", (), {"chat": _FakeClient})())

    with pytest.raises(ValueError, match="bad input"):
        asyncio.run(backend._call_openai([{"role": "user", "content": "hi"}]))

    # Non-retryable: should have called exactly once.
    assert len(calls) == 1

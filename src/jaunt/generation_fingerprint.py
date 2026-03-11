"""Compatibility wrapper for generation fingerprint helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from jaunt.config import JauntConfig
from jaunt.generate.fingerprint import generation_fingerprint_from_config


def generation_fingerprint(
    cfg: JauntConfig,
    *,
    kind: Literal["build", "test"],
    build_instructions: Sequence[str] | None = None,
    include_target_tests: bool | None = None,
) -> str:
    return generation_fingerprint_from_config(
        cfg,
        kind=kind,
        build_instructions=build_instructions,
        include_target_tests=include_target_tests,
    )

"""Compatibility wrapper for generation fingerprint helpers."""

from __future__ import annotations

from typing import Literal

from jaunt.config import JauntConfig
from jaunt.generate.fingerprint import generation_fingerprint_from_config


def generation_fingerprint(
    cfg: JauntConfig,
    *,
    kind: Literal["build", "test"],
) -> str:
    return generation_fingerprint_from_config(cfg, kind=kind)

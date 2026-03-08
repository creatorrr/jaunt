"""Stable generation fingerprints for artifact freshness and cache partitioning."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from jaunt.config import JauntConfig
from jaunt.generate.aider_contract import aider_generation_fingerprint_parts
from jaunt.generate.shared import load_prompt


def build_generation_fingerprint(
    *,
    engine: str,
    kind: Literal["build", "test"],
    mode: str = "",
    prompt_parts: list[str],
    editor_model: str = "",
    reasoning_effort: str = "",
    runtime_parts: list[str] | None = None,
) -> str:
    payload = {
        "engine": engine,
        "kind": kind,
        "mode": mode,
        "prompt_parts": prompt_parts,
    }
    if runtime_parts:
        payload["runtime_parts"] = runtime_parts
    if mode == "architect" and editor_model.strip():
        payload["editor_model"] = editor_model.strip()
    if reasoning_effort.strip():
        payload["reasoning_effort"] = reasoning_effort.strip()
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def generation_fingerprint_from_config(
    cfg: JauntConfig,
    *,
    kind: Literal["build", "test"],
) -> str:
    if kind == "build":
        system_prompt = load_prompt("build_system.md", cfg.prompts.build_system or None)
        user_prompt = load_prompt("build_module.md", cfg.prompts.build_module or None)
        mode = cfg.aider.build_mode if cfg.agent.engine == "aider" else ""
    else:
        system_prompt = load_prompt("test_system.md", cfg.prompts.test_system or None)
        user_prompt = load_prompt("test_module.md", cfg.prompts.test_module or None)
        mode = cfg.aider.test_mode if cfg.agent.engine == "aider" else ""
    editor_model = cfg.aider.editor_model if cfg.agent.engine == "aider" else ""
    reasoning_effort = cfg.llm.reasoning_effort if cfg.agent.engine == "aider" else ""
    runtime_parts = (
        aider_generation_fingerprint_parts(kind)
        if cfg.agent.engine == "aider"
        else []
    )

    return build_generation_fingerprint(
        engine=cfg.agent.engine,
        kind=kind,
        mode=mode,
        prompt_parts=[system_prompt, user_prompt],
        editor_model=editor_model,
        reasoning_effort=reasoning_effort or "",
        runtime_parts=runtime_parts,
    )

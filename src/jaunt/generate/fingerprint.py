"""Stable generation fingerprints for artifact freshness and cache partitioning."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Literal

from jaunt.config import JauntConfig
from jaunt.generate.shared import load_prompt

_CODEX_VERSION_UNKNOWN = "unknown"


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


@lru_cache(maxsize=1)
def resolve_codex_cli_version() -> str:
    try:
        proc = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except Exception:
        return _CODEX_VERSION_UNKNOWN

    text = (proc.stdout or proc.stderr or "").strip()
    if not text:
        return _CODEX_VERSION_UNKNOWN
    first_line = text.splitlines()[0].strip()
    return " ".join(first_line.split()) or _CODEX_VERSION_UNKNOWN


def _prompt_digest_part(name: str, content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"{name}:sha256:{digest}"


def _prompt_specs(
    cfg: JauntConfig, *, kind: Literal["build", "test"]
) -> list[tuple[str, str | None]]:
    if kind == "build":
        # The Jaunt preamble (codex_preamble.md) opens every build prompt, so a change to
        # it must invalidate already-built modules just like build_system/build_module.
        return [
            ("codex_preamble.md", cfg.prompts.build_preamble or None),
            ("build_system.md", cfg.prompts.build_system or None),
            ("build_module.md", cfg.prompts.build_module or None),
        ]
    return [
        ("test_system.md", cfg.prompts.test_system or None),
        ("test_module.md", cfg.prompts.test_module or None),
    ]


def generation_fingerprint_from_config(
    cfg: JauntConfig,
    *,
    kind: Literal["build", "test"],
    build_instructions: Sequence[str] | None = None,
    include_target_tests: bool | None = None,
    codex_version_resolver: Callable[[], str] | None = None,
) -> str:
    specs = _prompt_specs(cfg, kind=kind)
    prompt_parts = [load_prompt(name, override) for name, override in specs]
    mode = ""
    if cfg.agent.engine == "codex":
        prompt_parts = [
            _prompt_digest_part(name, content)
            for (name, _override), content in zip(specs, prompt_parts, strict=True)
        ]
    editor_model = ""
    reasoning_effort = cfg.codex.reasoning_effort if cfg.agent.engine == "codex" else ""
    runtime_parts = (
        [f"codex_model={cfg.codex.model}", f"codex_sandbox={cfg.codex.sandbox}"]
        if cfg.agent.engine == "codex"
        else []
    )
    if cfg.agent.engine == "codex" and cfg.codex.fingerprint_cli_version:
        resolver = codex_version_resolver or resolve_codex_cli_version
        version = (resolver() or _CODEX_VERSION_UNKNOWN).strip() or _CODEX_VERSION_UNKNOWN
        runtime_parts.append(f"codex_cli_version={version}")
    build_runtime_parts = list(runtime_parts)
    if kind == "build":
        instruction_source = (
            list(build_instructions) if build_instructions is not None else cfg.build.instructions
        )
        effective_instructions = [item.strip() for item in instruction_source if item.strip()]
        effective_include_target_tests = (
            bool(cfg.build.include_target_tests)
            if include_target_tests is None
            else bool(include_target_tests)
        )
        build_runtime_parts.extend(
            [
                f"include_target_tests={effective_include_target_tests}",
                "build_instructions=" + json.dumps(effective_instructions, ensure_ascii=True),
            ]
        )
        # Enabling the model-written project overview changes what every build prompt
        # contains, so flipping it on (or off) must invalidate already-built modules.
        # Keyed on the config flag rather than the generated prose so that (a) `jaunt
        # build` and `jaunt test` compute identical fingerprints and (b) editing docs
        # alone does not force a whole-project rebuild. Only contributes when enabled,
        # so projects that never use the overview are unaffected.
        if cfg.context.overview:
            build_runtime_parts.append("project_overview_enabled=True")

    return build_generation_fingerprint(
        engine=cfg.agent.engine,
        kind=kind,
        mode=mode,
        prompt_parts=prompt_parts,
        editor_model=editor_model,
        reasoning_effort=reasoning_effort or "",
        runtime_parts=build_runtime_parts,
    )

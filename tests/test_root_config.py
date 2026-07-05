from __future__ import annotations

from pathlib import Path

from jaunt.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_root_jaunt_toml_loads() -> None:
    """The committed root jaunt.toml must load under strict config validation.

    Jaunt self-hosts, so this file is a real, load-bearing config: strict
    parsing rejects unknown sections/keys, so a successful load pins that every
    section and key we ship is valid.
    """
    cfg = load_config(root=REPO_ROOT, config_path=REPO_ROOT / "jaunt.toml")

    assert cfg.version == 1
    assert cfg.agent.engine == "codex"
    assert cfg.codex.model == "gpt-5.5"
    assert cfg.codex.reasoning_effort == "high"
    assert cfg.codex.sandbox == "workspace-write"

    assert cfg.paths.source_roots == ["src"]
    assert cfg.paths.test_roots == []

    assert cfg.build.emit_stubs is True
    assert cfg.skills.auto is False
    assert cfg.context.repo_map is False

    assert cfg.contract.battery_dir == "tests/contract"
    assert cfg.contract.derive == ["examples", "errors"]
    assert cfg.contract.strength is True

    assert cfg.semantic_gate.enabled is True
    assert cfg.semantic_gate.model == "gpt-5.4-mini"
    assert cfg.semantic_gate.reasoning_effort == "high"

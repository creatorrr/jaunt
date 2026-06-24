from __future__ import annotations

from pathlib import Path

import pytest

from jaunt.config import load_config
from jaunt.errors import JauntConfigError


def test_contract_defaults(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    cfg = load_config(root=tmp_path)
    assert cfg.contract.battery_dir == "tests/contract"
    assert cfg.contract.derive == ["examples", "errors"]
    assert cfg.contract.strength is True


def test_contract_overrides(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[contract]",
                'battery_dir = "tests/battery"',
                'derive = ["errors"]',
                "strength = false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = load_config(root=tmp_path)
    assert cfg.contract.battery_dir == "tests/battery"
    assert cfg.contract.derive == ["errors"]
    assert cfg.contract.strength is False


def test_contract_invalid_derive_raises(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[contract]",
                'derive = ["examples", "bogus"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(JauntConfigError):
        load_config(root=tmp_path)

from __future__ import annotations

import shutil
from pathlib import Path

from jaunt import cli
from jaunt.contract.battery import parse_battery
from jaunt.contract.strength import parse_strength

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contract_slugify"


def test_example_reconciles_and_checks(tmp_path: Path) -> None:
    proj = tmp_path / "contract_slugify"
    shutil.copytree(EXAMPLE, proj)

    assert cli.cmd_reconcile(cli.parse_args(["reconcile", "--root", str(proj)])) == cli.EXIT_OK
    assert cli.cmd_check(cli.parse_args(["check", "--root", str(proj)])) == cli.EXIT_OK

    base = proj / "tests" / "contract" / "contract_slugify" / "specs"
    strong = parse_battery((base / "test_slugify.py").read_text(encoding="utf-8")).header
    weak = parse_battery((base / "test_describe.py").read_text(encoding="utf-8")).header
    assert strong is not None and weak is not None

    sk, sn = parse_strength(strong["strength"])
    wk, wn = parse_strength(weak["strength"])
    # The strong contract pins its body better than the deliberately weak one.
    assert sn > 0 and (sk / sn) > (wk / wn if wn else 1.0)

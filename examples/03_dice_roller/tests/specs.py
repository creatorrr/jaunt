from __future__ import annotations

import jaunt


@jaunt.test()
def test_parse_dice_variants() -> None:
    """
    Target: dice_demo.parse_dice

    parse_dice should accept:
    - "d6" -> (1, 6, 0)
    - "2d6+3" -> (2, 6, 3)
    - "2d6-1" -> (2, 6, -1)
    - whitespace around tokens, ex: "  2d6 + 3  "
    and raise ValueError on invalid inputs.
    """
    import pytest
    from dice_demo import parse_dice

    assert parse_dice("d6") == (1, 6, 0)
    assert parse_dice("2d6+3") == (2, 6, 3)
    assert parse_dice("2d6-1") == (2, 6, -1)
    assert parse_dice("  2d6 + 3  ") == (2, 6, 3)

    for bad in ["", "d", "2d", "2x6", "0d6", "2d0", "2d6++1"]:
        with pytest.raises(ValueError):
            parse_dice(bad)


@jaunt.test()
def test_roll_is_deterministic_with_seeded_rng() -> None:
    """
    Target: dice_demo.roll

    With rng=random.Random(0), the first two d6 rolls are 4 and 4, so:
    - roll("2d6+3", rng=rng) == 11
    """
    import random

    from dice_demo import roll

    rng = random.Random(0)
    assert roll("2d6+3", rng=rng) == 11

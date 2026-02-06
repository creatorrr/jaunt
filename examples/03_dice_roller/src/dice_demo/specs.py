from __future__ import annotations

import random

import jaunt


@jaunt.magic()
def parse_dice(expr: str) -> tuple[int, int, int]:
    """
    Parse dice expressions like "d6", "2d6+3", "2d6-1".

    Return (count, sides, bonus).

    Rules:
    - Allow surrounding whitespace.
    - "d6" means (1, 6, 0).
    - count and sides must be >= 1.
    - bonus defaults to 0 and may be negative.
    - Raise ValueError on invalid syntax.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=parse_dice)
def roll(expr: str, *, rng: random.Random) -> int:
    """
    Roll a dice expression using a provided RNG and return the total.

    - Uses parse_dice(expr) to parse inputs.
    - Rolls `count` times with rng.randint(1, sides).
    - Returns sum(rolls) + bonus.

    Determinism example:
    - With rng=random.Random(0), roll("2d6+3", rng=rng) == 11.
    """
    raise RuntimeError("spec stub (generated at build time)")

"""Dice notation: `d20`, `2d6+3`, `4d8-1`, `coin` / `d2`."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

DICE_RE = re.compile(
    r"^\s*(?:(\d+)\s*)?d\s*(\d+)\s*(?:([+-])\s*(\d+))?\s*$", re.IGNORECASE
)
MAX_DICE = 50
MAX_SIDES = 1000
MAX_MODIFIER = 10_000


@dataclass(frozen=True)
class RollResult:
    notation: str
    rolls: list[int]
    modifier: int
    total: int
    sides: int

    @property
    def breakdown(self) -> str:
        parts = ", ".join(str(roll) for roll in self.rolls)
        if self.modifier > 0:
            return f"[{parts}] + {self.modifier}"
        if self.modifier < 0:
            return f"[{parts}] - {abs(self.modifier)}"
        return f"[{parts}]"


def roll(notation: str, rng: random.Random | None = None) -> RollResult | None:
    text = (notation or "").strip().lower()
    if text == "coin":
        text = "d2"
    match = DICE_RE.match(text)
    if not match:
        return None

    count = int(match.group(1) or 1)
    sides = int(match.group(2))
    modifier = 0
    if match.group(3):
        modifier = int(match.group(4) or 0)
        if match.group(3) == "-":
            modifier = -modifier

    if not (1 <= count <= MAX_DICE and 2 <= sides <= MAX_SIDES):
        return None
    if abs(modifier) > MAX_MODIFIER:
        return None

    rng = rng or random.SystemRandom()
    rolls = [rng.randint(1, sides) for _ in range(count)]
    return RollResult(
        notation=text,
        rolls=rolls,
        modifier=modifier,
        total=sum(rolls) + modifier,
        sides=sides,
    )

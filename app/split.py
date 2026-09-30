from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, localcontext
from typing import NamedTuple, Sequence


class Share(NamedTuple):
    rightsholder_code: str
    ratio: Decimal


class Allocation(NamedTuple):
    rightsholder_code: str
    ratio: Decimal
    amount: int


# Enough guard digits for any practical ratio precision (schema stores 6 dp).
_GUARD_DIGITS = 40


def split_amount(amount: int, shares: Sequence[Share]) -> list[Allocation]:
    """Split one play ``amount`` (non-negative integer, smallest currency unit)
    by ``ratio`` shares whose ratios sum to exactly 100.

    Every share receives floor(amount * ratio / 100). The leftover integer
    units are handed out one by one to the shares with the largest fractional
    parts; ties are broken by ascending rightsholder code. The sum of the
    returned amounts therefore always equals ``amount`` exactly.
    """
    if amount < 0:
        raise ValueError("amount must be non-negative")
    if not shares:
        raise ValueError("at least one share is required")

    with localcontext() as ctx:
        ctx.prec = _GUARD_DIGITS
        total_ratio = sum((s.ratio for s in shares), Decimal(0))
        if total_ratio != Decimal(100):
            raise ValueError(f"ratio total must equal 100, got {total_ratio}")

        rows: list[dict] = []
        for s in shares:
            if s.ratio <= 0:
                raise ValueError("ratios must be positive")
            exact = Decimal(amount) * s.ratio / Decimal(100)
            base = int(exact.to_integral_value(rounding=ROUND_FLOOR))
            rows.append(
                {
                    "code": s.rightsholder_code,
                    "ratio": s.ratio,
                    "exact": exact,
                    "base": base,
                    "frac": exact - Decimal(base),
                    "top_up": 0,
                }
            )

        remainder = amount - sum(r["base"] for r in rows)
        if remainder > len(rows):  # pragma: no cover - mathematically impossible
            raise RuntimeError("remainder cannot exceed the number of shares")

        # Largest fractional part first, then smallest rightsholder code.
        # There are at most n-1 leftover units (each floor discards < 1), so
        # every share is topped up at most once; cycling again would wrongly
        # revive a smaller fractional part.
        for item in sorted(rows, key=lambda x: (-x["frac"], x["code"]))[:remainder]:
            item["top_up"] = 1

        return [
            Allocation(r["code"], r["ratio"], r["base"] + r["top_up"])
            for r in rows
        ]

"""Unit tests for the per-play integer split algorithm.

Runnable without any third-party dependencies:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from app.split import Allocation, Share, split_amount


def shares(spec: list[tuple[str, str]]) -> list[Share]:
    return [Share(code, Decimal(ratio)) for code, ratio in spec]


def total(allocs: list[Allocation]) -> int:
    return sum(a.amount for a in allocs)


class TestSplitAmount(unittest.TestCase):
    def test_single_holder_takes_everything(self):
        allocs = split_amount(100, shares([("H1", "100")]))
        self.assertEqual([(a.rightsholder_code, a.amount) for a in allocs], [("H1", 100)])
        self.assertEqual(total(allocs), 100)

    def test_even_split_no_remainder(self):
        allocs = split_amount(100, shares([("A", "50"), ("B", "50")]))
        self.assertEqual(total(allocs), 100)
        self.assertEqual({a.amount for a in allocs}, {50})

    def test_remainder_by_largest_fraction_then_code(self):
        # 100 / 3 -> 33.333... each: base 33, one leftover unit.
        # Equal fractional parts -> ascending code gets it: A.
        allocs = split_amount(100, shares([("C", "33.33333333"),
                                           ("B", "33.33333333"),
                                           ("A", "33.33333334")]))
        by_code = {a.rightsholder_code: a.amount for a in allocs}
        self.assertEqual(total(allocs), 100)
        self.assertEqual(by_code, {"A": 34, "B": 33, "C": 33})

    def test_remainder_goes_to_largest_fraction_first(self):
        # ratios 60/40 on 3 units -> 1.8 / 1.2 -> base 1/1, leftover 1 -> first holder
        allocs = split_amount(3, shares([("X", "60"), ("Y", "40")]))
        by_code = {a.rightsholder_code: a.amount for a in allocs}
        self.assertEqual(by_code, {"X": 2, "Y": 1})
        self.assertEqual(total(allocs), 3)

    def test_zero_amount(self):
        allocs = split_amount(0, shares([("A", "50"), ("B", "50")]))
        self.assertEqual([a.amount for a in allocs], [0, 0])
        self.assertEqual(total(allocs), 0)

    def test_conservation_many_cases(self):
        ratios_list = [
            [("H1", "10"), ("H2", "20"), ("H3", "30"), ("H4", "40")],
            [("H1", "1"), ("H2", "99")],
            [("H1", "0.000001"), ("H2", "99.999999")],
            [("Z", "12.5"), ("Y", "37.5"), ("X", "50")],
        ]
        for amount in range(0, 250):
            for rs in ratios_list:
                allocs = split_amount(amount, shares(rs))
                self.assertEqual(
                    total(allocs),
                    amount,
                    msg=f"amount={amount} ratios={rs}",
                )
                for a in allocs:
                    self.assertGreaterEqual(a.amount, 0)

    def test_amounts_never_exceed_more_than_one_unit_gap(self):
        # With equal ratios the floor split differs by at most one unit.
        rs = shares([("A", "25"), ("B", "25"), ("C", "25"), ("D", "25")])
        for amount in range(0, 100):
            amounts = [a.amount for a in split_amount(amount, rs)]
            self.assertLessEqual(max(amounts) - min(amounts), 1)

    def test_ratio_total_must_be_100(self):
        with self.assertRaises(ValueError):
            split_amount(100, shares([("A", "50"), ("B", "49")]))

    def test_negative_amount_rejected(self):
        with self.assertRaises(ValueError):
            split_amount(-1, shares([("A", "100")]))

    def test_empty_shares_rejected(self):
        with self.assertRaises(ValueError):
            split_amount(100, [])

    def test_code_tiebreak_is_stable(self):
        # All fractional parts identical; the three leftover units must be
        # handed to the three recipients (n-1 leftovers) in ascending code order.
        rs = shares([("C", "25"), ("B", "25"), ("A", "25"), ("D", "25")])
        by_code = {a.rightsholder_code: a.amount for a in split_amount(7, rs)}
        # 7/4 = 1.75 -> base 1 each, 3 leftovers to A, B, C (ascending).
        self.assertEqual(by_code, {"A": 2, "B": 2, "C": 2, "D": 1})

    def test_two_leftovers_go_to_two_largest_fractions(self):
        # 3 units / 50-30-20: floor 1.5/0.9/0.6 -> 1/0/0; two leftovers.
        # Fractional parts are .5/.9/.6, so the two top-ups go to RH-002 (.9)
        # and RH-003 (.6) -- fraction order, not nominal ratio order.
        rs = shares([("RH-001", "50"), ("RH-002", "30"), ("RH-003", "20")])
        by_code = {a.rightsholder_code: a.amount for a in split_amount(3, rs)}
        self.assertEqual(by_code, {"RH-001": 1, "RH-002": 1, "RH-003": 1})
        self.assertEqual(sum(by_code.values()), 3)

    def test_two_leftovers_fraction_order_skips_smaller(self):
        # 7 units / 40-30-20-10: floor 2.8/2.1/1.4/0.7 -> 2/2/1/0 (sum 5),
        # exactly 2 leftovers. Fraction order: A .8, D .7, C .4, B .1;
        # only A and D are topped up -> 3/2/1/1; C is never revived.
        rs = shares([("A", "40"), ("B", "30"), ("C", "20"), ("D", "10")])
        by_code = {a.rightsholder_code: a.amount for a in split_amount(7, rs)}
        self.assertEqual(by_code, {"A": 3, "B": 2, "C": 1, "D": 1})
        self.assertEqual(sum(by_code.values()), 7)

    def test_realistic_odd_amount(self):
        # amount 12345 units, 3 holders
        rs = shares([("R-001", "50"), ("R-002", "30"), ("R-003", "20")])
        by_code = {a.rightsholder_code: a.amount for a in split_amount(12345, rs)}
        self.assertEqual(sum(by_code.values()), 12345)
        # 6172.5 / 3703.5 / 2469.0 -> floor 6172/3703/2469, one leftover unit;
        # R-001 and R-002 tie on .5, ascending code gives the unit to R-001.
        self.assertEqual(by_code, {"R-001": 6173, "R-002": 3703, "R-003": 2469})


if __name__ == "__main__":
    unittest.main()

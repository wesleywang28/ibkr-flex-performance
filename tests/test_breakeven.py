"""Unit tests for the break-even price (diluted cost) ledger, using hand-made cases only."""
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analysis"))
from engine import diluted_rounds  # noqa: E402

T = pd.Timestamp


def fill(when, sym, qty, price, realized=0.0, fee=0.0):
    """An execution: cash out is positive when buying (incl. the fee), negative when selling."""
    return (T(when), 1, sym, qty, qty * price + fee, realized)


def dividend(when, sym, amount):
    """A dividend (amount > 0 received, < 0 paid on a short)."""
    return (T(when), 0, sym, 0.0, -amount, 0.0)


def cost(rounds, sym):
    return rounds[sym]["net"] / rounds[sym]["qty"]


class BreakEvenTest(unittest.TestCase):
    def test_long_trading_around(self):
        # buy 2 @ 100, sell 1 @ 110, buy 1 back @ 105 -> (200 - 110 + 105) / 2 = 97.5
        r = diluted_rounds([fill("2026-01-02 10:00", "A", 2, 100), fill("2026-01-02 11:00", "A", -1, 110, 10),
                            fill("2026-01-02 12:00", "A", 1, 105)])
        self.assertAlmostEqual(cost(r, "A"), 97.5)
        self.assertAlmostEqual(r["A"]["real"], 10)
        self.assertEqual(r["A"]["n"], 3)

    def test_long_losing_trade_raises_cost(self):
        # buy 2 @ 100, sell 1 @ 95, buy back @ 94: IBKR's FIFO cost is 97, break-even is 99.5
        r = diluted_rounds([fill("2026-01-02 10:00", "B", 2, 100), fill("2026-01-03 10:00", "B", -1, 95, -5),
                            fill("2026-01-03 11:00", "B", 1, 94)])
        self.assertAlmostEqual(cost(r, "B"), 99.5)

    def test_short_trading_around(self):
        # short 2 @ 100, cover 1 @ 90, short 1 again @ 95 -> effective short price 102.5; P&L at 95 = 15
        r = diluted_rounds([fill("2026-01-02 10:00", "S", -2, 100), fill("2026-01-02 11:00", "S", 1, 90, 10),
                            fill("2026-01-02 12:00", "S", -1, 95)])
        self.assertAlmostEqual(cost(r, "S"), 102.5)
        self.assertAlmostEqual(-2 * 95 - r["S"]["net"], 15)

    def test_flip_long_to_short(self):
        # long 100 @ 10, sell 200 @ 12: the old round closes, a new short of 100 @ 12 starts
        r = diluted_rounds([fill("2026-01-02 10:00", "F", 100, 10), fill("2026-01-03 10:00", "F", -200, 12, 200)])
        self.assertAlmostEqual(cost(r, "F"), 12)
        self.assertEqual(r["F"]["qty"], -100)
        self.assertEqual(r["F"]["real"], 0)
        self.assertEqual(r["F"]["start"], T("2026-01-03 10:00"))

    def test_flip_short_to_long(self):
        r = diluted_rounds([fill("2026-01-02 10:00", "G", -50, 20), fill("2026-01-03 10:00", "G", 80, 18, 100)])
        self.assertAlmostEqual(cost(r, "G"), 18)
        self.assertEqual(r["G"]["qty"], 30)

    def test_reset_after_full_close(self):
        r = diluted_rounds([fill("2026-01-02 10:00", "R", 100, 10), fill("2026-01-02 11:00", "R", -100, 11, 100),
                            fill("2026-01-02 12:00", "R", 100, 10.5)])
        self.assertAlmostEqual(cost(r, "R"), 10.5)
        self.assertEqual(r["R"]["real"], 0)
        self.assertEqual(r["R"]["n"], 1)

    def test_closed_position_is_not_returned(self):
        r = diluted_rounds([fill("2026-01-02 10:00", "Z", 100, 10), fill("2026-01-02 11:00", "Z", -100, 11, 100)])
        self.assertNotIn("Z", r)

    def test_dividend_on_long(self):
        # 1.00 per share, 30% withheld -> cost falls by 0.70
        r = diluted_rounds([fill("2026-01-02 10:00", "L", 100, 50), dividend("2026-02-01", "L", 100), dividend("2026-02-01", "L", -30)])
        self.assertAlmostEqual(cost(r, "L"), 49.3)
        self.assertAlmostEqual(r["L"]["div"], 70)

    def test_dividend_paid_on_short(self):
        r = diluted_rounds([fill("2026-01-02 10:00", "P", -100, 50), dividend("2026-02-01", "P", -100)])
        self.assertAlmostEqual(cost(r, "P"), 49)
        self.assertAlmostEqual(r["P"]["div"], -100)

    def test_no_dividend_when_bought_on_ex_date(self):
        r = diluted_rounds([dividend("2026-02-01", "N", 100), fill("2026-02-01 10:00", "N", 100, 50)])
        self.assertAlmostEqual(cost(r, "N"), 50)
        self.assertEqual(r["N"]["div"], 0)

    def test_cost_can_go_negative(self):
        r = diluted_rounds([fill("2026-01-02 10:00", "X", 10, 100), fill("2026-01-03 10:00", "X", -9, 200, 900)])
        self.assertAlmostEqual(cost(r, "X"), -800)

    def test_fees_are_included(self):
        r = diluted_rounds([fill("2026-01-02 10:00", "C", 100, 10, 0, 1.0), fill("2026-01-02 11:00", "C", -50, 12, 99, 1.0)])
        self.assertAlmostEqual(cost(r, "C"), (1001 - 599) / 50)

    def test_seeded_starting_position(self):
        # position already open when the data starts (kind -1), then traded around
        r = diluted_rounds([(T("2026-01-02"), -1, "H", 100, 100 * 20.0, 0.0),
                            fill("2026-01-05 10:00", "H", -50, 22, 100), fill("2026-01-05 11:00", "H", 50, 21)])
        self.assertTrue(r["H"]["seed"])
        self.assertEqual(r["H"]["n"], 2)                  # the seed is not counted as a fill
        self.assertAlmostEqual(cost(r, "H"), (2000 - 1100 + 1050) / 100)


if __name__ == "__main__":
    unittest.main()

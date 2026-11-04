"""End-to-end checks on the fictitious demo account: the numbers have to reconcile with each other."""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "analysis"))
sys.path.insert(0, os.path.join(ROOT, "demo"))
import engine  # noqa: E402
import generate_demo  # noqa: E402

DEMO = os.path.join(ROOT, "demo")


class DemoAccountTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with redirect_stdout(io.StringIO()):
            generate_demo.main()
        cls.R = engine.analyze(DEMO)

    def test_daily_pnl_adds_up_to_total(self):
        d = self.R["d"]
        pnl = d["end"] - d["net_contrib"] - d["pending_tax"]
        day = (d["end"] - d["start"] - d["flow_adj"]).iloc[1:]
        self.assertAlmostEqual(float(day.sum()), float(pnl.iloc[-1] - pnl.iloc[0]), places=2)

    def test_break_even_reconciles_with_ibkr(self):
        stk = [p for p in self.R["positions"] if p["cat"] == "STK"]
        self.assertTrue(stk)
        for p in stk:
            self.assertIsNotNone(p["dil_cost"], p["symbol"])
            self.assertAlmostEqual(p["dil_pnl"], p["upnl"] + p["round_real"] + p["round_div"], places=4, msg=p["symbol"])

    def test_demo_trades_around_positions(self):
        self.assertTrue(any(abs(p["round_real"] or 0) >= 1 for p in self.R["positions"]))

    def test_ledger_matches_daily_positions(self):
        files, days, trades, cash, account = engine.load_statements(DEMO)
        ev = engine.diluted_events(self.R["tdf"], cash, days)
        for dt in sorted(days):
            cut = engine.d8(dt) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            led = engine.diluted_rounds([e for e in ev if e[0] <= cut])
            ib = {}
            for p in days[dt].iter("OpenPosition"):
                if p.get("assetCategory") == "STK":
                    ib[p.get("symbol")] = ib.get(p.get("symbol"), 0.0) + engine.f(p.get("position"))
            for s in set(ib) | set(led):
                self.assertAlmostEqual(ib.get(s, 0.0), led.get(s, {}).get("qty", 0.0), places=6, msg=f"{dt} {s}")

    def test_dashboard_builds_in_both_languages(self):
        import build_dashboard
        for lang in ("en", "zh"):
            with redirect_stdout(io.StringIO()):
                out = build_dashboard.build(DEMO, demo=True, snapshot=False, lang=lang)
            with open(out, encoding="utf-8") as fh:
                html = fh.read()
            self.assertIn('"lang": "%s"' % lang, html)
            self.assertNotIn("/*__DATA__*/", html)


if __name__ == "__main__":
    unittest.main()

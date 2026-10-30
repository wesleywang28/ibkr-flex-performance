"""
Generate a completely fictitious demo dataset so the dashboard can be previewed without an IBKR account.

  python demo/generate_demo.py
  python analysis/build_dashboard.py demo --demo

Output:
  demo/data/raw/flex_demo.xml               simulated Activity Flex Query with "Breakout by Day"
  demo/data/benchmarks/SPY.json, QQQ.json   simulated benchmark prices in Yahoo Finance chart format

The account, prices and trades are randomly generated (fixed seed) and have nothing to do with any real
account or real market data. The demo deliberately includes several deposits, one withdrawal, a temporary
tax withholding that is later refunded, dividends with withholding tax, borrow fees, long and short trades,
and intraday trades.
"""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data")
rng = np.random.default_rng(20250301)

DAYS = pd.bdate_range("2025-03-03", "2025-12-30")
ACCOUNT = "U1234567"

# symbol, description, sub-category, start price, daily vol, daily drift
UNIVERSE = [
    ("NVDA", "NVIDIA CORP", "COMMON", 120, 0.032, 0.0010),
    ("TSLA", "TESLA INC", "COMMON", 260, 0.038, 0.0004),
    ("AMD", "ADVANCED MICRO DEVICES", "COMMON", 105, 0.030, 0.0006),
    ("META", "META PLATFORMS INC-CLASS A", "COMMON", 590, 0.024, 0.0007),
    ("PLTR", "PALANTIR TECHNOLOGIES INC-A", "COMMON", 85, 0.040, 0.0012),
    ("COIN", "COINBASE GLOBAL INC -CLASS A", "COMMON", 210, 0.045, 0.0003),
    ("XLE", "ENERGY SELECT SECTOR SPDR", "ETF", 90, 0.015, -0.0001),
    ("TLT", "ISHARES 20+ YEAR TREASURY BD", "ETF", 90, 0.010, -0.0001),
    ("SQQQ", "PROSHARES ULTRAPRO SHORT QQQ", "ETF", 30, 0.050, -0.0015),
]
DEPOSITS = {0: 30000.0, 55: 10000.0}
WITHDRAWALS = {160: -6000.0}
TEMP_TAX = {110: -1520.40, 112: -830.15, 115: -1265.00, 123: 3615.55}   # three temporary withholdings, refunded at once later
DIVIDEND_DAYS = {48, 112, 175}


def simulate_prices():
    px = {}
    for sym, _, _, p0, vol, drift in UNIVERSE:
        r = rng.normal(drift, vol, len(DAYS))
        px[sym] = p0 * np.exp(np.cumsum(r))
    return pd.DataFrame(px, index=DAYS)


def yahoo_json(sym, p0, vol, drift):
    days = pd.bdate_range("2024-12-02", "2025-12-31")
    r = rng.normal(drift, vol, len(days))
    close = p0 * np.exp(np.cumsum(r))
    ts = [int(datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc).timestamp()) for d in days]
    return {"chart": {"result": [{
        "meta": {"symbol": sym, "currency": "USD", "gmtoffset": -18000, "note": "synthetic demo data"},
        "timestamp": ts,
        "indicators": {"quote": [{"close": [round(float(x), 4) for x in close]}],
                       "adjclose": [{"adjclose": [round(float(x), 4) for x in close]}]},
    }], "error": None}}


def fmt(x, nd=6):
    return f"{x:.{nd}f}".rstrip("0").rstrip(".") if isinstance(x, float) else str(x)


def main():
    px = simulate_prices()
    meta = {u[0]: u for u in UNIVERSE}
    root = ET.Element("FlexQueryResponse", queryName="Demo_Daily", type="AF")
    stmts = ET.SubElement(root, "FlexStatements", count=str(len(DAYS)))

    cash, pos = 0.0, {}        # pos[sym] = dict(q, cost, open_dt, hold)
    prev_close = {}
    tid = 1000
    borrow_acc = 0.0
    prev_end = 0.0
    when = datetime(2026, 1, 2, 8, 0).strftime("%Y%m%d;%H%M%S")

    for i, day in enumerate(DAYS):
        ds = day.strftime("%Y%m%d")
        st = ET.SubElement(stmts, "FlexStatement", accountId=ACCOUNT, fromDate=ds, toDate=ds,
                           period="Last365CalendarDays", whenGenerated=when)
        ET.SubElement(st, "AccountInformation", accountId=ACCOUNT, currency="USD", accountType="Individual",
                      dateOpened=DAYS[0].strftime("%Y%m%d"), dateFunded=DAYS[0].strftime("%Y%m%d"))
        close = px.loc[day]
        start = prev_end
        flows = dict(depositsWithdrawals=0.0, commissions=0.0, dividends=0.0, withholdingTax=0.0,
                     interest=0.0, transactionTax=0.0)
        trades, cash_tx = [], []
        mtm_rows = {}

        def mrow(sym):
            if sym not in mtm_rows:
                pq = pos[sym]["q"] if sym in pos else 0.0
                mtm_rows[sym] = dict(prevQ=pq, prevP=prev_close.get(sym, close[sym]), prior=pq * (close[sym] - prev_close.get(sym, close[sym])),
                                     trans=0.0, comm=0.0)
            return mtm_rows[sym]

        for sym in list(pos):
            mrow(sym)

        # ---- cash flows ----
        for book in (DEPOSITS, WITHDRAWALS):
            if i in book:
                amt = book[i]
                cash += amt
                flows["depositsWithdrawals"] += amt
                cash_tx.append(dict(type="Deposits/Withdrawals", amount=amt, symbol="",
                                    description="CASH RECEIPTS / ELECTRONIC FUND TRANSFERS" if amt > 0 else "DISBURSEMENT INITIATED BY DEMO USER"))
        if i in TEMP_TAX:
            cash += TEMP_TAX[i]
            flows["transactionTax"] += TEMP_TAX[i]
        if day.month != DAYS[i - 1].month and i > 0:      # month start: post last month's interest and borrow fees
            prev_m = DAYS[i - 1].strftime("%b-%Y").upper()
            credit = round(max(cash, 0) * 0.035 / 12, 2)
            if credit > 0:
                cash += credit; flows["interest"] += credit
                cash_tx.append(dict(type="Broker Interest Received", amount=credit, symbol="", description=f"USD CREDIT INT FOR {prev_m}"))
            if borrow_acc > 0.5:
                fee = -round(borrow_acc, 2)
                cash += fee; flows["interest"] += fee
                cash_tx.append(dict(type="Broker Interest Paid", amount=fee, symbol="", description=f"USD BORROW FEES FOR {prev_m}"))
            borrow_acc = 0.0
        if i in DIVIDEND_DAYS:
            longs = [s for s in pos if pos[s]["q"] > 0]
            if longs:
                s = longs[0]
                div = round(pos[s]["q"] * 0.35, 2); wh = round(-div * 0.30, 2)
                cash += div + wh; flows["dividends"] += div; flows["withholdingTax"] += wh
                cash_tx.append(dict(type="Dividends", amount=div, symbol=s, description=f"{s} CASH DIVIDEND USD 0.35 PER SHARE (Ordinary Dividend)"))
                cash_tx.append(dict(type="Withholding Tax", amount=wh, symbol=s, description=f"{s} CASH DIVIDEND USD 0.35 PER SHARE - US TAX"))

        def trade(sym, q, price, oc, t):
            nonlocal cash, tid
            comm = -max(1.0, abs(q) * 0.005)
            cash += -q * price + comm
            flows["commissions"] += comm
            m = mrow(sym)
            m["trans"] += q * (close[sym] - price)
            m["comm"] += comm
            tid += 1
            realized = 0.0
            if oc == "C":
                p = pos[sym]
                realized = p["q"] * price - abs(comm) - p["cost"]
            trades.append(dict(symbol=sym, q=q, price=price, comm=comm, oc=oc, t=t, realized=realized, tid=tid))
            return comm

        # ---- closes ----
        for sym in list(pos):
            p = pos[sym]
            held = (day - p["open_day"]).days
            if held >= p["hold"] or (rng.random() < 0.08 and i < len(DAYS) - 12):
                price = round(close[sym] * (1 + rng.normal(0, 0.006)), 2)
                trade(sym, -p["q"], price, "C", f"{rng.integers(10, 16):02d}{rng.integers(0, 60):02d}{rng.integers(0, 60):02d}")
                del pos[sym]

        # ---- opens (no trading on day one) ----
        nav_now = cash + sum(p["q"] * close[s] for s, p in pos.items())
        if 0 < i < len(DAYS) - 1 and len(pos) < 3 and rng.random() < 0.42:
            cand = [s for s in px.columns if s not in pos]
            sym = cand[rng.integers(len(cand))]
            fut = px[sym].iloc[min(i + 3, len(DAYS) - 1)] / close[sym] - 1
            side = np.sign(fut) if rng.random() < 0.62 else -np.sign(fut)     # give the demo trader a slight edge
            if side < 0 and rng.random() < 0.4:
                side = 1
            side = side or 1
            size = nav_now * rng.uniform(0.25, 0.65)
            price = round(close[sym] * (1 + rng.normal(0, 0.006)), 2)
            q = float(side * max(1, int(size / price)))
            hold = int(rng.choice([0, 0, 1, 1, 2, 3, 4, 6, 9, 14])) if i < len(DAYS) - 12 else 99   # keep a few positions open at the end
            open_t = f"{rng.integers(9, 12):02d}{rng.integers(30, 60):02d}{rng.integers(0, 60):02d}"
            comm = trade(sym, q, price, "O", open_t)          # record the fill first (symbol not in pos yet)
            pos[sym] = dict(q=q, cost=q * price + abs(comm), open_day=day, hold=hold)
            if hold == 0:                                                  # intraday round trip
                p2 = round(close[sym] * (1 + rng.normal(0, 0.008)), 2)
                trade(sym, -q, p2, "C", f"15{rng.integers(10, 59):02d}{rng.integers(0, 60):02d}")
                del pos[sym]

        # accrue borrow fees daily
        borrow_acc += sum(abs(p["q"]) * close[s] for s, p in pos.items() if p["q"] < 0) * 0.03 / 360

        posval = sum(p["q"] * close[s] for s, p in pos.items())
        end = cash + posval
        mtm_total = sum(m["prior"] + m["trans"] for m in mtm_rows.values())

        # ---- XML ----
        eq = ET.SubElement(st, "EquitySummaryInBase")
        ET.SubElement(eq, "EquitySummaryByReportDateInBase", accountId=ACCOUNT, currency="USD",
                      reportDate=(DAYS[i - 1] if i else day).strftime("%Y%m%d"), total=fmt(start))
        ET.SubElement(eq, "EquitySummaryByReportDateInBase", accountId=ACCOUNT, currency="USD",
                      reportDate=ds, total=fmt(end), cash=fmt(cash), stock=fmt(posval))
        dw = flows["depositsWithdrawals"]
        denom = start + max(dw, 0)
        twr = (end - start - dw) / denom * 100 if denom > 1 else 0.0      # mimic IBKR: temporary withholding counts as a loss
        ET.SubElement(st, "ChangeInNAV", accountId=ACCOUNT, currency="USD", fromDate=ds, toDate=ds,
                      startingValue=fmt(start), endingValue=fmt(end), mtm=fmt(mtm_total), twr=fmt(twr, 9),
                      **{k: fmt(v) for k, v in flows.items()})
        mt = ET.SubElement(st, "MTMPerformanceSummaryInBase")
        for sym, m in mtm_rows.items():
            _, desc, sub, *_ = meta[sym]
            cq = pos[sym]["q"] if sym in pos else 0.0
            ET.SubElement(mt, "MTMPerformanceSummaryUnderlying", accountId=ACCOUNT, assetCategory="STK", subCategory=sub,
                          symbol=sym, underlyingSymbol=sym, description=desc, multiplier="1", reportDate=ds,
                          prevCloseQuantity=fmt(m["prevQ"]), prevClosePrice=fmt(round(m["prevP"], 4)),
                          closeQuantity=fmt(cq), closePrice=fmt(round(close[sym], 4)),
                          priorOpenMtm=fmt(m["prior"]), transactionMtm=fmt(m["trans"]), commissions=fmt(m["comm"]),
                          total=fmt(m["prior"] + m["trans"] + m["comm"]))
        op = ET.SubElement(st, "OpenPositions")
        for sym, p in pos.items():
            _, desc, sub, *_ = meta[sym]
            val = p["q"] * close[sym]
            ET.SubElement(op, "OpenPosition", accountId=ACCOUNT, assetCategory="STK", subCategory=sub, symbol=sym,
                          description=desc, multiplier="1", reportDate=ds, position=fmt(p["q"]),
                          markPrice=fmt(round(close[sym], 4)), positionValue=fmt(val), costBasisMoney=fmt(p["cost"]),
                          fifoPnlUnrealized=fmt(val - p["cost"]), side="Long" if p["q"] > 0 else "Short", levelOfDetail="SUMMARY")
        tr = ET.SubElement(st, "Trades")
        for t in trades:
            _, desc, sub, *_ = meta[t["symbol"]]
            ET.SubElement(tr, "Trade", accountId=ACCOUNT, currency="USD", fxRateToBase="1", assetCategory="STK", subCategory=sub,
                          symbol=t["symbol"], underlyingSymbol=t["symbol"], description=desc, multiplier="1",
                          tradeDate=ds, dateTime=f"{ds};{t['t']}", buySell="BUY" if t["q"] > 0 else "SELL",
                          quantity=fmt(t["q"]), tradePrice=fmt(t["price"]), tradeMoney=fmt(t["q"] * t["price"]),
                          proceeds=fmt(-t["q"] * t["price"]), ibCommission=fmt(t["comm"]), ibCommissionCurrency="USD",
                          netCash=fmt(-t["q"] * t["price"] + t["comm"]), openCloseIndicator=t["oc"],
                          fifoPnlRealized=fmt(t["realized"]), transactionID=str(t["tid"]), tradeID=str(t["tid"]),
                          transactionType="ExchTrade", levelOfDetail="EXECUTION")
        ct = ET.SubElement(st, "CashTransactions")
        for c in cash_tx:
            tid += 1
            ET.SubElement(ct, "CashTransaction", accountId=ACCOUNT, currency="USD", fxRateToBase="1", type=c["type"],
                          amount=fmt(c["amount"]), symbol=c["symbol"], description=c["description"],
                          dateTime=ds, settleDate=ds, reportDate=ds, transactionID=str(tid), levelOfDetail="DETAIL")

        prev_close = {s: close[s] for s in px.columns}
        prev_end = end

    os.makedirs(os.path.join(OUT, "raw"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "benchmarks"), exist_ok=True)
    ET.indent(root) if hasattr(ET, "indent") else None
    ET.ElementTree(root).write(os.path.join(OUT, "raw", "flex_demo.xml"), encoding="utf-8", xml_declaration=True)
    for sym, p0, vol, drift in [("SPY", 580, 0.010, 0.0004), ("QQQ", 500, 0.013, 0.0005)]:
        with open(os.path.join(OUT, "benchmarks", f"{sym}.json"), "w", encoding="utf-8") as fh:
            json.dump(yahoo_json(sym, p0, vol, drift), fh)
    print(f"Demo data written to {OUT} ({len(DAYS)} trading days, ending NAV about ${prev_end:,.0f})")


if __name__ == "__main__":
    main()

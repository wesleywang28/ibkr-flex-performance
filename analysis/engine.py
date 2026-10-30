"""
IBKR Flex analysis engine

Reads every Flex XML file under data/history and data/raw (Activity Flex Query with
"Breakout by Day" enabled), merges and de-duplicates them, then computes:
  - daily NAV, external cash flows and an adjusted TWR (temporary tax withholdings are
    treated as cash leaving / returning to the account, not as investment losses)
  - IBKR's own TWR (the daily `twr` field chained together) for comparison
  - money-weighted return (XIRR, Modified Dietz)
  - risk metrics, SPY/QQQ benchmarks and a "same cash flows into the benchmark" simulation
  - positions, per-symbol P&L, trade statistics, fees and income
"""
from __future__ import annotations

import glob
import json
import math
import os
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# ---------------- settings ----------------
_SETTINGS_FP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
SETTINGS = json.load(open(_SETTINGS_FP, encoding="utf-8")) if os.path.exists(_SETTINGS_FP) else {}
RISK_FREE = float(SETTINGS.get("risk_free", 0.04))   # annual risk-free rate used for Sharpe / Sortino
MIN_CAPITAL = 100.0       # days with less capital than this are ignored (e.g. a $1 test deposit)
EXTERNAL_FLOW_FIELDS = [  # ChangeInNAV fields treated as external cash flows
    "depositsWithdrawals", "assetTransfers", "internalCashTransfers",
    "debitCardActivity", "billPay", "donations", "paxosTransfers",
]
TEMP_WITHHOLD_FIELDS = ["transactionTax"]  # temporary withholdings (later refunded): cash flows in the adjusted TWR

# Event labels shown in the dashboard
EV_DEPOSIT, EV_WITHDRAWAL = "Deposit", "Withdrawal"
EV_WITHHOLD, EV_REFUND = "Temp. withholding", "Withholding refund"
LONG, SHORT = "long", "short"


def f(x) -> float:
    try:
        return float(x) if x not in (None, "") else 0.0
    except ValueError:
        return 0.0


def d8(s: str) -> pd.Timestamp:
    return pd.Timestamp(datetime.strptime(s[:8], "%Y%m%d"))


# ======================================================================
# 1. Load and merge
# ======================================================================
def load_statements(root_dir: str):
    files = sorted(glob.glob(os.path.join(root_dir, "data", "history", "*.xml"))) + \
            sorted(glob.glob(os.path.join(root_dir, "data", "raw", "*.xml")))
    if not files:
        latest = os.path.join(root_dir, "data", "latest.xml")
        files = [latest] if os.path.exists(latest) else []
    if not files:
        raise FileNotFoundError("No Flex XML files found under data/")

    days = {}            # toDate -> FlexStatement element (later files override earlier ones)
    trades = {}          # transactionID -> attrib
    cash = {}            # transactionID(+type+amount) -> attrib
    account = {}
    for fp in files:     # history first, then raw in chronological order (newest last)
        root = ET.parse(fp).getroot()
        for st in root.iter("FlexStatement"):
            days[st.get("toDate")] = st
            ai = st.find("AccountInformation")
            if ai is not None and ai.attrib:
                account = dict(ai.attrib)
            for t in st.iter("Trade"):
                key = t.get("transactionID") or t.get("ibExecID") or json.dumps(t.attrib, sort_keys=True)
                trades[key] = dict(t.attrib)
            for c in st.iter("CashTransaction"):
                key = (c.get("transactionID") or "") + "|" + (c.get("type") or "") + "|" + (c.get("amount") or "") + "|" + (c.get("dateTime") or "")
                cash[key] = dict(c.attrib)
    return files, days, list(trades.values()), list(cash.values()), account


# ======================================================================
# 2. Daily NAV and cash flows
# ======================================================================
def daily_frame(days: dict) -> pd.DataFrame:
    rows = []
    for dt in sorted(days):
        st = days[dt]
        c = st.find("ChangeInNAV")
        if c is None:
            continue
        a = c.attrib
        ext = sum(f(a.get(k)) for k in EXTERNAL_FLOW_FIELDS)
        tmp = sum(f(a.get(k)) for k in TEMP_WITHHOLD_FIELDS)
        rows.append(dict(
            date=d8(dt), start=f(a.get("startingValue")), end=f(a.get("endingValue")),
            mtm=f(a.get("mtm")), ext_flow=ext, temp_tax=tmp,
            ibkr_twr=f(a.get("twr")) / 100.0,
            commissions=f(a.get("commissions")), dividends=f(a.get("dividends")),
            interest=f(a.get("interest")), withholding=f(a.get("withholdingTax")),
        ))
    df = pd.DataFrame(rows).set_index("date").sort_index()
    df = df[(df["end"] != 0) | (df["start"] != 0) | (df["ext_flow"] != 0)]
    return df


def twr_series(df: pd.DataFrame, flow_col: str) -> pd.Series:
    """Daily Modified Dietz: inflows at the start of the day, outflows at the end."""
    F = df[flow_col]
    denom = df["start"] + F.clip(lower=0)
    r = (df["end"] - df["start"] - F) / denom.where(denom.abs() >= MIN_CAPITAL)
    return r.fillna(0.0)


def xirr(flows: list[tuple[pd.Timestamp, float]]) -> float | None:
    if len(flows) < 2:
        return None
    t0 = flows[0][0]
    ts = np.array([(d - t0).days / 365.0 for d, _ in flows])
    cf = np.array([v for _, v in flows])

    def npv(r):
        return float(np.sum(cf / (1 + r) ** ts))

    lo, hi = -0.99, 100.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def max_drawdown(idx: pd.Series):
    peak = idx.cummax()
    dd = idx / peak - 1
    trough = dd.idxmin()
    peak_date = idx.loc[:trough].idxmax()
    rec = idx.loc[trough:]
    rec_date = rec[rec >= idx.loc[peak_date]].index.min() if (rec >= idx.loc[peak_date]).any() else None
    return dd, float(dd.min()), peak_date, trough, rec_date


# ======================================================================
# 3. Benchmarks
# ======================================================================
def load_benchmark(root_dir: str, sym: str) -> pd.Series | None:
    fp = os.path.join(root_dir, "data", "benchmarks", f"{sym}.json")
    if not os.path.exists(fp):
        return None
    try:
        j = json.load(open(fp, encoding="utf-8"))
        res = j["chart"]["result"][0]
        ts = res["timestamp"]
        tz = res.get("meta", {}).get("gmtoffset", -14400)
        adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose") or res["indicators"]["quote"][0]["close"]
        dates = [pd.Timestamp(datetime.fromtimestamp(t + tz, tz=timezone.utc).date()) for t in ts]
        s = pd.Series(adj, index=dates, dtype="float64").dropna()
        return s[~s.index.duplicated(keep="last")].sort_index()
    except Exception as e:  # noqa
        print(f"[warn] could not parse benchmark {sym}: {e}")
        return None


# ======================================================================
# 4. Main analysis
# ======================================================================
def analyze(root_dir: str) -> dict:
    files, days, trades, cash, account = load_statements(root_dir)
    df = daily_frame(days)

    df["flow_adj"] = df["ext_flow"] + df["temp_tax"]
    df["r_adj"] = twr_series(df, "flow_adj")      # adjusted (main measure)
    df["r_raw"] = twr_series(df, "ext_flow")      # unadjusted (should be close to IBKR's)

    # Base date: the day before the first day with meaningful capital
    active = df.index[(df["start"] + df["flow_adj"].clip(lower=0)) >= MIN_CAPITAL]
    first = active.min()
    base_date = df.index[df.index.get_loc(first) - 1] if df.index.get_loc(first) > 0 else first
    d = df.loc[base_date:].copy()
    d.loc[base_date, ["r_adj", "r_raw", "ibkr_twr"]] = 0.0
    d["idx_adj"] = (1 + d["r_adj"]).cumprod()
    d["idx_raw"] = (1 + d["r_raw"]).cumprod()
    d["idx_ibkr"] = (1 + d["ibkr_twr"]).cumprod()
    d["net_contrib"] = df["ext_flow"].cumsum().loc[base_date:]
    d["pending_tax"] = df["temp_tax"].cumsum().loc[base_date:]

    end_date = d.index.max()
    nav_end = float(d["end"].iloc[-1])
    total_in = float(df.loc[df["ext_flow"] > 0, "ext_flow"].sum())
    total_out = float(-df.loc[df["ext_flow"] < 0, "ext_flow"].sum())
    net_contrib = total_in - total_out
    pending_tax = float(df["temp_tax"].sum())
    pnl = nav_end - net_contrib                       # actual P&L (unrefunded withholding shows up as a loss)
    pnl_ex_tax = pnl - pending_tax                    # P&L if the withholding is refunded later

    # ---- money-weighted ----
    flows = [(dt, -v) for dt, v in df["ext_flow"].items() if v != 0]
    flows.append((end_date, nav_end - pending_tax))
    irr = xirr(flows)
    T = (end_date - df.index[df["ext_flow"] != 0].min()).days or 1
    w_cap = sum(v * (end_date - dt).days / T for dt, v in df["ext_flow"].items() if v != 0)
    mdietz = pnl_ex_tax / w_cap if w_cap else None

    # ---- risk ----
    r = d["r_adj"].iloc[1:]
    r_eff = r.loc[r.index >= df.index[df["start"] >= 5000].min()] if (df["start"] >= 5000).any() else r
    vol = float(r_eff.std() * math.sqrt(252))
    n_days = len(r)
    cum_adj = float(d["idx_adj"].iloc[-1] - 1)
    ann_ret = (1 + cum_adj) ** (252 / n_days) - 1 if n_days > 0 else None
    sharpe = ((r_eff.mean() * 252) - RISK_FREE) / vol if vol > 0 else None
    downside = r_eff[r_eff < 0].std() * math.sqrt(252)
    sortino = ((r_eff.mean() * 252) - RISK_FREE) / downside if downside and downside > 0 else None
    dd, mdd, mdd_peak, mdd_trough, mdd_rec = max_drawdown(d["idx_adj"])
    dd_ibkr = d["idx_ibkr"] / d["idx_ibkr"].cummax() - 1

    # ---- benchmarks ----
    bench = {}
    for sym in ["SPY", "QQQ"]:
        px = load_benchmark(root_dir, sym)
        if px is None:
            continue
        px = px.reindex(px.index.union(d.index)).ffill().reindex(d.index)
        if px.isna().all():
            continue
        px = px.bfill()
        idx = px / px.iloc[0]
        br = idx.pct_change().fillna(0.0)
        # The same deposits / withdrawals invested in the benchmark instead
        units, sim = 0.0, []
        for dt in d.index:
            fl = df["ext_flow"].get(dt, 0.0) if dt != base_date else 0.0
            if dt == base_date:
                units = d.loc[dt, "end"] / px.loc[dt] if d.loc[dt, "end"] > 0 else 0.0
            if fl:
                units += fl / px.loc[dt]
            sim.append(units * px.loc[dt])
        aligned = pd.concat([r_eff, br.reindex(r_eff.index)], axis=1).dropna()
        beta = corr = alpha = None
        if len(aligned) > 10 and aligned.iloc[:, 1].var() > 0:
            cov = np.cov(aligned.iloc[:, 0], aligned.iloc[:, 1])
            beta = float(cov[0, 1] / cov[1, 1])
            corr = float(aligned.corr().iloc[0, 1])
            alpha = float((aligned.iloc[:, 0].mean() - beta * aligned.iloc[:, 1].mean()) * 252)
        bdd = idx / idx.cummax() - 1
        bench[sym] = dict(
            idx=idx, dd=bdd, sim=pd.Series(sim, index=d.index),
            cum=float(idx.iloc[-1] - 1), vol=float(br.loc[r_eff.index].std() * math.sqrt(252)),
            mdd=float(bdd.min()), beta=beta, corr=corr, alpha=alpha,
            sim_end=float(sim[-1]), sim_pnl=float(sim[-1] - net_contrib),
        )

    # ---- monthly returns ----
    def monthly(idx: pd.Series) -> dict:
        m = idx.groupby(idx.index.to_period("M")).last()
        prev = m.shift(1)
        prev.iloc[0] = idx.iloc[0]
        return {str(k): float(v) for k, v in (m / prev - 1).items()}

    months = dict(account=monthly(d["idx_adj"]), ibkr=monthly(d["idx_ibkr"]))
    for sym, b in bench.items():
        months[sym] = monthly(b["idx"])

    # ---- events (deposits / withdrawals / withholdings / refunds) ----
    events = []
    for dt, row in df.iterrows():
        if row["ext_flow"]:
            events.append(dict(date=dt, kind=EV_DEPOSIT if row["ext_flow"] > 0 else EV_WITHDRAWAL, amount=row["ext_flow"],
                               ibkr=row["ibkr_twr"], adj=float(df.loc[dt, "r_adj"]), nav=row["end"]))
        if row["temp_tax"]:
            events.append(dict(date=dt, kind=EV_WITHHOLD if row["temp_tax"] < 0 else EV_REFUND, amount=row["temp_tax"],
                               ibkr=row["ibkr_twr"], adj=float(df.loc[dt, "r_adj"]), nav=row["end"]))

    # ---- positions and daily exposure ----
    last = days[max(days)]
    positions = []
    for p in last.iter("OpenPosition"):
        mult = f(p.get("multiplier")) or 1
        positions.append(dict(
            symbol=p.get("symbol"), desc=p.get("description"), cat=p.get("assetCategory"), sub=p.get("subCategory"),
            qty=f(p.get("position")), mark=f(p.get("markPrice")), value=f(p.get("positionValue")),
            cost=f(p.get("costBasisMoney")), upnl=f(p.get("fifoPnlUnrealized")),
            avg=f(p.get("costBasisMoney")) / (f(p.get("position")) * mult) if f(p.get("position")) else 0,
        ))
    positions.sort(key=lambda x: -abs(x["value"]))
    long_mv = sum(p["value"] for p in positions if p["value"] > 0)
    short_mv = sum(p["value"] for p in positions if p["value"] < 0)
    eq_last = list(last.iter("EquitySummaryByReportDateInBase"))
    cash_bal = f(eq_last[-1].get("cash")) if eq_last else nav_end - long_mv - short_mv

    expo = []
    for dt in sorted(days):
        st = days[dt]
        L = S = 0.0
        per = defaultdict(float)
        for m in st.iter("MTMPerformanceSummaryUnderlying"):
            if not m.get("symbol") or m.get("assetCategory") in (None, "", "CASH"):
                continue
            v = f(m.get("closeQuantity")) * f(m.get("closePrice")) * (f(m.get("multiplier")) or 1)
            per[m.get("underlyingSymbol") or m.get("symbol").split()[0]] += v
            if v > 0:
                L += v
            else:
                S += v
        top = max(per.items(), key=lambda kv: abs(kv[1])) if per else ("", 0.0)
        expo.append(dict(date=d8(dt), long=L, short=S, max_pos=abs(top[1]), max_sym=top[0]))
    expo = pd.DataFrame(expo).set_index("date").reindex(d.index)
    expo[["long", "short", "max_pos"]] = expo[["long", "short", "max_pos"]].fillna(0.0)
    expo["max_sym"] = expo["max_sym"].fillna("")
    expo["max_pos_pct"] = expo["max_pos"] / d["end"].where(d["end"] > MIN_CAPITAL)
    expo["gross_lev"] = (expo["long"] - expo["short"]) / d["end"].where(d["end"] > MIN_CAPITAL)
    expo["net_lev"] = (expo["long"] + expo["short"]) / d["end"].where(d["end"] > MIN_CAPITAL)

    # ---- per-symbol P&L (options are grouped under their underlying) ----
    sym_pnl = defaultdict(lambda: dict(mtm=0.0, realized=0.0, unrealized=0.0, div=0.0, comm=0.0,
                                       n_exec=0, turnover=0.0, desc=""))
    for st in days.values():
        for m in st.iter("MTMPerformanceSummaryUnderlying"):
            s = m.get("symbol")
            if not s or m.get("assetCategory") in (None, ""):
                continue
            u = m.get("underlyingSymbol") or s.split()[0]
            sym_pnl[u]["mtm"] += f(m.get("total"))
            if not sym_pnl[u]["desc"] and m.get("assetCategory") == "STK":
                sym_pnl[u]["desc"] = m.get("description") or ""
    for t in trades:
        u = t.get("underlyingSymbol") or (t.get("symbol") or "").split()[0]
        sym_pnl[u]["realized"] += f(t.get("fifoPnlRealized"))
        sym_pnl[u]["comm"] += f(t.get("ibCommission"))
        sym_pnl[u]["n_exec"] += 1
        sym_pnl[u]["turnover"] += abs(f(t.get("tradeMoney")) * (f(t.get("fxRateToBase")) or 1))
    for p in positions:
        u = p["symbol"].split()[0]
        sym_pnl[u]["unrealized"] += p["upnl"]
    for c in cash:
        s = c.get("symbol")
        if s and c.get("type") in ("Dividends", "Payment In Lieu Of Dividends", "Withholding Tax"):
            sym_pnl[s]["div"] += f(c.get("amount")) * (f(c.get("fxRateToBase")) or 1)
    sym_rows = []
    for u, v in sym_pnl.items():
        if not u or u == "USD":
            continue
        v["total"] = v["realized"] + v["unrealized"] + v["div"]   # FIFO basis, consistent with IBKR's realized/unrealized report
        sym_rows.append(dict(symbol=u, **v))
    sym_rows.sort(key=lambda x: -x["total"])

    # ---- trade statistics ----
    tdf = pd.DataFrame(trades)
    open_lots = {}
    trade_stats = {}
    closes = []
    hold_days = []
    if not tdf.empty:
        for col in ["quantity", "tradePrice", "fifoPnlRealized", "ibCommission", "tradeMoney", "multiplier", "fxRateToBase"]:
            tdf[col] = tdf[col].map(f)
        tdf["dt"] = tdf["dateTime"].map(lambda s: datetime.strptime(s[:15], "%Y%m%d;%H%M%S") if s and ";" in s else datetime.strptime(s[:8], "%Y%m%d"))
        tdf = tdf.sort_values("dt")
        tdf["u"] = tdf.apply(lambda x: x.get("underlyingSymbol") or x["symbol"].split()[0], axis=1)

        # Closing trades: executions in the same symbol, day and direction count as one closing decision
        c = tdf[tdf["openCloseIndicator"].fillna("").str.contains("C")]
        if not c.empty:
            g = c.groupby(["u", "tradeDate", "buySell"]).agg(pnl=("fifoPnlRealized", "sum"), qty=("quantity", "sum")).reset_index()
            g["side"] = np.where(g["buySell"] == "SELL", LONG, SHORT)
            closes = g.to_dict("records")
            wins = g[g["pnl"] > 0]["pnl"]
            losses = g[g["pnl"] < 0]["pnl"]
            trade_stats = dict(
                n_close=int(len(g)), win_rate=float(len(wins) / len(g)) if len(g) else None,
                avg_win=float(wins.mean()) if len(wins) else 0.0, avg_loss=float(losses.mean()) if len(losses) else 0.0,
                profit_factor=float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() != 0 else None,
                gross_win=float(wins.sum()), gross_loss=float(losses.sum()),
                long_pnl=float(g.loc[g["side"] == LONG, "pnl"].sum()),
                short_pnl=float(g.loc[g["side"] == SHORT, "pnl"].sum()),
                n_long=int((g["side"] == LONG).sum()), n_short=int((g["side"] == SHORT).sum()),
            )
        # Holding periods via FIFO lot matching
        lots = defaultdict(list)
        for _, t in tdf.iterrows():
            key = t["symbol"]
            q = t["quantity"]
            while q != 0 and lots[key] and np.sign(lots[key][0][0]) != np.sign(q):
                lq, ld = lots[key][0]
                m = min(abs(q), abs(lq))
                hold_days.append(((t["dt"] - ld).total_seconds() / 86400, m * abs(t["tradePrice"]) * (t["multiplier"] or 1), t["u"], t["dt"]))
                lq = lq + m * np.sign(q)
                q = q - m * np.sign(q)
                if abs(lq) < 1e-9:
                    lots[key].pop(0)
                else:
                    lots[key][0] = (lq, ld)
            if q != 0:
                lots[key].append((q, t["dt"]))
        open_lots = {k: min(ld for _, ld in v) for k, v in lots.items() if v}
        trade_stats["n_exec"] = int(len(tdf))
        trade_stats["turnover"] = float((tdf["tradeMoney"].abs() * tdf["fxRateToBase"].replace(0, 1)).sum())
        trade_stats["commissions"] = float(tdf["ibCommission"].sum())
        trade_stats["n_symbols"] = int(tdf["u"].nunique())
        if hold_days:
            hd = np.array([h[0] for h in hold_days]); wt = np.array([h[1] for h in hold_days])
            trade_stats["avg_hold_days"] = float(np.average(hd, weights=wt))
            trade_stats["median_hold_days"] = float(np.median(hd))
            trade_stats["intraday_share"] = float(wt[hd < 1].sum() / wt.sum())
        tdf["month"] = tdf["dt"].map(lambda x: x.strftime("%Y-%m"))
        trade_stats["by_month"] = tdf.groupby("month").agg(n=("symbol", "size"), money=("tradeMoney", lambda s: float(s.abs().sum()))).reset_index().to_dict("records")

    avg_nav = float(d.loc[d["end"] > MIN_CAPITAL, "end"].mean())
    trade_stats["turnover_x"] = trade_stats.get("turnover", 0) / avg_nav if avg_nav else None

    # ---- fees and income ----
    fees = defaultdict(float)
    for c in cash:
        t, amt, desc = c.get("type"), f(c.get("amount")) * (f(c.get("fxRateToBase")) or 1), (c.get("description") or "").upper()
        if t == "Dividends" or t == "Payment In Lieu Of Dividends":
            fees["Dividends"] += amt
        elif t == "Withholding Tax":
            fees["Withholding tax (dividends / interest)"] += amt
        elif t == "Broker Interest Received":
            fees["Interest received"] += amt
        elif t == "Broker Interest Paid":
            fees["Borrow fees" if "BORROW" in desc else "Margin interest"] += amt
        elif t in ("Other Fees", "Commission Adjustments", "Advisor Fees"):
            fees["Other fees"] += amt
    fees["Commissions"] = trade_stats.get("commissions", 0.0)
    fees["Temp. withholding not yet refunded"] = pending_tax

    cash_rows = sorted(cash, key=lambda c: c.get("dateTime") or "")

    return dict(
        files=files, account=account, df=df, d=d, base_date=base_date, end_date=end_date,
        nav_end=nav_end, total_in=total_in, total_out=total_out, net_contrib=net_contrib,
        pending_tax=pending_tax, pnl=pnl, pnl_ex_tax=pnl_ex_tax, irr=irr, mdietz=mdietz,
        cum_adj=cum_adj, cum_raw=float(d["idx_raw"].iloc[-1] - 1), cum_ibkr=float(d["idx_ibkr"].iloc[-1] - 1),
        ann_ret=ann_ret, n_days=n_days, vol=vol, sharpe=sharpe, sortino=sortino,
        dd=dd, dd_ibkr=dd_ibkr, mdd=mdd, mdd_peak=mdd_peak, mdd_trough=mdd_trough, mdd_rec=mdd_rec,
        bench=bench, months=months, events=events, positions=positions,
        long_mv=long_mv, short_mv=short_mv, cash_bal=cash_bal, expo=expo,
        sym_rows=sym_rows, trade_stats=trade_stats, closes=closes, hold_days=hold_days,
        fees=dict(fees), cash_rows=cash_rows, avg_nav=avg_nav,
        tdf=tdf, open_lots=open_lots, r_eff=r_eff, settings=SETTINGS, root=root_dir,
        best_day=(r.idxmax(), float(r.max())), worst_day=(r.idxmin(), float(r.min())),
        pos_days=float((r_eff > 0).mean()),
    )


if __name__ == "__main__":
    import sys
    res = analyze(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for k in ["base_date", "end_date", "nav_end", "net_contrib", "pnl", "pnl_ex_tax", "pending_tax", "irr", "mdietz",
              "cum_adj", "cum_raw", "cum_ibkr", "vol", "sharpe", "mdd", "mdd_peak", "mdd_trough", "best_day", "worst_day"]:
        print(f"{k:14s} {res[k]}")

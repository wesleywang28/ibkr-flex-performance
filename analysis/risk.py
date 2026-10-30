"""
Trading-style comparison and risk flags

Takes the result of engine.analyze() and returns:
  style_rows     monthly trading-style metrics
  style_changes  notable changes in the latest month vs. the average of earlier months
  flags          risk flags (high / mid / info)
  rolling        rolling return, volatility and Sharpe (only once rolling_min_days of data exist)

Thresholds live in analysis/settings.json -> "thresholds". Flags describe the data only;
they are not investment advice.
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime

import numpy as np
import pandas as pd

DEFAULT_T = dict(
    single_position_mid=0.40, single_position_high=0.60, gross_mid=1.5, gross_high=2.0,
    single_loss_nav=0.02, loss_vs_avg_win=5, hold_multiple=3, hold_min_days=7,
    drawdown_mid=0.05, drawdown_high=0.10, post_loss_day=-0.03, post_loss_multiple=1.5,
    borrow_fee_multiple=3, borrow_fee_min=50, stale_business_days=3, token_warn_days=30,
    style_change_rel=0.5, style_change_share_pp=0.2,
)
LEVERAGED_RE = re.compile(r"\b(2X|3X|BULL|BEAR|ULTRA|ULTRASHORT|ULTRAPRO|DLY|DAILY|LEVERAGED|INVERSE|-1X|-2X|-3X)\b|BL3X|BR3X")
MONTH_RE = re.compile(r"FOR ([A-Z]{3})-(\d{4})")
MON = dict(JAN=1, FEB=2, MAR=3, APR=4, MAY=5, JUN=6, JUL=7, AUG=8, SEP=9, OCT=10, NOV=11, DEC=12)


CCY_SYMBOL = dict(USD="$", EUR="€", GBP="£", JPY="¥", CNH="¥", CNY="¥", HKD="HK$", SGD="S$", CAD="C$", AUD="A$", CHF="CHF ")
_CUR = {"sym": "$"}


def set_currency(code: str):
    _CUR["sym"] = CCY_SYMBOL.get((code or "USD").upper(), (code or "") + " ")


def money(v: float) -> str:
    return ("-" if v < 0 else "") + _CUR["sym"] + f"{abs(v):,.0f}"


def pct(v: float, dp: int = 1) -> str:
    return f"{v * 100:.{dp}f}%"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ======================================================================
# Monthly trading style
# ======================================================================
def monthly_style(R: dict) -> list[dict]:
    d, expo = R["d"], R["expo"]
    closes = pd.DataFrame(R["closes"])
    tdf = R["tdf"]
    hold = pd.DataFrame(R["hold_days"], columns=["days", "amt", "u", "dt"]) if R["hold_days"] else pd.DataFrame(columns=["days", "amt", "u", "dt"])
    big = d["end"] > 5000
    rows = []
    for m in sorted(R["months"]["account"].keys()):
        mm = m.replace("-", "")
        days_m = d.index[(d.index.strftime("%Y-%m") == m)]
        nav_m = d.loc[days_m, "end"]
        avg_nav = float(nav_m[nav_m > 5000].mean()) if (nav_m > 5000).any() else None
        row = dict(month=m, ret=R["months"]["account"].get(m), n_days=int(len(days_m)))
        c = closes[closes["tradeDate"].str[:6] == mm] if not closes.empty else closes
        if len(c):
            w, l = c[c["pnl"] > 0]["pnl"], c[c["pnl"] < 0]["pnl"]
            row.update(n_close=int(len(c)), win_rate=float(len(w) / len(c)),
                       avg_win=float(w.mean()) if len(w) else None, avg_loss=float(l.mean()) if len(l) else None,
                       payoff=float(w.mean() / -l.mean()) if len(w) and len(l) else None,
                       short_share=float((c["side"] == "short").mean()), max_loss=float(c["pnl"].min()))
        t = tdf[tdf["month"] == m] if not tdf.empty else tdf
        if len(t):
            row.update(n_exec=int(len(t)), n_symbols=int(t["u"].nunique()),
                       turnover_x=float(t["money_base"].sum() / avg_nav) if avg_nav else None,
                       exec_per_day=float(len(t) / max(len(days_m), 1)))
        h = hold[pd.to_datetime(hold["dt"]).dt.strftime("%Y-%m") == m] if len(hold) else hold
        if len(h) and h["amt"].sum() > 0:
            row.update(avg_hold=float(np.average(h["days"], weights=h["amt"])),
                       intraday=float(h.loc[h["days"] < 1, "amt"].sum() / h["amt"].sum()))
        e = expo.loc[days_m][big.loc[days_m]]
        if len(e):
            row.update(avg_gross=float(e["gross_lev"].mean()), max_gross=float(e["gross_lev"].max()),
                       max_pos=float(e["max_pos_pct"].max()),
                       max_pos_sym=str(e.loc[e["max_pos_pct"].idxmax(), "max_sym"]) if e["max_pos_pct"].notna().any() else "")
        rows.append(row)
    return rows


STYLE_METRICS = [  # (key, label, change type: rel = relative change / pp = percentage-point change, format)
    ("exec_per_day", "Executions per day", "rel", "num1"),
    ("turnover_x", "Turnover (traded value / avg NAV)", "rel", "x"),
    ("avg_hold", "Avg holding period", "rel", "days"),
    ("intraday", "Intraday share of closes", "pp", "pct"),
    ("short_share", "Short share of closes", "pp", "pct"),
    ("win_rate", "Win rate", "pp", "pct"),
    ("payoff", "Payoff ratio (avg win / avg loss)", "rel", "num2"),
    ("avg_loss", "Avg loss per close", "rel", "money"),
    ("avg_gross", "Avg gross exposure", "rel", "x"),
    ("max_pos", "Largest single position", "pp", "pct"),
]


def fmt(v, kind):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return dict(num1=f"{v:.1f}", num2=f"{v:.2f}", x=f"{v:.2f}×", days=f"{v:.1f} d", pct=pct(v, 0), money=money(v))[kind]


def style_changes(rows: list[dict], T: dict) -> tuple[str | None, list[dict]]:
    if len(rows) < 2:
        return None, []
    latest, prior = rows[-1], rows[:-1]
    out = []
    for key, name, kind, f in STYLE_METRICS:
        vals = [r[key] for r in prior if r.get(key) is not None]
        cur = latest.get(key)
        if cur is None or not vals:
            continue
        base = float(np.mean(vals))
        if kind == "pp":
            diff = cur - base
            big = abs(diff) >= T["style_change_share_pp"]
            delta = f"{diff * 100:+.0f} pp"
        else:
            if base == 0:
                continue
            rel = (abs(cur) - abs(base)) / abs(base)
            big = abs(rel) >= T["style_change_rel"]
            delta = f"{rel * 100:+.0f}%"
        out.append(dict(key=key, name=name, latest=fmt(cur, f), base=fmt(base, f), delta=delta, big=bool(big)))
    note = None if latest.get("n_days", 0) >= 8 else f"{latest['month']} has only {latest.get('n_days', 0)} trading days, so treat the comparison with caution"
    return note, out


# ======================================================================
# Risk flags
# ======================================================================
def risk_flags(R: dict, rows: list[dict], T: dict) -> list[dict]:
    flags = []
    add = lambda level, title, detail: flags.append(dict(level=level, title=title, detail=detail))
    nav, d, end = R["nav_end"], R["d"], R["end_date"]
    S = R.get("settings", {})

    # ---- data and account status ----
    if R["pending_tax"] <= -1:      # only possible with transaction_tax = "temporary"
        add("high", "Temporary withholding not yet refunded",
            f"{money(-R['pending_tax'])} of transactionTax has not been refunded. The adjusted TWR treats it as cash held back for now; "
            "if it is never refunded, your real return is lower by that amount.")
    if R.get("tax_cost", 0) <= -1:
        add("info" if R.get("tax_mode") == "cost" else "mid", f"Transaction tax counted as a cost: {money(R['tax_cost'])}",
            "IBKR recorded this as transactionTax and it has not been refunded, so it is included in P&L. "
            "If it is a temporary withholding, the return is corrected automatically once the refund appears; "
            "if it is stamp duty or a financial transaction tax, it is a real cost. "
            "Set \"transaction_tax\" in analysis/settings.json to change this treatment.")
    today = pd.Timestamp(R.get("as_of") or datetime.now().date())
    lag = len(pd.bdate_range(end + pd.Timedelta(days=1), today)) - 1
    if lag > T["stale_business_days"]:
        add("mid", "Data may not be updating",
            f"Data ends on {end:%Y-%m-%d}, about {lag} business days ago. Check data/pull_log.txt or run run_pull.bat manually.")
    log = os.path.join(R["root"], "data", "pull_log.txt")
    if os.path.exists(log):
        tail = [l.strip() for l in open(log, encoding="utf-8-sig").read().splitlines() if l.strip()][-3:]
        bad = [l for l in tail if "ERROR:" in l]
        if bad and not any("Success:" in l for l in tail[tail.index(bad[-1]):]):
            add("high", "The last automatic pull failed", bad[-1][20:])
    if S.get("token_expiry"):
        left = (pd.Timestamp(S["token_expiry"]) - today).days
        if left < 0:
            add("high", "Flex token has expired", "Generate a new token in IBKR, then run reset_token.bat to enter it.")
        elif left <= T["token_warn_days"]:
            add("mid", f"Flex token expires in {left} days",
                f"Expiry date: {S['token_expiry']}. Generate a new token in IBKR ahead of time, then run reset_token.bat.")

    # ---- concentration and leverage ----
    for p in R["positions"]:
        w = abs(p["value"]) / nav if nav else 0
        if w >= T["single_position_mid"]:
            lvl = "high" if w >= T["single_position_high"] else "mid"
            add(lvl, f"Concentrated position: {p['symbol']} {'short' if p['qty'] < 0 else 'long'} is {pct(w, 0)} of NAV",
                f"Market value {money(p['value'])}, unrealized {money(p['upnl'])}. A 10% move in this symbol moves the account by about {pct(w * 0.10, 1)}.")
    gross = (R["long_mv"] - R["short_mv"]) / nav if nav else 0
    if gross >= T["gross_mid"]:
        add("high" if gross >= T["gross_high"] else "mid", f"Gross exposure {gross:.2f}× NAV",
            f"Long {money(R['long_mv'])}, short {money(R['short_mv'])}. The higher the exposure, the more the same price move affects NAV.")
    if rows and rows[-1].get("max_gross") and rows[-1]["max_gross"] >= T["gross_high"]:
        add("mid", f"End-of-day gross exposure reached {rows[-1]['max_gross']:.2f}× in {rows[-1]['month']}",
            "Based on closing positions; intraday exposure may have been higher.")

    # ---- shorts ----
    shorts = [p for p in R["positions"] if p["qty"] < 0]
    if shorts:
        lev = [p["symbol"] for p in shorts if LEVERAGED_RE.search((p.get("desc") or "").upper())]
        add("info", "Open short positions", ", ".join(f"{p['symbol']} {money(p['value'])} ({pct(abs(p['value']) / nav, 0)})" for p in shorts) +
            ". Losses on a short are theoretically unlimited, borrow fees accrue while you hold it, and a squeeze or a recall of borrowed shares can force you out." +
            (f" {', '.join(lev)} {'is a' if len(lev) == 1 else 'are'} leveraged/inverse product{'' if len(lev) == 1 else 's'}." if lev else ""))
    fees = {}
    for c in R["cash_rows"]:
        desc = (c.get("description") or "").upper()
        if c.get("type") == "Broker Interest Paid" and "BORROW" in desc:
            m = MONTH_RE.search(desc)
            key = f"{m.group(2)}-{MON.get(m.group(1), 0):02d}" if m else (c.get("dateTime") or "")[:6]
            fees[key] = fees.get(key, 0.0) + _f(c.get("amount"))
    if len(fees) >= 2:
        ks = sorted(fees)
        cur, prev = -fees[ks[-1]], [-fees[k] for k in ks[:-1]]
        base = float(np.mean(prev))
        if cur >= T["borrow_fee_min"] and (base <= 0 or cur >= T["borrow_fee_multiple"] * base):
            add("mid", f"Borrow fees jumped: {money(cur)} for {ks[-1]}",
                f"Previous monthly average {money(base)}. Borrow fees scale with short size and borrow difficulty; hard-to-borrow names can be expensive.")

    # ---- leveraged / inverse ETFs held long ----
    lev_long = [p["symbol"] for p in R["positions"] if p["qty"] > 0 and LEVERAGED_RE.search((p.get("desc") or "").upper())]
    if lev_long:
        add("info", "Holding leveraged / inverse ETFs",
            f"{', '.join(lev_long)}: these rebalance daily, so over several days their return drifts away from 'underlying move × leverage', especially in choppy markets.")

    # ---- losing positions held for a long time ----
    avg_hold = R["trade_stats"].get("avg_hold_days") or 0
    limit = max(T["hold_multiple"] * avg_hold, T["hold_min_days"])
    for p in R["positions"]:
        od = R["open_lots"].get(p["symbol"])
        if od is None:
            continue
        held = (end - pd.Timestamp(od.date())).days
        if p["upnl"] < 0 and held >= limit:
            add("mid", f"Losing position held for a while: {p['symbol']}, {held} days",
                f"Unrealized {money(p['upnl'])} ({pct(p['upnl'] / abs(p['cost'] or 1), 1)}). Your average holding period is about {avg_hold:.1f} days; this one is over {T['hold_multiple']}× that.")

    # ---- large single losses ----
    avg_win = R["trade_stats"].get("avg_win") or 0
    recent = [c for c in R["closes"] if pd.Timestamp(c["tradeDate"]) >= end - pd.Timedelta(days=31)]
    for c in sorted(recent, key=lambda x: x["pnl"])[:3]:
        dt = pd.Timestamp(c["tradeDate"])
        nav_t = float(d["end"].asof(dt)) if len(d) else nav
        if c["pnl"] < 0 and (-c["pnl"] >= T["single_loss_nav"] * nav_t or (avg_win and -c["pnl"] >= T["loss_vs_avg_win"] * avg_win)):
            add("mid", f"Large single loss: {dt:%m/%d} {c['u']} {money(c['pnl'])}",
                f"About {pct(-c['pnl'] / nav_t, 1)} of NAV at the time, or {(-c['pnl'] / avg_win if avg_win else 0):.1f}× your average win ({money(avg_win)}).")
    if rows and rows[-1].get("payoff") is not None and rows[-1]["payoff"] < 1:
        r = rows[-1]
        add("mid", f"Payoff ratio below 1 in {r['month']}",
            f"Average win {money(r['avg_win'] or 0)}, average loss {money(r['avg_loss'] or 0)}. Even with a {pct(r['win_rate'], 0)} win rate, a few large losses can wipe out many small gains.")

    # ---- drawdown ----
    idx = d["idx_adj"]
    cur_dd = float(idx.iloc[-1] / idx.max() - 1)
    if cur_dd <= -T["drawdown_mid"]:
        add("high" if cur_dd <= -T["drawdown_high"] else "mid", f"Currently in a drawdown: {pct(cur_dd, 1)} from the peak",
            f"Peak on {idx.idxmax():%Y-%m-%d}.")

    # ---- does trading speed up after a big losing day? ----
    tdf = R["tdf"]
    if not tdf.empty:
        per_day = tdf.groupby(tdf["dt"].dt.normalize()).size()
        active_days = d.index[d["end"] > 5000]
        per_day = per_day.reindex(active_days).fillna(0)
        r = d["r_adj"].reindex(active_days)
        loss_days = r[r <= T["post_loss_day"]].index
        nxt = [active_days[active_days.get_loc(x) + 1] for x in loss_days if active_days.get_loc(x) + 1 < len(active_days)]
        if len(nxt) >= 3 and per_day.mean() > 0:
            ratio = per_day.loc[nxt].mean() / per_day.mean()
            if ratio >= T["post_loss_multiple"]:
                add("mid", f"More trading the day after a big loss ({ratio:.1f}×)",
                    f"On the day after the {len(nxt)} days with a loss of {pct(-T['post_loss_day'], 0)} or more, you averaged {per_day.loc[nxt].mean():.1f} executions vs. {per_day.mean():.1f} normally.")

    # ---- tail risk from the account's own daily returns ----
    re_ = R["r_eff"]
    if len(re_) >= 30:
        p5 = float(np.percentile(re_, 5))
        tail = float(re_[re_ <= p5].mean())
        add("info", f"Historical daily returns: about 1 day in 20 loses more than {pct(-p5, 1)}",
            f"About {money(-p5 * nav)} at current NAV; the worst 5% of days lost {pct(-tail, 1)} on average (about {money(-tail * nav)}). "
            f"Estimated from your last {len(re_)} trading days; not a forecast.")

    order = dict(high=0, mid=1, info=2)
    flags.sort(key=lambda x: order[x["level"]])
    return flags


# ======================================================================
# Rolling metrics
# ======================================================================
def rolling_metrics(R: dict) -> dict | None:
    S = R.get("settings", {})
    win, need = int(S.get("rolling_window", 63)), int(S.get("rolling_min_days", 252))
    r = R["r_eff"]
    if len(r) < need:
        return None
    ret = (1 + r).rolling(win).apply(np.prod, raw=True) - 1
    vol = r.rolling(win).std() * math.sqrt(252)
    sharpe = (r.rolling(win).mean() * 252 - float(S.get("risk_free", 0.04))) / vol
    df = pd.DataFrame(dict(ret=ret, vol=vol, sharpe=sharpe)).dropna()
    return dict(window=win, dates=[x.strftime("%Y-%m-%d") for x in df.index],
                ret=df["ret"].round(5).tolist(), vol=df["vol"].round(5).tolist(), sharpe=df["sharpe"].round(3).tolist())


def analyze_style_risk(R: dict) -> dict:
    set_currency(R.get("currency", "USD"))
    T = dict(DEFAULT_T, **R.get("settings", {}).get("thresholds", {}))
    rows = monthly_style(R)
    note, changes = style_changes(rows, T)
    return dict(style_rows=rows, style_note=note, style_changes=changes,
                flags=risk_flags(R, rows, T), rolling=rolling_metrics(R))

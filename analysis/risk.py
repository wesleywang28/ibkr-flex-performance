"""
Trading-style comparison and risk flags

Takes the result of engine.analyze() and returns:
  style_rows     monthly trading-style metrics
  style_changes  notable changes in the latest month vs. the average of earlier months
  flags          risk flags (high / mid / info)
  rolling        rolling return, volatility and Sharpe (only once rolling_min_days of data exist)

Thresholds live in analysis/settings.json -> "thresholds". Flags describe the data only;
they are not investment advice. Messages are in English or Chinese depending on
settings.json -> "language" ("en" or "zh").
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
_LANG = {"v": "en"}


def set_currency(code: str):
    _CUR["sym"] = CCY_SYMBOL.get((code or "USD").upper(), (code or "") + " ")


def set_language(lang: str):
    _LANG["v"] = "zh" if str(lang or "").lower().startswith("zh") else "en"


def tr(en: str, zh: str) -> str:
    """Pick the message for the current language."""
    return zh if _LANG["v"] == "zh" else en


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


STYLE_METRICS = [  # (key, (English label, Chinese label), change type: rel = relative change / pp = percentage-point change, format)
    ("exec_per_day", ("Executions per day", "日均成交笔数"), "rel", "num1"),
    ("turnover_x", ("Turnover (traded value / avg NAV)", "换手（成交额 ÷ 平均净值）"), "rel", "x"),
    ("avg_hold", ("Avg holding period", "平均持仓时间"), "rel", "days"),
    ("intraday", ("Intraday share of closes", "日内平仓占比"), "pp", "pct"),
    ("short_share", ("Short share of closes", "空头平仓占比"), "pp", "pct"),
    ("win_rate", ("Win rate", "胜率"), "pp", "pct"),
    ("payoff", ("Payoff ratio (avg win / avg loss)", "盈亏比（平均盈利 ÷ 平均亏损）"), "rel", "num2"),
    ("avg_loss", ("Avg loss per close", "平均单笔亏损"), "rel", "money"),
    ("avg_gross", ("Avg gross exposure", "平均总敞口"), "rel", "x"),
    ("max_pos", ("Largest single position", "单一标的最大占比"), "pp", "pct"),
]


def fmt(v, kind):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return dict(num1=f"{v:.1f}", num2=f"{v:.2f}", x=f"{v:.2f}×", days=f"{v:.1f}" + tr(" d", " 天"), pct=pct(v, 0), money=money(v))[kind]


def style_changes(rows: list[dict], T: dict) -> tuple[str | None, list[dict]]:
    if len(rows) < 2:
        return None, []
    latest, prior = rows[-1], rows[:-1]
    out = []
    for key, names, kind, f in STYLE_METRICS:
        vals = [r[key] for r in prior if r.get(key) is not None]
        cur = latest.get(key)
        if cur is None or not vals:
            continue
        base = float(np.mean(vals))
        if kind == "pp":
            diff = cur - base
            big = abs(diff) >= T["style_change_share_pp"]
            delta = f"{diff * 100:+.0f}" + tr(" pp", " 个百分点")
        else:
            if base == 0:
                continue
            rel = (abs(cur) - abs(base)) / abs(base)
            big = abs(rel) >= T["style_change_rel"]
            delta = f"{rel * 100:+.0f}%"
        out.append(dict(key=key, name=tr(*names), latest=fmt(cur, f), base=fmt(base, f), delta=delta, big=bool(big)))
    n = latest.get("n_days", 0)
    note = None if n >= 8 else tr(f"{latest['month']} has only {n} trading days, so treat the comparison with caution",
                                  f"{latest['month']} 只有 {n} 个交易日，对比仅供参考")
    return note, out


# ======================================================================
# Risk flags
# ======================================================================
def risk_flags(R: dict, rows: list[dict], T: dict) -> list[dict]:
    flags = []
    add = lambda level, title, detail: flags.append(dict(level=level, title=title, detail=detail))
    nav, d, end = R["nav_end"], R["d"], R["end_date"]
    S = R.get("settings", {})
    sep = tr(", ", "、")

    # ---- data and account status ----
    if R["pending_tax"] <= -1:      # only possible with transaction_tax = "temporary"
        add("high", tr("Temporary withholding not yet refunded", "有临时预扣尚未退回"),
            tr(f"{money(-R['pending_tax'])} of transactionTax has not been refunded. The adjusted TWR treats it as cash held back for now; "
               "if it is never refunded, your real return is lower by that amount.",
               f"目前仍有 {money(-R['pending_tax'])} 的 transactionTax 未退回，调整口径暂时把它当作暂扣资金；如果最终不退回，实际收益要扣掉这笔钱。"))
    if R.get("tax_cost", 0) <= -1:
        add("info" if R.get("tax_mode") == "cost" else "mid",
            tr(f"Transaction tax counted as a cost: {money(R['tax_cost'])}", f"交易税计入成本：{money(R['tax_cost'])}"),
            tr("IBKR recorded this as transactionTax and it has not been refunded, so it is included in P&L. "
               "If it is a temporary withholding, the return is corrected automatically once the refund appears; "
               "if it is stamp duty or a financial transaction tax, it is a real cost. "
               "Set \"transaction_tax\" in analysis/settings.json to change this treatment.",
               "IBKR 把这笔记为 transactionTax，且尚未退回，所以计入了盈亏。如果是临时预扣，退回后收益会自动修正；"
               "如果是印花税或金融交易税，就是真实成本。可以在 analysis/settings.json 的 \"transaction_tax\" 修改处理方式。"))
    today = pd.Timestamp(R.get("as_of") or datetime.now().date())
    lag = len(pd.bdate_range(end + pd.Timedelta(days=1), today)) - 1
    if lag > T["stale_business_days"]:
        add("mid", tr("Data may not be updating", "数据可能没有自动更新"),
            tr(f"Data ends on {end:%Y-%m-%d}, about {lag} business days ago. Check data/pull_log.txt or run run_pull.bat manually.",
               f"数据截至 {end:%Y-%m-%d}，已经落后约 {lag} 个工作日。检查 data/pull_log.txt，或双击 run_pull.bat 手动拉取。"))
    log = os.path.join(R["root"], "data", "pull_log.txt")
    if os.path.exists(log):
        tail = [l.strip() for l in open(log, encoding="utf-8-sig").read().splitlines() if l.strip()][-3:]
        bad = [l for l in tail if "ERROR:" in l]
        if bad and not any("Success:" in l for l in tail[tail.index(bad[-1]):]):
            add("high", tr("The last automatic pull failed", "最近一次自动拉取失败"), bad[-1][20:])
    if S.get("token_expiry"):
        left = (pd.Timestamp(S["token_expiry"]) - today).days
        if left < 0:
            add("high", tr("Flex token has expired", "Flex Token 已过期"),
                tr("Generate a new token in IBKR, then run reset_token.bat to enter it.",
                   "在 IBKR 生成新的 Token 后，双击 reset_token.bat 重新输入。"))
        elif left <= T["token_warn_days"]:
            add("mid", tr(f"Flex token expires in {left} days", f"Flex Token 还有 {left} 天过期"),
                tr(f"Expiry date: {S['token_expiry']}. Generate a new token in IBKR ahead of time, then run reset_token.bat.",
                   f"到期日 {S['token_expiry']}。提前在 IBKR 生成新的 Token，再双击 reset_token.bat。"))

    # ---- concentration and leverage ----
    for p in R["positions"]:
        w = abs(p["value"]) / nav if nav else 0
        if w >= T["single_position_mid"]:
            lvl = "high" if w >= T["single_position_high"] else "mid"
            short = p["qty"] < 0
            add(lvl, tr(f"Concentrated position: {p['symbol']} {'short' if short else 'long'} is {pct(w, 0)} of NAV",
                        f"单一持仓集中：{p['symbol']} {'空头' if short else '多头'}占净值 {pct(w, 0)}"),
                tr(f"Market value {money(p['value'])}, unrealized {money(p['upnl'])}. A 10% move in this symbol moves the account by about {pct(w * 0.10, 1)}.",
                   f"市值 {money(p['value'])}，未实现 {money(p['upnl'])}。这个标的波动 10%，账户大约变动 {pct(w * 0.10, 1)}。"))
    gross = (R["long_mv"] - R["short_mv"]) / nav if nav else 0
    if gross >= T["gross_mid"]:
        add("high" if gross >= T["gross_high"] else "mid", tr(f"Gross exposure {gross:.2f}× NAV", f"总敞口 {gross:.2f}× 净值"),
            tr(f"Long {money(R['long_mv'])}, short {money(R['short_mv'])}. The higher the exposure, the more the same price move affects NAV.",
               f"多头 {money(R['long_mv'])}，空头 {money(R['short_mv'])}。敞口越高，同样的价格波动对净值影响越大。"))
    if rows and rows[-1].get("max_gross") and rows[-1]["max_gross"] >= T["gross_high"]:
        mg, mo = rows[-1]["max_gross"], rows[-1]["month"]
        add("mid", tr(f"End-of-day gross exposure reached {mg:.2f}× in {mo}", f"{mo} 收盘总敞口最高到过 {mg:.2f}×"),
            tr("Based on closing positions; intraday exposure may have been higher.", "按每日收盘持仓计算，日内敞口可能更高。"))

    # ---- shorts ----
    shorts = [p for p in R["positions"] if p["qty"] < 0]
    if shorts:
        lev = [p["symbol"] for p in shorts if LEVERAGED_RE.search((p.get("desc") or "").upper())]
        lst = sep.join(f"{p['symbol']} {money(p['value'])} " + tr(f"({pct(abs(p['value']) / nav, 0)})", f"（{pct(abs(p['value']) / nav, 0)}）") for p in shorts)
        if _LANG["v"] == "zh":
            detail = lst + "。空头的亏损理论上没有上限，持有期间还要付融券费；遇到逼空或借券被召回时可能被动平仓。" + \
                (f"其中 {sep.join(lev)} 是杠杆/反向产品。" if lev else "")
        else:
            detail = lst + ". Losses on a short are theoretically unlimited, borrow fees accrue while you hold it, and a squeeze or a recall of borrowed shares can force you out." + \
                (f" {', '.join(lev)} {'is a' if len(lev) == 1 else 'are'} leveraged/inverse product{'' if len(lev) == 1 else 's'}." if lev else "")
        add("info", tr("Open short positions", "当前持有空头"), detail)
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
            add("mid", tr(f"Borrow fees jumped: {money(cur)} for {ks[-1]}", f"融券费上升：{ks[-1]} 为 {money(cur)}"),
                tr(f"Previous monthly average {money(base)}. Borrow fees scale with short size and borrow difficulty; hard-to-borrow names can be expensive.",
                   f"此前月均 {money(base)}。融券费随做空规模和借券难度变化，难借的标的费率可能很高。"))

    # ---- leveraged / inverse ETFs held long ----
    lev_long = [p["symbol"] for p in R["positions"] if p["qty"] > 0 and LEVERAGED_RE.search((p.get("desc") or "").upper())]
    if lev_long:
        add("info", tr("Holding leveraged / inverse ETFs", "持有杠杆/反向 ETF"),
            tr(f"{', '.join(lev_long)}: these rebalance daily, so over several days their return drifts away from 'underlying move × leverage', especially in choppy markets.",
               f"{sep.join(lev_long)}：这类产品每天再平衡，持有多天后收益会偏离“标的涨跌 × 倍数”，震荡市里损耗更明显。"))

    # ---- losing positions held for a long time ----
    avg_hold = R["trade_stats"].get("avg_hold_days") or 0
    limit = max(T["hold_multiple"] * avg_hold, T["hold_min_days"])
    for p in R["positions"]:
        od = R["open_lots"].get(p["symbol"])
        if od is None:
            continue
        held = (end - pd.Timestamp(od.date())).days
        if p["upnl"] < 0 and held >= limit:
            ret = pct(p['upnl'] / abs(p['cost'] or 1), 1)
            add("mid", tr(f"Losing position held for a while: {p['symbol']}, {held} days", f"亏损持仓持有较久：{p['symbol']} 已 {held} 天"),
                tr(f"Unrealized {money(p['upnl'])} ({ret}). Your average holding period is about {avg_hold:.1f} days; this one is over {T['hold_multiple']}× that.",
                   f"未实现 {money(p['upnl'])}（{ret}）。你的平均持仓约 {avg_hold:.1f} 天，这笔已超过 {T['hold_multiple']} 倍。"))

    # ---- large single losses ----
    avg_win = R["trade_stats"].get("avg_win") or 0
    recent = [c for c in R["closes"] if pd.Timestamp(c["tradeDate"]) >= end - pd.Timedelta(days=31)]
    for c in sorted(recent, key=lambda x: x["pnl"])[:3]:
        dt = pd.Timestamp(c["tradeDate"])
        nav_t = float(d["end"].asof(dt)) if len(d) else nav
        if c["pnl"] < 0 and (-c["pnl"] >= T["single_loss_nav"] * nav_t or (avg_win and -c["pnl"] >= T["loss_vs_avg_win"] * avg_win)):
            sh, mult = pct(-c['pnl'] / nav_t, 1), (-c['pnl'] / avg_win if avg_win else 0)
            add("mid", tr(f"Large single loss: {dt:%m/%d} {c['u']} {money(c['pnl'])}", f"单笔亏损偏大：{dt:%m/%d} {c['u']} {money(c['pnl'])}"),
                tr(f"About {sh} of NAV at the time, or {mult:.1f}× your average win ({money(avg_win)}).",
                   f"约占当时净值 {sh}，相当于平均盈利（{money(avg_win)}）的 {mult:.1f} 倍。"))
    if rows and rows[-1].get("payoff") is not None and rows[-1]["payoff"] < 1:
        r = rows[-1]
        aw, al, wr = money(r['avg_win'] or 0), money(r['avg_loss'] or 0), pct(r['win_rate'], 0)
        add("mid", tr(f"Payoff ratio below 1 in {r['month']}", f"{r['month']} 盈亏比低于 1"),
            tr(f"Average win {aw}, average loss {al}. Even with a {wr} win rate, a few large losses can wipe out many small gains.",
               f"平均盈利 {aw}，平均亏损 {al}。即使胜率有 {wr}，几笔大亏也可能吃掉多笔小赚。"))

    # ---- drawdown ----
    idx = d["idx_adj"]
    cur_dd = float(idx.iloc[-1] / idx.max() - 1)
    if cur_dd <= -T["drawdown_mid"]:
        add("high" if cur_dd <= -T["drawdown_high"] else "mid",
            tr(f"Currently in a drawdown: {pct(cur_dd, 1)} from the peak", f"当前处于回撤中：距高点 {pct(cur_dd, 1)}"),
            tr(f"Peak on {idx.idxmax():%Y-%m-%d}.", f"高点在 {idx.idxmax():%Y-%m-%d}。"))

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
                a, b, th = per_day.loc[nxt].mean(), per_day.mean(), pct(-T['post_loss_day'], 0)
                add("mid", tr(f"More trading the day after a big loss ({ratio:.1f}×)", f"大亏后次日交易明显增多（{ratio:.1f}×）"),
                    tr(f"On the day after the {len(nxt)} days with a loss of {th} or more, you averaged {a:.1f} executions vs. {b:.1f} normally.",
                       f"单日亏损超过 {th} 的 {len(nxt)} 个交易日，次日平均成交 {a:.1f} 笔，平时 {b:.1f} 笔。"))

    # ---- tail risk from the account's own daily returns ----
    re_ = R["r_eff"]
    if len(re_) >= 30:
        p5 = float(np.percentile(re_, 5))
        tail = float(re_[re_ <= p5].mean())
        add("info", tr(f"Historical daily returns: about 1 day in 20 loses more than {pct(-p5, 1)}",
                       f"按历史日收益：约每 20 个交易日有 1 天亏损超过 {pct(-p5, 1)}"),
            tr(f"About {money(-p5 * nav)} at current NAV; the worst 5% of days lost {pct(-tail, 1)} on average (about {money(-tail * nav)}). "
               f"Estimated from your last {len(re_)} trading days; not a forecast.",
               f"按当前净值约 {money(-p5 * nav)}；最差的 5% 交易日平均亏 {pct(-tail, 1)}（约 {money(-tail * nav)}）。"
               f"按你过去 {len(re_)} 个交易日的实际表现估算，不代表未来。"))

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


def analyze_style_risk(R: dict, lang: str | None = None) -> dict:
    set_currency(R.get("currency", "USD"))
    set_language(lang or R.get("settings", {}).get("language", "en"))
    T = dict(DEFAULT_T, **R.get("settings", {}).get("thresholds", {}))
    rows = monthly_style(R)
    note, changes = style_changes(rows, T)
    return dict(style_rows=rows, style_note=note, style_changes=changes,
                flags=risk_flags(R, rows, T), rolling=rolling_metrics(R))

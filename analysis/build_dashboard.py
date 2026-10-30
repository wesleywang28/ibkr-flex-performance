"""
Build the IBKR performance dashboard (a single, self-contained HTML file)

Usage:
  python analysis/build_dashboard.py                 # uses data/ under the project root
  python analysis/build_dashboard.py <project root>
  python analysis/build_dashboard.py demo --demo     # demo data (run demo/generate_demo.py first)

Output:
  <root>/ibkr_dashboard.html                  latest dashboard (overwritten each run)
  <root>/reports/ibkr_dashboard_YYYY-MM.html  monthly snapshot (overwritten within the month,
                                              so each month keeps its last state)
  <root>/data/last_summary.json               used to report "changes since the last update"
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.request
from datetime import datetime

try:
    import pandas as pd
except ImportError:
    sys.exit("Missing dependencies. Run:  pip install -r requirements.txt")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine  # noqa: E402
import risk  # noqa: E402

OUT_NAME = "ibkr_dashboard"
CHARTJS_VERSION = "4.4.1"
CHARTJS_CDNS = [
    f"https://cdn.jsdelivr.net/npm/chart.js@{CHARTJS_VERSION}/dist/chart.umd.js",
    f"https://cdnjs.cloudflare.com/ajax/libs/Chart.js/{CHARTJS_VERSION}/chart.umd.min.js",
    f"https://unpkg.com/chart.js@{CHARTJS_VERSION}/dist/chart.umd.js",
]


def clean(x):
    """Convert numpy / pandas / NaN values into JSON-serializable ones."""
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, pd.Timestamp):
        return x.strftime("%Y-%m-%d")
    if hasattr(x, "item"):
        x = x.item()
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def ser(s: pd.Series, nd=6):
    return [None if pd.isna(v) else round(float(v), nd) for v in s.values]


def chartjs_tag() -> str:
    """Inline a local copy of Chart.js so the page works offline; download it once into
    analysis/vendor/ if missing; fall back to the CDN if the download fails."""
    fp = os.path.join(HERE, "vendor", "chart.umd.min.js")
    if not os.path.exists(fp):
        for url in CHARTJS_CDNS:
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    js = r.read().decode("utf-8")
                if "Chart" in js and len(js) > 100_000:
                    os.makedirs(os.path.dirname(fp), exist_ok=True)
                    with open(fp, "w", encoding="utf-8") as fh:
                        fh.write(js)
                    print(f"Downloaded Chart.js {CHARTJS_VERSION} to {fp}")
                    break
            except Exception:  # noqa
                continue
    if os.path.exists(fp):
        return "<script>" + open(fp, encoding="utf-8").read() + "</script>"
    print("[warn] Could not download Chart.js; the page will load it from a CDN (needs internet when opened)")
    return f'<script src="{CHARTJS_CDNS[0]}"></script>'


def build(root: str, demo: bool = False, snapshot: bool = True) -> str:
    R = engine.analyze(root)
    if demo:
        R["as_of"] = R["end_date"]          # demo data: don't flag the data as stale
        R["settings"] = dict(R["settings"], token_expiry="")
    X = risk.analyze_style_risk(R)
    R["_x"] = X
    d = R["d"]
    dates = [x.strftime("%Y-%m-%d") for x in d.index]
    acct = R["account"].get("accountId", "")
    masked = acct[:5] + "•••" + acct[-2:] if len(acct) > 7 else acct

    series = dict(
        dates=dates,
        adj=ser(d["idx_adj"] - 1), ibkr=ser(d["idx_ibkr"] - 1),
        r_adj=ser(d["r_adj"]), r_ibkr=ser(d["ibkr_twr"]),
        nav=ser(d["end"], 2), contrib=ser(d["net_contrib"], 2),
        dd_adj=ser(R["dd"]), dd_ibkr=ser(R["dd_ibkr"]),
        long=ser(R["expo"]["long"], 2), short=ser(R["expo"]["short"], 2),
        gross=ser(R["expo"]["gross_lev"], 4), net=ser(R["expo"]["net_lev"], 4),
    )
    bench = {}
    for sym, b in R["bench"].items():
        series[sym] = ser(b["idx"] - 1)
        series["dd_" + sym] = ser(b["dd"])
        series["sim_" + sym] = ser(b["sim"], 2)
        bench[sym] = {k: v for k, v in b.items() if not isinstance(v, pd.Series)}

    months = sorted(R["months"]["account"].keys())
    month_rows = [dict(m=m, **{k: R["months"][k].get(m) for k in R["months"]}) for m in months]

    closes = sorted(R["closes"], key=lambda x: x["pnl"])
    top_loss = [c for c in closes[:6] if c["pnl"] < 0]
    top_win = [c for c in closes[::-1][:6] if c["pnl"] > 0]

    payload = clean(dict(
        meta=dict(account=masked, currency=R["account"].get("currency", "USD"),
                  opened=R["account"].get("dateOpened", ""), start=R["base_date"], end=R["end_date"],
                  generated=datetime.now().strftime("%Y-%m-%d %H:%M"), n_files=len(R["files"]),
                  rf=engine.RISK_FREE, demo=demo),
        kpi=dict(nav=R["nav_end"], contrib=R["net_contrib"], total_in=R["total_in"], total_out=R["total_out"],
                 pnl=R["pnl"], pnl_ex_tax=R["pnl_ex_tax"], pending_tax=R["pending_tax"],
                 start_value=R["start_value"], pre_existing=R["pre_existing"], tax_cost=R["tax_cost"],
                 cum_adj=R["cum_adj"], cum_ibkr=R["cum_ibkr"], cum_raw=R["cum_raw"],
                 mdietz=R["mdietz"], irr=R["irr"], ann=R["ann_ret"], n_days=R["n_days"],
                 vol=R["vol"], sharpe=R["sharpe"], sortino=R["sortino"],
                 mdd=R["mdd"], mdd_peak=R["mdd_peak"], mdd_trough=R["mdd_trough"], mdd_rec=R["mdd_rec"],
                 best=R["best_day"], worst=R["worst_day"], pos_days=R["pos_days"], avg_nav=R["avg_nav"],
                 long_mv=R["long_mv"], short_mv=R["short_mv"], cash=R["cash_bal"]),
        bench=bench, series=series, events=R["events"], months=month_rows,
        positions=R["positions"], syms=R["sym_rows"], trade=R["trade_stats"],
        top_win=top_win, top_loss=top_loss, fees=R["fees"],
        flags=X["flags"], style_rows=X["style_rows"], style_changes=X["style_changes"],
        style_note=X["style_note"], rolling=X["rolling"],
    ))

    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    html = tpl.replace("<script>/*__CHARTJS__*/</script>", chartjs_tag()) \
              .replace("/*__DATA__*/", json.dumps(payload, ensure_ascii=False))
    out = os.path.join(root, f"{OUT_NAME}.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    if snapshot:
        rep_dir = os.path.join(root, "reports")
        os.makedirs(rep_dir, exist_ok=True)
        snap = os.path.join(rep_dir, f"{OUT_NAME}_{R['end_date']:%Y-%m}.html")
        with open(snap, "w", encoding="utf-8") as fh:
            fh.write(html)
    print_summary(R, root)
    return out


def print_summary(R: dict, root: str):
    """Print a short text summary and compare it with the previous run."""
    d = R["d"]
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    snap_fp = os.path.join(root, "data", "last_summary.json")
    prev = json.load(open(snap_fp, encoding="utf-8")) if os.path.exists(snap_fp) else None
    end = R["end_date"].strftime("%Y-%m-%d")
    spy = R["bench"].get("SPY")
    m = lambda v, sign=False: ("+" if sign and v > 0 else "") + risk.money(v)
    lines = [f"Data through {end} ({R['n_days']} trading days)",
             f"NAV {m(R['nav_end'])}, total P&L {m(R['pnl'], True)}, net contributions {m(R['net_contrib'])}" +
             (f" (incl. starting value {m(R['start_value'])})" if R["pre_existing"] else ""),
             f"Latest day {d['r_adj'].iloc[-1]:+.2%}" + (f" (SPY {spy['idx'].iloc[-1] / spy['idx'].iloc[-2] - 1:+.2%})" if spy else ""),
             f"Cumulative TWR {R['cum_adj']:+.2%} (IBKR shows {R['cum_ibkr']:+.2%})" + (f", SPY {spy['cum']:+.2%}" if spy else ""),
             f"Max drawdown {R['mdd']:.2%}, annualized volatility {R['vol']:.1%}, Sharpe {(R['sharpe'] or 0):.2f}"]
    if prev and prev.get("end") != end:
        lines.append(f"Since last update ({prev['end']}): NAV {m(R['nav_end'] - prev['nav'], True)}, "
                     f"TWR {(1 + R['cum_adj']) / (1 + prev['cum_adj']) - 1:+.2%}, {R['trade_stats'].get('n_exec', 0) - prev.get('n_exec', 0)} new executions")
    elif prev:
        lines.append("(Same data date as last time; no new trading days)")
    last_trades = [t for t in R["closes"] if t["tradeDate"] == R["end_date"].strftime("%Y%m%d")]
    if last_trades:
        lines.append("Closed on the latest day: " + ", ".join(f"{t['u']} {t['pnl']:+,.0f}" for t in last_trades))
    lines.append("Positions: " + (", ".join(f"{p['symbol']} {'long' if p['qty'] > 0 else 'short'} {abs(p['qty']):,.0f} ({p['upnl']:+,.0f})" for p in R["positions"]) or "none"))
    X = R.get("_x", {})
    fl = [x for x in X.get("flags", []) if x["level"] in ("high", "mid")]
    if fl:
        lines.append("Risk flags:")
        lines += [f"  [{'HIGH' if x['level'] == 'high' else 'MEDIUM'}] {x['title']}" for x in fl]
    ch = [c for c in X.get("style_changes", []) if c["big"]]
    if ch:
        month = X["style_rows"][-1]["month"]
        lines.append(f"Trading-style changes ({month} vs. earlier monthly average): " +
                     "; ".join(f"{c['name']} {c['latest']} (was {c['base']}, {c['delta']})" for c in ch))
    if X.get("style_note"):
        lines.append(X["style_note"])
    for e in R["events"]:
        if prev and e["date"].strftime("%Y-%m-%d") > prev["end"] and abs(e["amount"]) >= 100:
            lines.append(f"New event: {e['date']:%Y-%m-%d} {e['kind']} {m(e['amount'], True)}")
    print("\n".join(lines))
    json.dump(dict(end=end, nav=R["nav_end"], cum_adj=R["cum_adj"], n_exec=R["trade_stats"].get("n_exec", 0)),
              open(snap_fp, "w", encoding="utf-8"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the IBKR performance dashboard")
    ap.add_argument("root", nargs="?", default=os.path.dirname(HERE), help="project root (the folder that contains data/)")
    ap.add_argument("--demo", action="store_true", help="demo mode: skip the stale-data and token-expiry checks")
    ap.add_argument("--no-snapshot", action="store_true", help="don't write a monthly snapshot to reports/")
    a = ap.parse_args()
    try:
        print(build(os.path.abspath(a.root), demo=a.demo, snapshot=not a.no_snapshot))
    except FileNotFoundError as e:
        sys.exit(f"{e}\nNo account data yet: run run_pull.bat first, or preview with demo data via demo/generate_demo.py.")

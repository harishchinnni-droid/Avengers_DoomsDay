"""
STEP 13b — Visual HTML dashboard.

The Excel Dashboard sheet is a grid of numbers; useful for lookup, useless for
seeing shape. This writes a standalone HTML file that opens in any browser
with an equity curve, exit-reason breakdown and KPI cards.

No server, no dependencies at runtime -- Chart.js loads from a CDN and the
data is embedded as JSON. Open it by double-clicking.

Colours carry meaning, not decoration: red for loss, green for profit, blue
for neutral magnitude. Dark mode is handled.

ONE FILE FOR THE WHOLE RANGE (02-Aug-26, at his request)
----------------------------------------------------------
Per-date HTML files are gone -- run_TW_ALL.py no longer writes one per
session. This is the only HTML dashboard, built once at the end of a
BACKTEST run, covering whatever date range was tested (his plan: 28-Jul to
25-Aug, one full FNO expiry cycle). A date dropdown switches between the
aggregate view and any single day's view without regenerating anything --
every session's numbers are precomputed once and embedded in the page, and
the dropdown just swaps which precomputed block feeds the charts and tiles.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

import config
import dashboard
import ist_clock

_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
:root{--bg:#faf9f7;--card:#fff;--ink:#0b0b0b;--ink2:#52514e;--ink3:#898781;
--line:#e1e0d9;--red:#d03b3b;--green:#1baf7a;--blue:#2a78d6;--amber:#eda100}
@media(prefers-color-scheme:dark){:root{--bg:#141413;--card:#1a1a19;--ink:#fff;
--ink2:#c3c2b7;--ink3:#898781;--line:#2c2c2a}}
*{box-sizing:border-box}
body{margin:0;padding:28px;background:var(--bg);color:var(--ink);
font:400 15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:22px;font-weight:500;margin:0}
.sub{color:var(--ink2);font-size:13px;margin-top:4px}
.head{display:flex;justify-content:space-between;align-items:flex-start;
flex-wrap:wrap;gap:12px;margin-bottom:24px}
.headctl{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pill{font-size:12px;padding:5px 14px;border-radius:8px}
select#daypick{font:inherit;font-size:13px;padding:7px 12px;border-radius:8px;
border:1px solid var(--line);background:var(--card);color:var(--ink);
cursor:pointer}
.grid{display:grid;gap:12px;margin-bottom:24px}
.k4{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}
.k2{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.card{background:var(--card);border-radius:12px;padding:16px 18px;
border:1px solid var(--line)}
.tile{background:var(--card);border-radius:8px;padding:14px 16px}
.lbl{font-size:13px;color:var(--ink2)}
.big{font-size:30px;font-weight:500;margin-top:4px;letter-spacing:-0.5px}
.mid{font-size:20px;font-weight:500;margin-top:2px}
.hint{font-size:12px;color:var(--ink3);margin-top:2px}
.red{color:var(--red)}.green{color:var(--green)}
.sec{font-size:14px;font-weight:500;margin:0 0 10px}
.chart{position:relative;height:220px}
.chart.tall{height:260px}
.warn{border-radius:8px;padding:14px 18px;margin-bottom:16px;font-size:13px;
line-height:1.65}
.warn b{font-weight:500;display:block;margin-bottom:4px;font-size:14px}
.wr{background:rgba(208,59,59,.10);color:var(--red)}
.wa{background:rgba(237,161,0,.12);color:#8a5f00}
@media(prefers-color-scheme:dark){.wa{color:var(--amber)}}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:500;color:var(--ink2);padding:8px 6px;
border-bottom:1px solid var(--line);font-size:12px}
td{padding:8px 6px;border-bottom:1px solid var(--line)}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.foot{color:var(--ink3);font-size:12px;margin-top:20px;text-align:center}
.hidden{display:none !important}
</style></head><body><div class="wrap">

<div class="head">
  <div><h1>__TITLE__</h1><div class="sub" id="subtitle">__SUBTITLE__</div></div>
  <div class="headctl">
    <select id="daypick" onchange="renderAll(this.value)"></select>
    <div class="pill" id="pill"></div>
  </div>
</div>

<div class="grid k4">
  <div class="tile"><div class="lbl">Net P/L</div>
    <div class="big" id="kNet"></div></div>
  <div class="tile"><div class="lbl">Win rate</div>
    <div class="big" id="kWinRate"></div>
    <div class="hint" id="kBreakeven"></div></div>
  <div class="tile"><div class="lbl">Expectancy</div>
    <div class="big" id="kExpectancy"></div>
    <div class="hint">per trade</div></div>
  <div class="tile"><div class="lbl">Trades</div>
    <div class="big" id="kTrades"></div>
    <div class="hint" id="kWinLoss"></div></div>
</div>

<div id="warnings"></div>

<div class="card" style="margin-bottom:12px">
  <div class="sec" id="eqTitle">Equity curve</div>
  <div class="chart"><canvas id="eq"></canvas></div>
</div>

<div class="grid k2">
  <div class="card"><div class="sec">Net P/L by exit reason</div>
    <div class="chart"><canvas id="ex"></canvas></div></div>
  <div class="card"><div class="sec">Target hit rate</div>
    <div class="chart"><canvas id="tg"></canvas></div></div>
</div>

<div class="grid k2">
  <div class="card"><div class="sec">Net P/L by hour of entry</div>
    <div class="chart"><canvas id="hr"></canvas></div></div>
  <div class="card"><div class="sec">Outcome distribution (R multiples)</div>
    <div class="chart"><canvas id="rm"></canvas></div></div>
</div>

<div class="grid k2">
  <div class="card"><div class="sec">Win rate by exit reason</div>
    <div class="chart"><canvas id="ew"></canvas></div></div>
  <div class="card"><div class="sec">Contribution by symbol</div>
    <table><thead><tr><th>Symbol</th><th style="text-align:right">Trades</th>
    <th style="text-align:right">Net</th></tr></thead>
    <tbody id="symRows"></tbody></table></div>
</div>

<div id="monthOnly">
<div class="grid k2">
  <div class="card"><div class="sec">Net P/L by session</div>
    <div class="chart"><canvas id="dy"></canvas></div></div>
  <div class="card"><div class="sec">Trade quality</div>
    <div class="grid k4" style="margin:0">
      <div class="tile"><div class="lbl">Avg win</div>
        <div class="mid green" id="qAvgWin"></div></div>
      <div class="tile"><div class="lbl">Avg loss</div>
        <div class="mid red" id="qAvgLoss"></div></div>
      <div class="tile"><div class="lbl">Payoff ratio</div>
        <div class="mid" id="qPayoff"></div></div>
      <div class="tile"><div class="lbl">Profit factor</div>
        <div class="mid" id="qProfitFactor"></div></div>
      <div class="tile"><div class="lbl">Max drawdown</div>
        <div class="mid" id="qMaxDD"></div></div>
      <div class="tile"><div class="lbl">Costs</div>
        <div class="mid" id="qCosts"></div></div>
      <div class="tile"><div class="lbl">Net ex-best</div>
        <div class="mid" id="qNetExBest"></div></div>
      <div class="tile"><div class="lbl">Best trade</div>
        <div class="mid" id="qBest"></div>
        <div class="hint" id="qBestSymbol"></div></div>
    </div></div>
</div>

<div class="card"><div class="sec">Sessions</div><table><thead><tr><th>Date</th>
<th style="text-align:right">Trades</th><th style="text-align:right">Win %</th>
<th style="text-align:right">Costs</th><th style="text-align:right">Net</th></tr></thead>
<tbody id="sessionRows"></tbody></table></div>
</div>

<div class="foot">Historical measurement on the sessions tested.
Past performance does not predict future results. Paper trading only.</div>

</div>
<script>
const D=__DATA__;
const DAY_ORDER=__DAYORDER__;
const dk=matchMedia('(prefers-color-scheme:dark)').matches;
const g=dk?'#2c2c2a':'#e1e0d9', m='#898781';
const rup=v=>(v<0?'-':'')+'\\u20b9'+Math.abs(Math.round(v/1000))+'k';
const B={responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}}};
const charts={};

function mkChart(id, cfg){
  if(!document.getElementById(id)) return;
  if(charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), cfg);
}

function renderAll(key){
  const d = D[key];
  if(!d) return;

  document.getElementById('pill').textContent = d.verdict;
  document.getElementById('pill').style.background =
    d.verdictClass === 'green' ? 'rgba(27,175,122,.12)' : 'rgba(208,59,59,.12)';
  document.getElementById('pill').style.color =
    d.verdictClass === 'green' ? '#1baf7a' : '#d03b3b';

  document.getElementById('subtitle').textContent = d.subtitle;

  const kNet = document.getElementById('kNet');
  kNet.textContent = d.netFmt; kNet.className = 'big ' + d.netClass;
  document.getElementById('kWinRate').textContent = d.winRate;
  document.getElementById('kBreakeven').textContent = d.breakevenHint;
  const kExp = document.getElementById('kExpectancy');
  kExp.textContent = d.expectancyFmt; kExp.className = 'big ' + d.expectancyClass;
  document.getElementById('kTrades').textContent = d.trades;
  document.getElementById('kWinLoss').textContent = d.wins + ' won / ' + d.losses + ' lost';

  document.getElementById('warnings').innerHTML = d.warnings.map(w =>
    '<div class="warn ' + w.cls + '"><b>' + w.title + '</b>' + w.body + '</div>').join('');

  document.getElementById('eqTitle').textContent = d.eqTitle;

  mkChart('eq', {type:'line', data:{labels:d.eqLabels, datasets:[{data:d.eqValues,
    borderColor:d.net>=0?'#1baf7a':'#d03b3b',
    backgroundColor:d.net>=0?'rgba(27,175,122,.10)':'rgba(208,59,59,.10)',borderWidth:2,
    fill:true,tension:.2,pointRadius:4,pointBackgroundColor:d.net>=0?'#1baf7a':'#d03b3b',
    pointBorderColor:dk?'#1a1a19':'#fff',pointBorderWidth:2}]},
    options:{...B,scales:{y:{grid:{color:g},border:{display:false},
    ticks:{color:m,font:{size:11},callback:rup}},
    x:{grid:{display:false},border:{color:g},ticks:{color:m,font:{size:11}}}}}});

  mkChart('ex', {type:'bar', data:{labels:d.exLabels, datasets:[{data:d.exValues,
    borderRadius:4,barThickness:20,backgroundColor:d.exValues.map(v=>v>=0?'#1baf7a':'#d03b3b')}]},
    options:{...B,indexAxis:'y',scales:{x:{grid:{color:g},border:{display:false},
    ticks:{color:m,font:{size:11},callback:rup}},
    y:{grid:{display:false},border:{display:false},ticks:{color:m,font:{size:11}}}}}});

  mkChart('tg', {type:'bar', data:{labels:['T1','T2','T3'], datasets:[{data:d.targets,
    borderRadius:4,barThickness:44,backgroundColor:['#2a78d6','#85b7eb','#d3d1c7']}]},
    options:{...B,scales:{y:{grid:{color:g},border:{display:false},max:100,
    ticks:{color:m,font:{size:11},callback:v=>v+'%'}},
    x:{grid:{display:false},border:{color:g},ticks:{color:m,font:{size:11}}}}}});

  mkChart('hr', {type:'bar', data:{labels:d.hourLabels, datasets:[{data:d.hourNet,
    borderRadius:4,backgroundColor:d.hourNet.map(v=>v>=0?'#1baf7a':'#d03b3b')}]},
    options:{...B,plugins:{legend:{display:false},tooltip:{callbacks:{
    afterLabel:c=>'win rate '+d.hourWin[c.dataIndex]+'%'}}},
    scales:{y:{grid:{color:g},border:{display:false},
    ticks:{color:m,font:{size:11},callback:rup}},
    x:{grid:{display:false},border:{color:g},ticks:{color:m,font:{size:11}}}}}});

  mkChart('rm', {type:'bar', data:{labels:d.rLabels, datasets:[{data:d.rValues,
    borderRadius:4,backgroundColor:['#d03b3b','#e34948','#85b7eb','#2a78d6','#1baf7a']}]},
    options:{...B,scales:{y:{grid:{color:g},border:{display:false},
    ticks:{color:m,font:{size:11},precision:0}},
    x:{grid:{display:false},border:{color:g},ticks:{color:m,font:{size:11}}}}}});

  mkChart('ew', {type:'bar', data:{labels:d.exLabels, datasets:[{data:d.exWin,
    borderRadius:4,barThickness:20,
    backgroundColor:d.exWin.map(v=>v>=50?'#1baf7a':v>=25?'#eda100':'#d03b3b')}]},
    options:{...B,indexAxis:'y',scales:{x:{grid:{color:g},border:{display:false},max:100,
    ticks:{color:m,font:{size:11},callback:v=>v+'%'}},
    y:{grid:{display:false},border:{display:false},ticks:{color:m,font:{size:11}}}}}});

  document.getElementById('symRows').innerHTML = d.symRows.map(r =>
    '<tr><td>' + r[0] + '</td><td class="n">' + r[1] + '</td><td class="n ' +
    (r[2] >= 0 ? 'green' : 'red') + '">' + r[3] + '</td></tr>').join('');

  const monthOnly = document.getElementById('monthOnly');
  if(d.dayLabels && d.dayLabels.length){
    monthOnly.classList.remove('hidden');
    mkChart('dy', {type:'bar', data:{labels:d.dayLabels, datasets:[{data:d.dayValues,
      borderRadius:4,backgroundColor:d.dayValues.map(v=>v>=0?'#1baf7a':'#d03b3b')}]},
      options:{...B,scales:{y:{grid:{color:g},border:{display:false},
      ticks:{color:m,font:{size:11},callback:rup}},
      x:{grid:{display:false},border:{color:g},ticks:{color:m,font:{size:11}}}}}});
    document.getElementById('qAvgWin').textContent = d.avgWinFmt;
    document.getElementById('qAvgLoss').textContent = d.avgLossFmt;
    document.getElementById('qPayoff').textContent = d.payoff;
    document.getElementById('qProfitFactor').textContent = d.profitFactor;
    document.getElementById('qMaxDD').textContent = d.maxDrawdownFmt;
    document.getElementById('qCosts').textContent = d.costsFmt;
    const qNetExBest = document.getElementById('qNetExBest');
    qNetExBest.textContent = d.netExBestFmt;
    qNetExBest.className = 'mid ' + d.netExBestClass;
    document.getElementById('qBest').textContent = d.bestFmt;
    document.getElementById('qBestSymbol').textContent = d.bestSymbol;
    document.getElementById('sessionRows').innerHTML = d.sessionRows.map(r =>
      '<tr><td>' + r[0] + '</td><td class="n">' + r[1] + '</td><td class="n">' + r[2] +
      '%</td><td class="n">' + r[3] + '</td><td class="n ' + (r[5] >= 0 ? 'green' : 'red') +
      '">' + r[4] + '</td></tr>').join('');
  } else {
    monthOnly.classList.add('hidden');
    if(charts['dy']){ charts['dy'].destroy(); delete charts['dy']; }
  }
}

const picker = document.getElementById('daypick');
DAY_ORDER.forEach(k => {
  const opt = document.createElement('option');
  opt.value = k; opt.textContent = (k === 'ALL' ? 'All sessions' : k);
  picker.appendChild(opt);
});
renderAll('ALL');
</script></body></html>"""


def _fmt(v: float) -> str:
    """Indian numbering, e.g. -1,39,252."""
    neg = v < 0
    s = f"{abs(v):,.0f}"
    parts = s.replace(",", "")
    if len(parts) > 3:
        head, tail = parts[:-3], parts[-3:]
        chunks = []
        while len(head) > 2:
            chunks.insert(0, head[-2:])
            head = head[:-2]
        if head:
            chunks.insert(0, head)
        s = ",".join(chunks) + "," + tail
    return ("\u2212" if neg else "") + "\u20b9" + s


def _compute_dataset(orders: pd.DataFrame, subtitle: str,
                     daily: pd.DataFrame | None = None) -> dict:
    """
    Every number one key of the date picker needs, in one dict. Called once
    for 'ALL' (the whole range, with `daily` supplied for the session-level
    chart and table) and once per individual date (daily=None -- a single
    day's equity curve is trade-by-trade, not session-by-session, since one
    point on a day-granularity chart says nothing).
    """
    s = dashboard.compute_stats(orders, "OVERALL")

    ex = {}
    if not orders.empty and "Exit Reason" in orders.columns:
        nets = pd.to_numeric(orders["Net P/L (Rs)"], errors="coerce").fillna(0)
        for reason, val in zip(orders["Exit Reason"].astype(str), nets):
            key = reason.split("(")[0].strip()[:22] or "open"
            ex[key] = ex.get(key, 0.0) + float(val)
    ex_sorted = sorted(ex.items(), key=lambda kv: kv[1])

    n = max(len(orders), 1)
    targets = []
    for col in ("T1 Hit", "T2 Hit", "T3 Hit"):
        hit = 0
        if col in orders.columns:
            hit = int((orders[col].astype(str).str.strip().str.upper() == "YES").sum())
        targets.append(round(hit / n * 100, 1))

    hourly = {}
    if not orders.empty and "Entry Time" in orders.columns:
        nets_h = pd.to_numeric(orders["Net P/L (Rs)"], errors="coerce").fillna(0)
        for t, val in zip(orders["Entry Time"].astype(str), nets_h):
            hh = t[:2]
            if not hh.isdigit():
                continue
            b = hourly.setdefault(f"{hh}:00", [0, 0, 0.0])
            b[0] += 1
            b[1] += 1 if val > 0 else 0
            b[2] += float(val)
    hour_labels = sorted(hourly)
    hour_net = [round(hourly[k][2], 2) for k in hour_labels]
    hour_winrate = [round(hourly[k][1] / hourly[k][0] * 100, 1) for k in hour_labels]

    ex_win = []
    for k, _ in ex_sorted:
        rows = orders[orders["Exit Reason"].astype(str).str.startswith(k)] \
            if not orders.empty else orders
        if len(rows):
            w = (pd.to_numeric(rows["Net P/L (Rs)"], errors="coerce").fillna(0) > 0).sum()
            ex_win.append(round(w / len(rows) * 100, 1))
        else:
            ex_win.append(0.0)

    r_buckets = {"< -1R": 0, "-1R..0": 0, "0..1R": 0, "1..2R": 0, "> 2R": 0}
    if not orders.empty and "Risk Amount (Rs)" in orders.columns:
        risks = pd.to_numeric(orders["Risk Amount (Rs)"], errors="coerce")
        nets_r = pd.to_numeric(orders["Net P/L (Rs)"], errors="coerce").fillna(0)
        for risk, val in zip(risks, nets_r):
            if not risk or pd.isna(risk) or risk <= 0:
                continue
            r = val / risk
            key = ("< -1R" if r < -1 else "-1R..0" if r < 0
                   else "0..1R" if r < 1 else "1..2R" if r < 2 else "> 2R")
            r_buckets[key] += 1

    sym_rows = []
    if not orders.empty:
        g = orders.copy()
        g["_net"] = pd.to_numeric(g["Net P/L (Rs)"], errors="coerce").fillna(0)
        agg = g.groupby("Symbol")["_net"].agg(["count", "sum"]).sort_values("sum")
        for sym, row in agg.iterrows():
            sym_rows.append([str(sym), int(row["count"]), round(float(row["sum"]), 2),
                             _fmt(float(row["sum"]))])

    payoff = (s.avg_win / s.avg_loss) if s.avg_loss else 0.0
    breakeven = (s.avg_loss / (s.avg_win + s.avg_loss) * 100
                 if (s.avg_win + s.avg_loss) else 0.0)

    warns = []
    if s.net > 0 and s.net_ex_best < 0:
        warns.append({"cls": "wr", "title": "All profit came from one trade",
                      "body": f"Net is {_fmt(s.net)} but excluding {s.best_symbol} it is "
                              f"{_fmt(s.net_ex_best)}. One trade is not an edge."})
    if s.trades < 100:
        warns.append({"cls": "wa", "title": f"{s.trades} trades is too small a sample",
                      "body": "Below 100 trades variance dominates. Do not tune on this."})
    if s.cost_share_of_gross > 40:
        warns.append({"cls": "wa", "title": f"Costs ate {s.cost_share_of_gross:.0f}% of gross profit",
                      "body": f"{_fmt(s.costs)} of {_fmt(s.gross)}. Fewer, better trades."})
    if breakeven and s.win_rate < breakeven:
        warns.append({"cls": "wr", "title": "Win rate is below breakeven for this payoff",
                      "body": f"At a payoff ratio of {payoff:.2f} you need "
                              f"{breakeven:.0f}% wins. You have {s.win_rate:.0f}%."})

    if daily is not None and not daily.empty:
        day_labels = daily["Date"].astype(str).tolist()
        day_values = [float(x) for x in daily["Net (Rs)"]]
        eq_labels = ["Start"] + day_labels
        eq_values = [0.0] + [float(x) for x in daily["Cumulative (Rs)"]]
        eq_title = "Equity curve (session by session)"
        session_rows = [
            [str(r["Date"]), int(r["Trades"]), round(float(r["Win Rate %"]), 1),
             _fmt(float(r["Costs (Rs)"])), _fmt(float(r["Net (Rs)"])), float(r["Net (Rs)"])]
            for _, r in daily.iterrows()
        ]
    else:
        curve = dashboard.equity_curve_table(orders)
        eq_labels = ["Start"] + [f"{ex_time} {sym}" for ex_time, sym, *_ in curve]
        eq_values = [0.0] + [float(c[3]) for c in curve]
        eq_title = "Equity curve (trade by trade)"
        day_labels, day_values, session_rows = [], [], []

    return {
        "subtitle": subtitle,
        "net": s.net, "netFmt": _fmt(s.net), "netClass": "green" if s.net >= 0 else "red",
        "winRate": f"{s.win_rate:.1f}%", "breakevenHint": f"need {breakeven:.0f}% at this payoff",
        "expectancyFmt": _fmt(s.expectancy),
        "expectancyClass": "green" if s.expectancy >= 0 else "red",
        "trades": s.trades, "wins": s.wins, "losses": s.losses,
        "verdict": "Profitable" if s.net > 0 else "No edge",
        "verdictClass": "green" if s.net > 0 else "red",
        "eqTitle": eq_title, "eqLabels": eq_labels, "eqValues": eq_values,
        "exLabels": [k for k, _ in ex_sorted], "exValues": [v for _, v in ex_sorted],
        "exWin": ex_win, "targets": targets,
        "hourLabels": hour_labels, "hourNet": hour_net, "hourWin": hour_winrate,
        "rLabels": list(r_buckets), "rValues": list(r_buckets.values()),
        "symRows": sym_rows,
        "warnings": warns,
        "dayLabels": day_labels, "dayValues": day_values, "sessionRows": session_rows,
        "avgWinFmt": _fmt(s.avg_win), "avgLossFmt": _fmt(s.avg_loss),
        "payoff": f"{payoff:.2f}", "profitFactor": f"{s.profit_factor:.2f}",
        "maxDrawdownFmt": _fmt(s.max_drawdown), "costsFmt": _fmt(s.costs),
        "netExBestFmt": _fmt(s.net_ex_best),
        "netExBestClass": "green" if s.net_ex_best >= 0 else "red",
        "bestFmt": _fmt(s.best), "bestSymbol": s.best_symbol,
    }


def build_html(orders: pd.DataFrame, daily: pd.DataFrame | None,
               title: str, subtitle: str,
               sessions: list[tuple[str, pd.DataFrame]] | None = None) -> str:
    """
    `sessions` is [(date_label, that_date's_orders_df), ...] -- when given,
    the page gets a date dropdown alongside the 'ALL' aggregate. When not
    given (or only one session exists), the dropdown still renders with just
    'All sessions' so the page behaves exactly like the old single-view
    dashboard.
    """
    datasets = {"ALL": _compute_dataset(orders, subtitle, daily=daily)}
    day_order = ["ALL"]
    for label, day_orders in (sessions or []):
        day_subtitle = f"{label} · single session"
        datasets[label] = _compute_dataset(day_orders, day_subtitle, daily=None)
        day_order.append(label)

    return (_TEMPLATE.replace("__TITLE__", title)
            .replace("__SUBTITLE__", subtitle)
            .replace("__DATA__", json.dumps(datasets))
            .replace("__DAYORDER__", json.dumps(day_order)))


def write_dashboard(orders: pd.DataFrame, path: Path,
                    daily: pd.DataFrame | None = None,
                    title: str = "F&O dashboard",
                    subtitle: str = "",
                    sessions: list[tuple[str, pd.DataFrame]] | None = None) -> Path:
    subtitle = subtitle or f"Built {ist_clock.now_ist():%d-%b-%y %H:%M} IST"
    path.write_text(build_html(orders, daily, title, subtitle, sessions=sessions),
                    encoding="utf-8")
    print(f"[dashboard] visual dashboard -> {path.name}"
          f" ({len(sessions) if sessions else 0} day(s) selectable)")
    return path


if __name__ == "__main__":
    import paths
    real = [("HDFCLIFE", -102.07, "PAPER"), ("BEL", 104.18, "PAPER"),
            ("MARUTI", 1039.03, "PAPER"), ("BAJFINANCE", 3352.55, "PAPER"),
            ("RELIANCE", -395.52, "LIVE"), ("INDUSINDBK", -1038.33, "PAPER")]
    df = pd.DataFrame([{
        "Symbol": s, "Net P/L (Rs)": n, "Gross P/L (Rs)": n + 200,
        "Costs (Rs)": 200, "Capital Required (Rs)": 15000, "Trade Mode": m,
        "Exit Reason": "Target 1 Hit" if n > 0 else "Stop Loss Hit",
        "Exit Time": "10:00:00", "Entry Time": "09:45:00",
        "T1 Hit": "YES" if n > 0 else "", "T2 Hit": "", "T3 Hit": "",
    } for s, n, m in real])
    daily = pd.DataFrame([
        {"Date": "28-Jul-26", "Trades": 3, "Win Rate %": 33.3, "Costs (Rs)": 600,
         "Net (Rs)": 641.11, "Cumulative (Rs)": 641.11},
        {"Date": "29-Jul-26", "Trades": 3, "Win Rate %": 66.7, "Costs (Rs)": 600,
         "Net (Rs)": 2318.85, "Cumulative (Rs)": 2959.96},
    ])
    sessions = [("28-Jul-26", df.iloc[:3]), ("29-Jul-26", df.iloc[3:])]

    out = write_dashboard(df, Path("/tmp/dashboard_selftest.html"), daily,
                          title="F&O backtest (self-test)",
                          subtitle="28-Jul-26 to 29-Jul-26 · 2 sessions",
                          sessions=sessions)
    html = out.read_text(encoding="utf-8")
    assert "daypick" in html and "28-Jul-26" in html and "29-Jul-26" in html
    assert '"ALL"' in html
    print(f"self-test dashboard written to {out}, {len(html):,} bytes")
    print("dashboard_html self-check passed")

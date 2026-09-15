"""
STEP 13 — Dashboard. Is this system making money or losing it?

Split LIVE and PAPER, and never blend them. On 31-Jul-26 the blended figure
read +Rs 1,476 while the LIVE leg alone was -Rs 2,483 across five trades and
the PAPER leg was +Rs 3,960. One number hid the only result that involved
real money.

THE METRIC THAT MATTERS MOST
----------------------------
Net P/L excluding the single best trade. On that session one trade
(BAJFINANCE, +Rs 3,353) carried everything -- the other thirteen together
netted -Rs 1,876. A system whose profit lives in one trade has not shown an
edge yet, and the headline P/L will not tell you that. This dashboard does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

import config
import ist_clock

# --------------------------------------------------------------------------
# STYLED EXCEL SHEET -- ported from F:\06_Claude_v2's actual dashboard.py
# (02-Aug-26, at his request: "use the attached dashboard.py ... color
# combination and everything"). Two layers, exactly as their file names
# them: DATA (per_symbol_table/exit_reason_table/equity_curve_table/
# hourly_breakdown_table below, and write_dashboard_sheet's plain ws.append
# rows) writes VALUES ONLY, no fill/font. STYLE (style_dashboard_sheet,
# further down) scans the sheet's own text afterwards to find section
# headers and KPI labels, and colours purely off what it finds -- it never
# touches a cell's value, so it can be re-run on its own without
# accumulating formatting. The LIVE/PAPER split is ours, not theirs, and is
# kept -- blending real and simulated P/L is how a -Rs 2,483 LIVE result
# once hid inside a +Rs 1,476 headline number.
# --------------------------------------------------------------------------


@dataclass
class Stats:
    label: str
    trades: int = 0
    closed: int = 0
    open_now: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    gross: float = 0.0
    costs: float = 0.0
    net: float = 0.0
    capital_deployed: float = 0.0
    best: float = 0.0
    best_symbol: str = "N/A"
    worst: float = 0.0
    worst_symbol: str = "N/A"
    max_drawdown: float = 0.0
    net_ex_best: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.closed * 100 if self.closed else 0.0

    @property
    def avg_win(self) -> float:
        return self._sum_wins / self.wins if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return abs(self._sum_losses) / self.losses if self.losses else 0.0

    @property
    def profit_factor(self) -> float:
        return self._sum_wins / abs(self._sum_losses) if self._sum_losses else 0.0

    @property
    def expectancy(self) -> float:
        return self.net / self.closed if self.closed else 0.0

    @property
    def cost_share_of_gross(self) -> float:
        return self.costs / self.gross * 100 if self.gross > 0 else 0.0

    _sum_wins: float = 0.0
    _sum_losses: float = 0.0


def compute_stats(orders: pd.DataFrame, label: str) -> Stats:
    s = Stats(label=label)
    if orders.empty:
        return s

    net_col = "Net P/L (Rs)"
    s.trades = len(orders)
    closed = orders[orders["Exit Reason"].astype(str).str.strip() != ""]
    s.closed = len(closed)
    s.open_now = s.trades - s.closed

    s.gross = float(pd.to_numeric(orders["Gross P/L (Rs)"], errors="coerce").fillna(0).sum())
    s.costs = float(pd.to_numeric(orders["Costs (Rs)"], errors="coerce").fillna(0).sum())
    s.net = float(pd.to_numeric(orders[net_col], errors="coerce").fillna(0).sum())
    s.capital_deployed = float(
        pd.to_numeric(orders["Capital Required (Rs)"], errors="coerce").fillna(0).sum()
    )

    nets = pd.to_numeric(closed[net_col], errors="coerce").fillna(0)
    s.wins = int((nets > 0).sum())
    s.losses = int((nets < 0).sum())
    s.breakeven = int((nets == 0).sum())
    s._sum_wins = float(nets[nets > 0].sum())
    s._sum_losses = float(nets[nets < 0].sum())

    if len(nets):
        best_i, worst_i = nets.idxmax(), nets.idxmin()
        s.best = float(nets.max())
        s.worst = float(nets.min())
        s.best_symbol = str(closed.loc[best_i, "Symbol"])
        s.worst_symbol = str(closed.loc[worst_i, "Symbol"])
        s.net_ex_best = float(nets.sum() - nets.max())

        equity = nets.cumsum()
        s.max_drawdown = float((equity.cummax() - equity).max())

    return s


def _panel(s: Stats) -> list[list]:
    return [
        [f"{s.label} -- TRADE SUMMARY", "", "", f"{s.label} -- P/L OVERVIEW", ""],
        ["Total Trades Taken", s.trades, "", "Total Capital Deployed (Rs)", round(s.capital_deployed, 2)],
        ["Trades Closed", s.closed, "", "Gross P/L (Rs)", round(s.gross, 2)],
        ["Trades Open", s.open_now, "", "Total Costs (Rs)", round(s.costs, 2)],
        ["Profitable Trades", s.wins, "", "Total Net P/L (Rs)", round(s.net, 2)],
        ["Loss Trades", s.losses, "", "Costs as % of Gross", f"{s.cost_share_of_gross:.1f}%"],
        ["Breakeven Trades", s.breakeven, "", "Max Single Profit (Rs)", round(s.best, 2)],
        ["Win Rate", f"{s.win_rate:.1f}%", "", "Best Trade Symbol", s.best_symbol],
        ["Profit Factor", f"{s.profit_factor:.2f}", "", "Max Single Loss (Rs)", round(s.worst, 2)],
        ["Avg Profit per Win (Rs)", round(s.avg_win, 2), "", "Worst Trade Symbol", s.worst_symbol],
        ["Avg Loss per Loss (Rs)", round(s.avg_loss, 2), "", "Max Drawdown (Rs)", round(s.max_drawdown, 2)],
        ["Expectancy per Trade (Rs)", round(s.expectancy, 2), "",
         "NET EXCLUDING BEST TRADE (Rs)", round(s.net_ex_best, 2)],
        ["", "", "", "", ""],
    ]


def build_dashboard(orders: pd.DataFrame, trade_date: date,
                    mode: str) -> pd.DataFrame:
    """Assemble the Dashboard sheet as a plain grid."""
    rows: list[list] = [
        [f"F&O TRADING DASHBOARD -- {trade_date:%d %b %Y}", "", "", "", ""],
        [f"Mode: {mode}   |   Built {ist_clock.now_ist():%H:%M:%S} IST", "", "", "", ""],
        ["Rebuilt every cycle. Figures are provisional until the session closes.",
         "", "", "", ""],
        ["", "", "", "", ""],
    ]

    overall = compute_stats(orders, "OVERALL")
    rows += _panel(overall)

    for mode_name in ("LIVE", "PAPER"):
        subset = orders[orders["Trade Mode"].astype(str).str.upper() == mode_name] \
            if not orders.empty else orders
        note = ("real orders placed with Angel One" if mode_name == "LIVE"
                else "simulated only -- no broker order")
        rows.append([f"{mode_name} TRADING ({note})", "", "", "", ""])
        rows += _panel(compute_stats(subset, mode_name))

    # The honesty check, spelled out rather than left for the reader to spot.
    if overall.closed >= 2:
        rows.append(["CONCENTRATION CHECK", "", "", "", ""])
        share = (overall.best / overall.net * 100) if overall.net > 0 else 0.0
        rows.append(["Net P/L (Rs)", round(overall.net, 2), "",
                     "Net excluding best trade (Rs)", round(overall.net_ex_best, 2)])
        rows.append(["Best trade as % of net", f"{share:.0f}%", "", "", ""])
        if overall.net > 0 and overall.net_ex_best < 0:
            rows.append([
                "WARNING: every rupee of profit came from one trade. The other "
                f"{overall.closed - 1} trades netted Rs {overall.net_ex_best:,.0f}.",
                "", "", "", ""])
        rows.append(["", "", "", "", ""])

    if overall.gross > 0 and overall.cost_share_of_gross > 40:
        rows.append([
            f"WARNING: costs consumed {overall.cost_share_of_gross:.0f}% of gross "
            f"profit (Rs {overall.costs:,.0f} of Rs {overall.gross:,.0f}).",
            "", "", "", ""])

    return pd.DataFrame(rows, columns=["Metric", "Value", "", "Metric ", "Value "])


# --------------------------------------------------------------------------
# table computations for the styled sheet, single-date scope
# --------------------------------------------------------------------------
def per_symbol_table(orders: pd.DataFrame) -> list[tuple]:
    """(symbol, trades, wins, losses, total P/L, avg P/L), alphabetical."""
    if orders.empty:
        return []
    nets = pd.to_numeric(orders["Net P/L (Rs)"], errors="coerce").fillna(0)
    g = orders.assign(_net=nets)
    out = []
    for sym, rows in g.groupby("Symbol"):
        n = rows["_net"]
        out.append((str(sym), len(rows), int((n > 0).sum()), int((n < 0).sum()),
                    round(float(n.sum()), 2), round(float(n.mean()), 2)))
    return sorted(out, key=lambda r: r[0])


def exit_reason_table(orders: pd.DataFrame) -> list[tuple]:
    """(reason, count, total P/L), most frequent reason first."""
    if orders.empty or "Exit Reason" not in orders.columns:
        return []
    nets = pd.to_numeric(orders["Net P/L (Rs)"], errors="coerce").fillna(0)
    g = orders.assign(_net=nets)
    out = []
    for reason, rows in g.groupby(g["Exit Reason"].astype(str)):
        if not reason.strip():
            continue
        out.append((reason, len(rows), round(float(rows["_net"].sum()), 2)))
    return sorted(out, key=lambda r: r[1], reverse=True)


def equity_curve_table(orders: pd.DataFrame) -> list[tuple]:
    """
    (entry time, exit time, symbol, trade P/L, cumulative P/L,
    capital required, pct), chronological by exit, resolved trades only.

    Entry Time added (06-Aug-26, Harish's request) so the curve shows both
    ends of each trade -- when the capital went in, when it came back --
    which is what the rotation analysis below needs to be readable on its
    own without cross-referencing Orders.
    """
    if orders.empty or "Exit Time" not in orders.columns:
        return []
    closed = orders[orders["Exit Reason"].astype(str).str.strip() != ""].copy()
    if closed.empty:
        return []
    closed["_net"] = pd.to_numeric(closed["Net P/L (Rs)"], errors="coerce").fillna(0)
    closed["_cap"] = pd.to_numeric(
        closed.get("Capital Required (Rs)", 0), errors="coerce").fillna(0)
    closed = closed.sort_values("Exit Time")
    cum = 0.0
    out = []
    for _, r in closed.iterrows():
        cum += r["_net"]
        pct = (r["_net"] / r["_cap"] * 100) if r["_cap"] else 0.0
        out.append((str(r.get("Entry Time", "")), str(r["Exit Time"]),
                    str(r["Symbol"]), round(r["_net"], 2),
                    round(cum, 2), round(r["_cap"], 2), f"{pct:.1f}%"))
    return out


def capital_rotation_table(orders: pd.DataFrame) -> tuple[list[tuple], float, float]:
    """
    Answers "how much money did I actually need today, if I reuse the same
    pool as trades close" -- as opposed to "how much would I need if every
    trade had to be funded separately."

    Walks every entry (+capital) and exit (-capital) in true chronological
    order (a sweep line), and tracks capital tied up at each instant. The
    running peak is the smallest pool that could have funded the whole day
    on a rotate-as-you-go basis. Compared against the flat sum of every
    trade's capital requirement (what you'd need with zero reuse), the gap
    is what rotation saved him.

    At same-instant ties, entries are applied before exits -- the
    conservative read: assume the next entry needs its money before the
    prior trade's money is confirmed free, rather than assuming a same-tick
    handoff that may not be real. A trade still open at day-end (no Exit
    Time/Reason) never gets a release event, so its capital correctly
    stays "in use" through the last event of the day.

    Returns (rows, peak_concurrent_capital, total_capital_no_reuse).
    """
    if orders.empty or "Entry Time" not in orders.columns:
        return [], 0.0, 0.0
    df = orders.copy()
    df["_cap"] = pd.to_numeric(
        df.get("Capital Required (Rs)", 0), errors="coerce").fillna(0)

    events = []  # (time_str, tie_break, delta, symbol, kind)
    total_no_reuse = 0.0
    for _, r in df.iterrows():
        et = str(r.get("Entry Time", "")).strip()
        if not et:
            continue
        cap = float(r["_cap"])
        total_no_reuse += cap
        sym = str(r.get("Symbol", ""))
        events.append((et, 0, cap, sym, "ENTRY"))
        xt = str(r.get("Exit Time", "")).strip()
        if xt and str(r.get("Exit Reason", "")).strip():
            events.append((xt, 1, -cap, sym, "EXIT"))

    events.sort(key=lambda e: (e[0], e[1]))
    running = 0.0
    peak = 0.0
    out = []
    for t, _, delta, sym, kind in events:
        running += delta
        peak = max(peak, running)
        out.append((t, sym, kind, round(delta, 2), round(running, 2)))
    return out, round(peak, 2), round(total_no_reuse, 2)


def hourly_breakdown_table(orders: pd.DataFrame) -> list[tuple]:
    """(hour label, trades, wins, total P/L), chronological."""
    if orders.empty or "Entry Time" not in orders.columns:
        return []
    nets = pd.to_numeric(orders["Net P/L (Rs)"], errors="coerce").fillna(0)
    buckets: dict[str, list] = {}
    for t, val in zip(orders["Entry Time"].astype(str), nets):
        hh = t[:2]
        if not hh.isdigit():
            continue
        b = buckets.setdefault(f"{hh}:00", [0, 0, 0.0])
        b[0] += 1
        b[1] += 1 if val > 0 else 0
        b[2] += float(val)
    return [(k, v[0], v[1], round(v[2], 2)) for k, v in sorted(buckets.items())]


# --------------------------------------------------------------------------
# DATA layer, part 2: plain-value writer. No fill/font decided here -- that
# is style_dashboard_sheet()'s job alone, exactly like the file this was
# ported from. Keeping the two apart means style_dashboard_sheet can be
# re-run on its own (e.g. after a manual edit) and it always re-derives
# colour from whatever text is currently in the cells, never accumulating.
# --------------------------------------------------------------------------
def _append_kpi_panel(ws, left_title: str, right_title: str, s: Stats) -> None:
    """One 2-column KPI block: label|value, label|value. Plain values only."""
    ws.append([])
    ws.append([None, left_title, None, None, right_title])

    ws.append([None, "Total Trades Taken", s.trades, None,
               "Total Capital Deployed (Rs)", round(s.capital_deployed, 2)])
    capital_row = ws.max_row

    ws.append([None, "Trades Closed", s.closed, None,
               "Total Net P/L (Rs)", round(s.net, 2)])
    pl_row = ws.max_row
    # Live formula, not a baked-in number, so it stays correct if either
    # input is edited by hand. IFERROR guards the zero-capital case.
    pct_cell = ws.cell(row=pl_row, column=7)
    pct_cell.value = f'=IFERROR(F{pl_row}/F{capital_row},"")'
    pct_cell.number_format = "0.00%"

    ws.append([None, "Trades Open", s.open_now, None,
               "Max Single Profit (Rs)", round(s.best, 2)])
    ws.append([None, "Profitable Trades", s.wins, None,
               "Best Trade Symbol", s.best_symbol])
    ws.append([None, "Loss Trades", s.losses, None,
               "Max Single Loss (Rs)", round(s.worst, 2)])
    ws.append([None, "Breakeven Trades", s.breakeven, None,
               "Worst Trade Symbol", s.worst_symbol])
    ws.append([None, "Win Rate", f"{s.win_rate:.1f}%", None,
               "Avg Profit per Win (Rs)", round(s.avg_win, 2)])
    ws.append([None, "Profit Factor", f"{s.profit_factor:.2f}", None,
               "Avg Loss per Loss (Rs)", round(s.avg_loss, 2)])
    ws.append([None, None, None, None,
               "Expectancy per Trade (Rs)", round(s.expectancy, 2)])
    ws.append([None, None, None, None,
               "Max Drawdown (Rs)", round(s.max_drawdown, 2)])


def write_dashboard_sheet(workbook_path, orders: pd.DataFrame,
                          trade_date: date, mode: str) -> None:
    """
    Build the Dashboard sheet: write every value plain (no styling), then
    hand the finished sheet to style_dashboard_sheet() to colour it. Native
    Excel charts were added 15-Aug-26 and removed again 16-Aug-26 at
    Harish's request ("not adding any value"). The LIVE/PAPER split is
    ours, not the ported file's -- blending real and simulated P/L is how a
    -Rs 2,483 LIVE result once hid inside a +Rs 1,476 headline number.
    """
    import openpyxl

    wb = openpyxl.load_workbook(workbook_path)
    if "Dashboard" in wb.sheetnames:
        del wb["Dashboard"]
    ws = wb.create_sheet("Dashboard", 0)

    overall = compute_stats(orders, "OVERALL")
    ws.append([None, f"F&O TRADING DASHBOARD  --  {trade_date:%d %b %Y}"])
    ws.append([None, f"Status: {mode} -- {overall.trades} trade(s), "
                     f"{overall.closed} closed"])

    _append_kpi_panel(ws, "TRADE SUMMARY", "P/L OVERVIEW", overall)

    live = orders[orders["Trade Mode"].astype(str).str.upper() == "LIVE"] \
        if not orders.empty else orders
    paper = orders[orders["Trade Mode"].astype(str).str.upper() == "PAPER"] \
        if not orders.empty else orders
    live_stats = compute_stats(live, "LIVE")
    paper_stats = compute_stats(paper, "PAPER")

    import openpyxl.styles as _styles

    ws.append([])
    ws.append([None, "LIVE TRADING (real orders placed with Angel One)"])
    ws.cell(row=ws.max_row, column=2).font = _styles.Font(bold=True)
    if live.empty:
        ws.append([None, "No LIVE orders were placed in this session."])
        ws.cell(row=ws.max_row, column=2).font = _styles.Font(italic=True, color="808080")
    else:
        ws.append([None, f"{len(live)} LIVE order(s) placed -- see the panel below."])
        _append_kpi_panel(ws, "LIVE -- TRADE SUMMARY", "LIVE -- P/L OVERVIEW", live_stats)

    ws.append([])
    ws.append([None, "PAPER TRADING (simulated only -- no broker order)"])
    ws.cell(row=ws.max_row, column=2).font = _styles.Font(bold=True)
    _append_kpi_panel(ws, "PAPER -- TRADE SUMMARY", "PAPER -- P/L OVERVIEW", paper_stats)

    ws.append([])
    footnote = (
        f"Historical backtest result only -- not a guarantee of future "
        f"performance. Sample size: {overall.closed} resolved trade(s)."
    )
    if overall.closed < 30:
        footnote += (" Below the ~30-trade threshold generally treated as "
                     "statistically meaningful -- read every ratio above as "
                     "provisional.")
    ws.append([None, footnote])
    ws.cell(row=ws.max_row, column=2).font = _styles.Font(italic=True, color="808080")

    # "TRADE OUTCOME" and "LIVE VS PAPER -- NET P/L" tables removed
    # 12-Sep-26 at Harish's request ("not adding any values") -- they were
    # leftover chart-source tables from the native Excel charts added
    # 15-Aug-26 and removed 16-Aug-26; the charts never came back, and every
    # number in these two tables is already shown in the KPI panels above
    # (Profitable/Loss/Breakeven Trades in the overall panel, Total Net P/L
    # in the separate LIVE/PAPER panels), so they were pure duplication.
    ws.append([])
    ws.append([None, "PER-SYMBOL PERFORMANCE"])
    ws.append([None, "Symbol", "Trades", "Wins", "Losses",
               "Total P/L (Rs)", "Avg P/L (Rs)", "Positive P/L (Rs)", "Negative P/L (Rs)"])
    for sym, trades, wins, losses, total, avg in per_symbol_table(orders):
        ws.append([None, sym, trades, wins, losses, total, avg,
                   total if total > 0 else 0, total if total < 0 else 0])

    ws.append([])
    ws.append([None, "EXIT REASON DISTRIBUTION"])
    ws.append([None, "Exit Reason", "Count", "Total P/L (Rs)"])
    for reason, count, total in exit_reason_table(orders):
        ws.append([None, reason, count, total])

    ws.append([])
    ws.append([None, "EQUITY CURVE (chronological, resolved trades only)"])
    ws.append([None, "Entry Time", "Exit Time", "Symbol", "Trade P/L (Rs)",
               "Cumulative P/L (Rs)", "Capital Required (Rs)", "Profit Per Trade"])
    for en_time, ex_time, sym, trade_pl, cum_pl, cap, pct in equity_curve_table(orders):
        ws.append([None, en_time, ex_time, sym, trade_pl, cum_pl, cap, pct])

    ws.append([])
    ws.append([None, "HOURLY P&L BREAKDOWN"])
    ws.append([None, "Hour", "Trades", "Wins", "Total P/L (Rs)",
               "Positive P/L (Rs)", "Negative P/L (Rs)"])
    for hour, trades, wins, total in hourly_breakdown_table(orders):
        ws.append([None, hour, trades, wins, total,
                   total if total > 0 else 0, total if total < 0 else 0])

    # CAPITAL ROTATION (06-Aug-26, Harish's request): how big a fund pool
    # was actually needed today if capital is reused as trades close,
    # versus what every trade would need if funded separately. This is the
    # number that decides how much capital he actually has to allocate to
    # this system -- not the sum of every trade's size.
    rotation_rows, peak_capital, total_no_reuse = capital_rotation_table(orders)
    if rotation_rows:
        saved = total_no_reuse - peak_capital
        saved_pct = (saved / total_no_reuse * 100) if total_no_reuse else 0.0
        n_trades = len({sym for _, sym, kind, _, _ in rotation_rows if kind == "ENTRY"})
        ws.append([])
        # Two half-panels under one row, same convention as TRADE SUMMARY /
        # P/L OVERVIEW above -- gets the KPI-panel banding/border treatment,
        # not the plain unstyled block Harish flagged as boring.
        ws.append([None, "ROTATION SUMMARY", None, None, "ROTATION SAVINGS"])
        ws.append([None, "Peak Capital In Use (Rs)", peak_capital, None,
                   "Total Capital If No Reuse (Rs)", total_no_reuse])
        ws.append([None, "Trades Rotated Through", n_trades, None,
                   "Capital Saved By Rotation (Rs)", round(saved, 2)])
        ws.append([None, "", "", None,
                   "Saved As % Of No-Reuse Total", f"{saved_pct:.1f}%"])
        ws.append([])
        ws.append([None, "CAPITAL ROTATION LOG (chronological entries & exits)"])
        ws.append([None, "Time", "Symbol", "Event", "Capital Change (Rs)",
                   "Capital In Use (Rs)"])
        for t, sym, kind, delta, running in rotation_rows:
            ws.append([None, t, sym, kind, delta, running])

    style_dashboard_sheet(ws)
    _autofit_columns(ws)

    wb.save(workbook_path)
    print(f"[dashboard] styled Dashboard sheet -> {workbook_path.name}")


# --------------------------------------------------------------------------
# STYLE layer -- ported near-verbatim from F:\06_Claude_v2's dashboard.py.
# Pure formatting over an already-populated Dashboard sheet: scans the
# sheet's own TEXT to find section headers and KPI labels, and colours off
# what it finds. Never touches a cell's value, so it is safe to call again
# on its own -- it always re-derives colour rather than accumulating it.
# --------------------------------------------------------------------------
_NAVY = "1F3864"
_SECTION_BLUE = "2F5596"
_TABLE_HEAD_BLUE = "4472C4"
_STATUS_BAND = "D9E2F3"
_GREEN = "1E9E4C"
_RED = "D33B2C"
_BAND_LIGHT = "FFFFFF"
_BAND_DARK = "F2F2F2"
_BORDER_COLOR = "D9D9D9"

_SECTION_TITLES = ("TRADE SUMMARY", "P/L OVERVIEW", "PER-SYMBOL PERFORMANCE",
                   "EXIT REASON DISTRIBUTION", "EQUITY CURVE", "HOURLY P&L BREAKDOWN",
                   "ROTATION SUMMARY", "ROTATION SAVINGS", "CAPITAL ROTATION LOG")
_KPI_PANEL_TITLES = ("TRADE SUMMARY", "P/L OVERVIEW", "ROTATION SUMMARY", "ROTATION SAVINGS")
_THRESHOLD_RULES = (
    ("win rate", lambda v: v >= 50),
    ("profit factor", lambda v: v >= 1),
    ("net p/l", lambda v: v >= 0),
    ("total p/l", lambda v: v >= 0),
    ("expectancy", lambda v: v >= 0),
)
_GOOD_LABEL_KEYWORDS = ("profitable trades", "max single profit", "best trade", "avg profit",
                        "capital saved", "saved as %")
_BAD_LABEL_KEYWORDS = ("loss trades", "max single loss", "worst trade", "avg loss", "max drawdown")


def _clean(text) -> str:
    return str(text).strip().upper() if text is not None else ""


def _parse_numeric(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = (str(value).strip().replace("Rs.", "").replace("Rs", "")
        .replace(",", "").replace("%", "").strip())
    try:
        return float(s)
    except ValueError:
        return None


def _classify_kpi_label(label_text):
    label = str(label_text).strip().lower()
    for keyword, is_good_fn in _THRESHOLD_RULES:
        if keyword in label:
            return "__threshold__", is_good_fn
    if any(k in label for k in _GOOD_LABEL_KEYWORDS):
        return "good", None
    if any(k in label for k in _BAD_LABEL_KEYWORDS):
        return "bad", None
    return None, None


def _find_last_used_column(ws, min_col=2) -> int:
    last_col = min_col
    for row in ws.iter_rows():
        for cell in row:
            if cell.value not in (None, "") and cell.column > last_col:
                last_col = cell.column
    return last_col


def _find_section_headers(ws, last_col) -> list[dict]:
    found = []
    for row in ws.iter_rows():
        for cell in row:
            text = _clean(cell.value)
            if not text:
                continue
            for title in _SECTION_TITLES:
                if title in text:
                    found.append({"row": cell.row, "start_col": cell.column, "title": title})
                    break
    found.sort(key=lambda h: (h["row"], h["start_col"]))
    for h in found:
        same_row_next = [o for o in found if o["row"] == h["row"] and o["start_col"] > h["start_col"]]
        h["end_col"] = min(o["start_col"] for o in same_row_next) - 1 if same_row_next else last_col
    return found


def _section_row_extent(ws, header_row, start_col, end_col, max_row) -> int:
    last_row = header_row
    for r in range(header_row + 1, max_row + 1):
        row_vals = [ws.cell(r, c).value for c in range(start_col, end_col + 1)]
        if all(v in (None, "") for v in row_vals):
            break
        first_cell_text = _clean(ws.cell(r, start_col).value)
        if any(title in first_cell_text for title in _SECTION_TITLES) and r != header_row:
            break
        last_row = r
    return last_row


def _style_title_and_status(ws, last_col) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    font_title = Font(color="FFFFFF", bold=True, size=14)
    last_col_letter = _col_letter(last_col)
    if ws.cell(1, 2).value not in (None, ""):
        ws.merge_cells(f"B1:{last_col_letter}1")
        ws.row_dimensions[1].height = 28
        title_cell = ws.cell(1, 2)
        title_cell.font = font_title
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        for c in range(2, last_col + 1):
            ws.cell(1, c).fill = PatternFill("solid", fgColor=_NAVY)

    if ws.cell(2, 2).value not in (None, ""):
        ws.merge_cells(f"B2:{last_col_letter}2")
        status_cell = ws.cell(2, 2)
        status_cell.alignment = Alignment(horizontal="center", vertical="center")
        status_cell.font = Font(bold=True, italic=True, color="2E7D32")
        for c in range(2, last_col + 1):
            ws.cell(2, c).fill = PatternFill("solid", fgColor=_STATUS_BAND)


def _style_section_header(ws, header: dict) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    row, start_col, end_col = header["row"], header["start_col"], header["end_col"]
    if end_col > start_col:
        ws.merge_cells(start_row=row, start_column=start_col, end_row=row, end_column=end_col)
    for c in range(start_col, end_col + 1):
        cell = ws.cell(row, c)
        cell.fill = PatternFill("solid", fgColor=_SECTION_BLUE)
        cell.font = Font(color="FFFFFF", bold=True, size=11)
    ws.cell(row, start_col).alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 20


def _style_kpi_panel(ws, header: dict) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    start_col, end_col = header["start_col"], header["end_col"]
    value_col = start_col + 1
    last_row = _section_row_extent(ws, header["row"], start_col, end_col, ws.max_row)
    thin = Side(style="thin", color=_BORDER_COLOR)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    band_idx = 0
    for r in range(header["row"] + 1, last_row + 1):
        label_cell = ws.cell(r, start_col)
        value_cell = ws.cell(r, value_col)
        if label_cell.value in (None, ""):
            continue

        band = _BAND_LIGHT if band_idx % 2 == 0 else _BAND_DARK
        band_idx += 1
        for c in range(start_col, end_col + 1):
            cell = ws.cell(r, c)
            cell.fill = PatternFill("solid", fgColor=band)
            cell.border = border

        label_cell.alignment = Alignment(horizontal="left", vertical="center")

        classification, threshold_fn = _classify_kpi_label(label_cell.value)
        numeric_val = _parse_numeric(value_cell.value)

        fill_color = None
        if classification == "__threshold__" and numeric_val is not None:
            fill_color = _GREEN if threshold_fn(numeric_val) else _RED
        elif classification == "good":
            fill_color = _GREEN
        elif classification == "bad":
            fill_color = _RED

        if fill_color:
            value_cell.fill = PatternFill("solid", fgColor=fill_color)
            value_cell.font = Font(color="FFFFFF", bold=True)
        else:
            value_cell.font = Font(color="000000", bold=True)
        value_cell.alignment = Alignment(horizontal="right", vertical="center")


def _style_wide_table(ws, header: dict) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    start_col, end_col = header["start_col"], header["end_col"]
    header_row = header["row"] + 1
    last_row = _section_row_extent(ws, header["row"], start_col, end_col, ws.max_row)
    if header_row > last_row:
        return
    thin = Side(style="thin", color=_BORDER_COLOR)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    pl_cols = set()
    for c in range(start_col, end_col + 1):
        cell = ws.cell(header_row, c)
        cell.fill = PatternFill("solid", fgColor=_TABLE_HEAD_BLUE)
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
        if "P/L" in _clean(cell.value):
            pl_cols.add(c)
    ws.row_dimensions[header_row].height = 18

    band_idx = 0
    for r in range(header_row + 1, last_row + 1):
        if all(ws.cell(r, c).value in (None, "") for c in range(start_col, end_col + 1)):
            continue
        band = _BAND_LIGHT if band_idx % 2 == 0 else _BAND_DARK
        band_idx += 1
        for c in range(start_col, end_col + 1):
            cell = ws.cell(r, c)
            cell.fill = PatternFill("solid", fgColor=band)
            cell.border = border
            if c in pl_cols:
                numeric_val = _parse_numeric(cell.value)
                cell.font = (Font(color=_GREEN, bold=True) if (numeric_val is None or numeric_val >= 0)
                            else Font(color=_RED, bold=True))
                cell.alignment = Alignment(horizontal="right", vertical="center")
            elif isinstance(cell.value, (int, float)):
                cell.font = Font(color="000000")
                cell.alignment = Alignment(horizontal="right", vertical="center")
            else:
                cell.font = Font(color="000000")
                cell.alignment = Alignment(
                    horizontal=("left" if c == start_col else "center"), vertical="center")


def _col_letter(idx: int) -> str:
    from openpyxl.utils import get_column_letter
    return get_column_letter(idx)


def style_dashboard_sheet(ws) -> None:
    """Colours an already-populated Dashboard sheet in place. Never touches
    a cell VALUE -- safe to call again on its own."""
    ws.column_dimensions["A"].width = 3
    last_col = _find_last_used_column(ws)
    _style_title_and_status(ws, last_col)
    for header in _find_section_headers(ws, last_col):
        _style_section_header(ws, header)
        if header["title"] in _KPI_PANEL_TITLES:
            _style_kpi_panel(ws, header)
        else:
            _style_wide_table(ws, header)
    ws.sheet_view.showGridLines = False


def _autofit_columns(ws, max_width: int = 45, min_width: int = 10) -> None:
    """
    Set every column's width to fit its longest cell, instead of the old
    fixed A-G tuple -- the CAPITAL ROTATION section (06-Aug-26) has its own
    column mix (Time/Symbol/Event/...) that the fixed widths weren't sized
    for. Capped so one long warning line doesn't blow the whole sheet out.
    Column A stays the fixed 3px separator style_dashboard_sheet already
    sets.
    """
    from openpyxl.utils import get_column_letter

    widths: dict[int, int] = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value in (None, ""):
                continue
            widths[cell.column] = max(widths.get(cell.column, 0), len(str(cell.value)))

    for col, length in widths.items():
        if col == 1:
            continue
        ws.column_dimensions[get_column_letter(col)].width = min(
            max_width, max(min_width, length + 2))


if __name__ == "__main__":
    # Rebuild the 31-Jul-26 session from its real numbers and check the
    # dashboard surfaces what the original one did not.
    real = [
        ("HDFCLIFE", 55.00, 157.07, -102.07, 11990, "PAPER"),
        ("BEL", 285.00, 180.82, 104.18, 13680, "PAPER"),
        ("MARUTI", 1275.00, 235.97, 1039.03, 17350, "PAPER"),
        ("SUNPHARMA", 280.00, 241.21, 38.79, 18305, "PAPER"),
        ("HDFCLIFE", -495.00, 152.35, -647.35, 11935, "LIVE"),
        ("KOTAKBANK", 500.00, 212.70, 287.30, 16000, "PAPER"),
        ("COALINDIA", -202.50, 159.05, -361.55, 12285, "LIVE"),
        ("BAJFINANCE", -487.50, 224.30, -711.80, 17437.5, "LIVE"),
        ("BAJAJFINSV", 180.00, 233.95, -53.95, 17805, "PAPER"),
        ("TATACONSUM", -220.00, 147.16, -367.16, 11385, "LIVE"),
        ("BAJFINANCE", 3615.00, 262.45, 3352.55, 18075, "PAPER"),
        ("JSWSTEEL", 573.75, 241.42, 332.33, 18157.5, "PAPER"),
        ("RELIANCE", -200.00, 195.52, -395.52, 15075, "LIVE"),
        ("INDUSINDBK", -805.00, 233.33, -1038.33, 18305, "PAPER"),
    ]
    df = pd.DataFrame([{
        "Symbol": s, "Gross P/L (Rs)": g, "Costs (Rs)": c, "Net P/L (Rs)": n,
        "Capital Required (Rs)": cap, "Trade Mode": m, "Exit Reason": "closed",
    } for s, g, c, n, cap, m in real])

    for label, subset in [("OVERALL", df),
                          ("LIVE", df[df["Trade Mode"] == "LIVE"]),
                          ("PAPER", df[df["Trade Mode"] == "PAPER"])]:
        s = compute_stats(subset, label)
        print(f"{label:<8} trades {s.trades:>3}  win {s.win_rate:>5.1f}%  "
              f"gross {s.gross:>9,.0f}  costs {s.costs:>8,.0f}  net {s.net:>9,.0f}  "
              f"net-ex-best {s.net_ex_best:>9,.0f}")

    overall = compute_stats(df, "OVERALL")
    print(f"\ncosts were {overall.cost_share_of_gross:.0f}% of gross profit")
    print(f"best trade {overall.best_symbol} Rs {overall.best:,.0f} = "
          f"{overall.best / overall.net * 100:.0f}% of total net")
    assert overall.net_ex_best < 0, "expected the other 13 trades to be net negative"
    print("\ndashboard self-check passed")

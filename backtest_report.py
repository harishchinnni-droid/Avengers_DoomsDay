"""
STEP 15 — Multi-session backtest report.

One day proves nothing. 31-Jul-26 produced 21 trades; a single bad or good
session at that sample size is noise. This module accumulates every date in a
BACKTEST range and reports the aggregate, so the question "does this edge
exist" gets answered with a sample rather than an anecdote.

WHAT IT REFUSES TO DO
---------------------
It will not report a headline number without also reporting:

  * the sample size, and a warning below MIN_TRADES_FOR_CONFIDENCE
  * net excluding the single best trade -- if the edge lives in one trade,
    there is no edge
  * an out-of-sample split, so in-sample tuning is visible
  * the lookahead audit: any fill timestamped before its signal
  * costs as a share of gross

Every figure is a historical measurement on the dates tested. None of it is
predictive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

import config
import dashboard
import ist_clock

# Below this, per-strategy statistics are not worth quoting. Retail systems
# are routinely declared profitable on 15 trades and then fail; the variance
# at that sample size swamps any real edge.
MIN_TRADES_FOR_CONFIDENCE = 100

# Fraction of DATES (not trades) used in-sample. The split is chronological
# -- a random split would leak future information into the training half.
IN_SAMPLE_FRACTION = 0.70


@dataclass
class SessionResult:
    trade_date: date
    orders: pd.DataFrame
    rejections: int = 0

    @property
    def net(self) -> float:
        if self.orders.empty:
            return 0.0
        return float(pd.to_numeric(self.orders["Net P/L (Rs)"],
                                   errors="coerce").fillna(0).sum())

    @property
    def trades(self) -> int:
        return len(self.orders)


class BacktestAccumulator:
    """Collects one SessionResult per date, then reports on the whole run."""

    def __init__(self):
        self.sessions: list[SessionResult] = []

    def add(self, trade_date: date, orders: pd.DataFrame,
            rejections: int = 0) -> None:
        self.sessions.append(SessionResult(trade_date, orders.copy(), rejections))

    @property
    def all_orders(self) -> pd.DataFrame:
        frames = []
        for s in self.sessions:
            if s.orders.empty:
                continue
            df = s.orders.copy()
            df.insert(0, "Trade Date", s.trade_date.strftime("%d-%b-%y"))
            frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    # ------------------------------------------------------------------
    def lookahead_failures(self) -> pd.DataFrame:
        """
        Rows whose fill predates the signal that produced them.

        This must always be empty. If it is not, every other number in the
        report is fiction and should be discarded rather than interpreted.
        """
        df = self.all_orders
        if df.empty or "Lookahead Check" not in df.columns:
            return pd.DataFrame()
        bad = df[df["Lookahead Check"].astype(str).str.startswith("FAIL")]
        return bad

    def daily_table(self) -> pd.DataFrame:
        rows = []
        equity = 0.0
        for s in sorted(self.sessions, key=lambda x: x.trade_date):
            equity += s.net
            stats = dashboard.compute_stats(s.orders, "d")
            rows.append({
                "Date": s.trade_date.strftime("%d-%b-%y"),
                "Trades": s.trades,
                "Wins": stats.wins,
                "Losses": stats.losses,
                "Win Rate %": round(stats.win_rate, 1),
                "Gross (Rs)": round(stats.gross, 2),
                "Costs (Rs)": round(stats.costs, 2),
                "Net (Rs)": round(s.net, 2),
                "Cumulative (Rs)": round(equity, 2),
                "Rejections": s.rejections,
            })
        return pd.DataFrame(rows)

    def split_stats(self) -> tuple:
        """(in_sample, out_of_sample) stats, split chronologically by date."""
        ordered = sorted(self.sessions, key=lambda x: x.trade_date)
        if len(ordered) < 4:
            return None, None
        cut = max(1, int(len(ordered) * IN_SAMPLE_FRACTION))
        def merge(group):
            frames = [s.orders for s in group if not s.orders.empty]
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        return (dashboard.compute_stats(merge(ordered[:cut]), "IN-SAMPLE"),
                dashboard.compute_stats(merge(ordered[cut:]), "OUT-OF-SAMPLE"))

    def max_drawdown(self) -> float:
        daily = self.daily_table()
        if daily.empty:
            return 0.0
        equity = daily["Cumulative (Rs)"]
        return float((equity.cummax() - equity).max())

    # ------------------------------------------------------------------
    def summary_sheet(self) -> pd.DataFrame:
        orders = self.all_orders
        overall = dashboard.compute_stats(orders, "OVERALL")
        dates = sorted(s.trade_date for s in self.sessions)
        rows: list[list] = [
            ["BACKTEST REPORT", "", "", "", ""],
            [f"Built {ist_clock.now_ist():%d-%b-%y %H:%M:%S} IST", "", "", "", ""],
            ["", "", "", "", ""],
            ["Sessions tested", len(self.sessions), "",
             "Date range", f"{dates[0]:%d-%b-%y} to {dates[-1]:%d-%b-%y}" if dates else "n/a"],
            ["Total trades", overall.trades, "", "Rejections",
             sum(s.rejections for s in self.sessions)],
            ["", "", "", "", ""],
            ["Win rate", f"{overall.win_rate:.1f}%", "", "Gross P/L (Rs)", round(overall.gross, 2)],
            ["Profit factor", f"{overall.profit_factor:.2f}", "", "Costs (Rs)", round(overall.costs, 2)],
            ["Expectancy/trade (Rs)", round(overall.expectancy, 2), "",
             "NET P/L (Rs)", round(overall.net, 2)],
            ["Avg win (Rs)", round(overall.avg_win, 2), "",
             "Costs as % of gross", f"{overall.cost_share_of_gross:.1f}%"],
            ["Avg loss (Rs)", round(overall.avg_loss, 2), "",
             "Max drawdown (Rs)", round(self.max_drawdown(), 2)],
            ["", "", "", "", ""],
            ["-- HONESTY CHECKS --", "", "", "", ""],
            ["Net excluding best trade (Rs)", round(overall.net_ex_best, 2), "",
             "Best trade", f"{overall.best_symbol} Rs {overall.best:,.2f}"],
        ]

        if overall.net > 0 and overall.net_ex_best < 0:
            rows.append(["VERDICT", "NO EDGE DEMONSTRATED -- all profit came "
                         "from a single trade", "", "", ""])

        if overall.trades < MIN_TRADES_FOR_CONFIDENCE:
            rows.append(["SAMPLE SIZE",
                         f"{overall.trades} trades is below {MIN_TRADES_FOR_CONFIDENCE}. "
                         f"Variance dominates. Do not tune on this.", "", "", ""])

        bad = self.lookahead_failures()
        rows.append(["Lookahead failures", len(bad), "",
                     "Status", "CLEAN" if bad.empty else "BACKTEST INVALID"])
        if not bad.empty:
            rows.append(["WARNING", "Fills timestamped before their signal. "
                         "Every figure above is fiction.", "", "", ""])

        ins, oos = self.split_stats()
        if ins and oos:
            rows += [
                ["", "", "", "", ""],
                ["-- IN-SAMPLE / OUT-OF-SAMPLE --", "", "", "", ""],
                ["In-sample trades", ins.trades, "", "In-sample net (Rs)", round(ins.net, 2)],
                ["In-sample win rate", f"{ins.win_rate:.1f}%", "",
                 "In-sample expectancy (Rs)", round(ins.expectancy, 2)],
                ["Out-of-sample trades", oos.trades, "", "Out-of-sample net (Rs)", round(oos.net, 2)],
                ["Out-of-sample win rate", f"{oos.win_rate:.1f}%", "",
                 "Out-of-sample expectancy (Rs)", round(oos.expectancy, 2)],
            ]
            if ins.expectancy > 0 and oos.expectancy < 0:
                rows.append(["VERDICT", "Profitable in-sample, unprofitable "
                             "out-of-sample -- classic overfit", "", "", ""])

        rows += [
            ["", "", "", "", ""],
            ["Historical measurement on the dates tested. Past performance "
             "does not predict future results.", "", "", ""],
        ]
        return pd.DataFrame(rows, columns=["Metric", "Value", "", "Metric ", "Value "])

    # ------------------------------------------------------------------
    def write(self, path) -> None:
        """Write Summary, Daily and All Trades sheets to a standalone workbook."""
        import excel_format

        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            self.summary_sheet().to_excel(writer, sheet_name="Summary", index=False)
            self.daily_table().to_excel(writer, sheet_name="Daily", index=False)
            orders = self.all_orders
            if not orders.empty:
                orders.to_excel(writer, sheet_name="All Trades", index=False)
            bad = self.lookahead_failures()
            if not bad.empty:
                bad.to_excel(writer, sheet_name="Lookahead Failures", index=False)

        excel_format.format_workbook(path, verbose=False)
        print(f"[report] backtest report -> {path.name}")

        # Visual companion. The spreadsheet is for lookup; this is for seeing
        # the shape of the result, which numbers in a grid never show.
        import config
        if not config.DASHBOARD_ENABLED:
            print("[report] visual dashboard skipped (config.DASHBOARD_ENABLED = False)")
            return
        try:
            import dashboard_html
            dates = sorted(s.trade_date for s in self.sessions)
            ordered = sorted(self.sessions, key=lambda s: s.trade_date)
            sessions_for_picker = [
                (s.trade_date.strftime("%d-%b-%y"), s.orders) for s in ordered
                if not s.orders.empty
            ]
            dashboard_html.write_dashboard(
                self.all_orders, path.with_suffix(".html"), self.daily_table(),
                title="F&O backtest",
                subtitle=(f"{dates[0]:%d-%b-%y} to {dates[-1]:%d-%b-%y} · "
                          f"{len(self.sessions)} sessions · paper only"),
                sessions=sessions_for_picker)
        except Exception as exc:
            print(f"[report] visual dashboard skipped: {exc}")

    def print_summary(self) -> None:
        orders = self.all_orders
        s = dashboard.compute_stats(orders, "OVERALL")
        dates = sorted(x.trade_date for x in self.sessions)
        print(f"\n{'=' * 70}\nBACKTEST REPORT\n{'=' * 70}")
        if dates:
            print(f"  {len(self.sessions)} session(s), {dates[0]:%d-%b-%y} "
                  f"to {dates[-1]:%d-%b-%y}")
        print(f"  trades {s.trades}   win rate {s.win_rate:.1f}%   "
              f"profit factor {s.profit_factor:.2f}")
        print(f"  gross Rs {s.gross:,.2f}   costs Rs {s.costs:,.2f} "
              f"({s.cost_share_of_gross:.0f}% of gross)")
        print(f"  NET   Rs {s.net:,.2f}   expectancy Rs {s.expectancy:,.2f}/trade")
        print(f"  max drawdown Rs {self.max_drawdown():,.2f}")
        print(f"  net excluding best trade Rs {s.net_ex_best:,.2f}")

        bad = self.lookahead_failures()
        if not bad.empty:
            print(f"\n  BACKTEST INVALID: {len(bad)} fill(s) timestamped before "
                  f"their signal.")
        if s.trades < MIN_TRADES_FOR_CONFIDENCE:
            print(f"\n  SAMPLE TOO SMALL: {s.trades} trades, need "
                  f"{MIN_TRADES_FOR_CONFIDENCE}+ before tuning anything.")
        if s.net > 0 and s.net_ex_best < 0:
            print(f"\n  NO EDGE: every rupee of profit came from one trade.")

        ins, oos = self.split_stats()
        if ins and oos:
            print(f"\n  in-sample  : {ins.trades:>4} trades  net Rs {ins.net:>11,.2f}  "
                  f"expectancy Rs {ins.expectancy:>8,.2f}")
            print(f"  out-of-sample: {oos.trades:>2} trades  net Rs {oos.net:>11,.2f}  "
                  f"expectancy Rs {oos.expectancy:>8,.2f}")
        print(f"\n  Historical only. Not a forecast.\n{'=' * 70}")


if __name__ == "__main__":
    print(__doc__)

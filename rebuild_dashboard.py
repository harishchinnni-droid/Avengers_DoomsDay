"""
STANDALONE — rebuild the consolidated dashboard from whatever dated
workbooks already sit in the project folder.

WHY THIS EXISTS
----------------
run_TW_ALL.py only writes the consolidated "Backtest ... .xlsx/.html"
report at the END of a single multi-date BACKTEST run, from an
accumulator it built in memory while running. If you want that same
consolidated report LATER -- after the fact, from whatever dated
'DD-Mon-YY FNO-*-TW-ALL.xlsx' workbooks are sitting on disk, possibly from
several separate runs -- there was no way to get it without re-running
the whole pipeline. This script reads the workbooks directly instead.

WHAT IT DOES
------------
1. Scans a folder for 'DD-Mon-YY FNO-L-TW-ALL.xlsx' / 'DD-Mon-YY FNO-BT-TW-ALL.xlsx'
   files (skips anything else, including a prior consolidated report).
2. Reads the Orders sheet (and Rejected sheet, for the rejection count)
   out of each one.
3. Feeds them into the same BacktestAccumulator run_TW_ALL.py uses, so
   the output -- Summary / Daily / All Trades sheets, honesty checks,
   in-sample/out-of-sample split, the visual HTML twin -- is identical in
   shape to what a live multi-date run would have produced.

USAGE
-----
    py rebuild_dashboard.py
        scans the project root (one level above this folder), all dates,
        both LIVE and BACKTEST workbooks, writes
        'Backtest <first> to <last>.xlsx' (+ matching .html) there.

    py rebuild_dashboard.py --folder "F:\\some\\other\\folder"
    py rebuild_dashboard.py --mode BACKTEST        (BACKTEST workbooks only)
    py rebuild_dashboard.py --mode LIVE            (LIVE workbooks only)
    py rebuild_dashboard.py --start 11-Aug-26 --end 13-Aug-26
    py rebuild_dashboard.py --out "My Report.xlsx"

Every number in the output is a re-read of whatever is already saved in
those workbooks' Orders sheets -- nothing here re-simulates or re-runs a
strategy. Historical measurement only, same as the rest of this project.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

import calendar_mgmt
import paths

# 'DD-Mon-YY FNO-L-TW-ALL.xlsx' or 'DD-Mon-YY FNO-BT-TW-ALL.xlsx'
_WORKBOOK_RE = re.compile(
    r"^(?P<stamp>\d{2}-[A-Za-z]{3}-\d{2}) FNO-(?P<mode>L|BT)-TW-ALL\.xlsx$"
)


def find_workbooks(folder: Path, mode_filter: str | None,
                   start: date | None, end: date | None) -> list[tuple[date, str, Path]]:
    """Return [(trade_date, 'LIVE'|'BACKTEST', path), ...], sorted by date."""
    found = []
    for f in folder.glob("*.xlsx"):
        m = _WORKBOOK_RE.match(f.name)
        if not m:
            continue
        d = calendar_mgmt.parse_date(m.group("stamp"))
        if d is None:
            continue
        mode = "LIVE" if m.group("mode") == "L" else "BACKTEST"
        if mode_filter and mode != mode_filter:
            continue
        if start and d < start:
            continue
        if end and d > end:
            continue
        found.append((d, mode, f))
    found.sort(key=lambda x: x[0])
    return found


def _read_sheet(path: Path, sheet: str) -> pd.DataFrame:
    try:
        return pd.read_excel(path, sheet_name=sheet)
    except ValueError:
        return pd.DataFrame()  # sheet not in this workbook -- treat as empty


def build_accumulator(workbooks: list[tuple[date, str, Path]]):
    import backtest_report

    acc = backtest_report.BacktestAccumulator()
    for d, mode, path in workbooks:
        orders = _read_sheet(path, "Orders")
        rejected = _read_sheet(path, "Rejected")
        acc.add(d, orders, rejections=len(rejected))
        print(f"[rebuild] {calendar_mgmt.format_date(d)} [{mode}] "
              f"{path.name} -> {len(orders)} order row(s), "
              f"{len(rejected)} rejection(s)")
    return acc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", type=str, default=None,
                    help="folder to scan for dated workbooks (default: project root)")
    ap.add_argument("--mode", choices=["LIVE", "BACKTEST"], default=None,
                    help="only include this mode's workbooks (default: both)")
    ap.add_argument("--start", type=str, default=None, help="DD-Mon-YY, inclusive")
    ap.add_argument("--end", type=str, default=None, help="DD-Mon-YY, inclusive")
    ap.add_argument("--out", type=str, default=None,
                    help="output .xlsx filename (default: 'Backtest <first> to <last>.xlsx')")
    args = ap.parse_args()

    folder = Path(args.folder) if args.folder else paths.BASE_DIR
    if not folder.exists():
        print(f"[rebuild] folder not found: {folder}")
        return 1

    start = calendar_mgmt.parse_date(args.start) if args.start else None
    end = calendar_mgmt.parse_date(args.end) if args.end else None
    if args.start and start is None:
        print(f"[rebuild] could not parse --start {args.start!r}, expected DD-Mon-YY")
        return 1
    if args.end and end is None:
        print(f"[rebuild] could not parse --end {args.end!r}, expected DD-Mon-YY")
        return 1

    workbooks = find_workbooks(folder, args.mode, start, end)
    if not workbooks:
        print(f"[rebuild] no dated FNO workbooks found in {folder} "
              f"matching the given filters")
        return 1

    print(f"[rebuild] {len(workbooks)} workbook(s) found in {folder}")
    acc = build_accumulator(workbooks)
    if not acc.sessions:
        print("[rebuild] nothing to report")
        return 1

    acc.print_summary()

    if args.out:
        out = folder / args.out
    else:
        dates = sorted(s.trade_date for s in acc.sessions)
        out = folder / (f"Backtest {calendar_mgmt.format_date(dates[0])} "
                        f"to {calendar_mgmt.format_date(dates[-1])}.xlsx")

    acc.write(out)
    print(f"\n[rebuild] done -> {out.name} (+ {out.with_suffix('.html').name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

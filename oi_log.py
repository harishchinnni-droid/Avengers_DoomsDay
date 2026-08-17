"""
Portable log of every OI confirmation check (option_audit.oi_confirms),
one CSV per trading date in paths.OI_DATA_DIR.

CSV, not xlsx (Harish, 07-Aug-26): this gets appended to many times through
the day, from a live process that's also writing the main trade workbook.
An xlsx is a zip archive -- read it mid-write and you get exactly the
BadZipFile/CRC corruption already hit reading the live workbook that same
day. A CSV survives a torn read or a partial copy; each line is independent.

Deliberately its own folder (paths.OI_DATA_DIR) and its own small file per
day, so it's a simple, low-risk thing to copy or sync between machines
without touching the trade workbook at all.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

import config
import ist_clock
import paths

COLUMNS = ["Date", "Time", "Symbol", "Signal", "Stage", "Call OI", "Put OI",
          "Own-Side Share", "Threshold", "Verdict", "Note"]


def day_file(trade_date: date) -> Path:
    return paths.OI_DATA_DIR / f"{trade_date:%d-%b-%y}_OI.csv"


def log_oi_check(trade_date: date, symbol: str, signal: str, stage: str,
                 snapshot: dict | None, ok: bool, note: str,
                 now: datetime | None = None) -> None:
    """
    Append one row for one OI check. Safe to call every time oi_confirms
    runs with real data -- callers only call this when `snapshot` is not
    None, so a BACKTEST run or a disabled gate never spams the file with
    "skipped" rows.

    `stage` is "early" (the normal audit-time gate, #25 in the pipeline) or
    "late" (the re-check closer to fill, right before the trade is
    finalised into Orders/Missed_Concurrent).
    """
    now = now or ist_clock.now_ist()
    paths.OI_DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = day_file(trade_date)
    is_new = not path.exists()

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(COLUMNS)
        writer.writerow([
            trade_date.strftime("%d-%b-%y"), now.strftime("%H:%M:%S"),
            symbol, signal, stage,
            snapshot.get("call_oi", "") if snapshot else "",
            snapshot.get("put_oi", "") if snapshot else "",
            f"{snapshot['own_share']:.4f}" if snapshot else "",
            config.OI_MIN_OWN_SIDE_SHARE,
            "PASS" if ok else "FAIL", note,
        ])


if __name__ == "__main__":
    import tempfile
    orig_dir = paths.OI_DATA_DIR
    try:
        paths.OI_DATA_DIR = Path(tempfile.mkdtemp())
        td = date(2026, 8, 7)
        log_oi_check(td, "AMBER", "BUY PE", "early",
                    {"call_oi": 1000, "put_oi": 9000, "own_share": 0.90},
                    True, "OI backs BUY PE (put share 90%)")
        log_oi_check(td, "AMBER", "BUY PE", "late",
                    {"call_oi": 1200, "put_oi": 8800, "own_share": 0.88},
                    True, "OI backs BUY PE (put share 88%)")
        rows = day_file(td).read_text(encoding="utf-8").strip().splitlines()
        print(f"wrote {len(rows) - 1} row(s) to {day_file(td).name}")
        for r in rows:
            print(" ", r)
        assert len(rows) == 3, "expected header + 2 rows"
        print("\noi_log self-check passed")
    finally:
        paths.OI_DATA_DIR = orig_dir

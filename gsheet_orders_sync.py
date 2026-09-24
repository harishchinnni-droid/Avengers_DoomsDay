"""
GSHEET SYNC (HARISH) -- mirror the Excel "Orders" sheet into Google Sheets.

ADDITIVE ONLY (Harish, 24-Sep-26). The Excel workbook stays the source of
truth and keeps being written exactly as before. This module only COPIES
each Orders snapshot into the "Avengers_DoomsDay" Google Sheet, tab
gid=481203106, so the trades can be watched from a phone/browser.

    run_HARISH.py (LIVE)  --each 5-min candle-->  Excel Orders sheet  (unchanged)
                                             \-->  gsheet_orders_sync.push()  (new)

RULES
-----
* LIVE only. Backtests never call this.
* PAPER and LIVE trades both pushed; "Trade Mode" column tells them apart.
* Existing columns are matched BY HEADER NAME (case-insensitive), never by
  position -- moving a column in the Google Sheet does not break anything.
* Any column in EXTRA_COLUMNS that the sheet doesn't have yet is created at
  the END of the header row. Nothing existing is renamed, moved or deleted.
* Only cells of mapped columns in HARISH-owned rows are ever written. Rows
  written by anything else (Source != HARISH) are never touched.
* Upsert, not append-every-time: each trade has a "Sync Key"
  (Date|TradingSymbol|Entry Time). Same key -> row updated in place, so a
  restart or every 5-min refresh never duplicates a trade.
* NEVER breaks the pipeline. Network runs in a background thread; every
  error is printed and swallowed. If Google is down, Excel still gets
  written and trading continues.

ONE-OFF TEST (pushes the Orders sheet of any existing workbook):
    py gsheet_orders_sync.py "24-Sep-26 FNO-L-HARISH.xlsx"
    py gsheet_orders_sync.py "24-Sep-26 FNO-L-HARISH.xlsx" --dry-run   (no network, prints rows)

Needs (once):  pip install gspread google-auth
Service account in harish_credentials.json must be Editor on the sheet
(already true -- the sb_* bot uses the same account on the same sheet).
"""

from __future__ import annotations

import math
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Settings -- all here, config.py untouched
# ---------------------------------------------------------------------------
GSHEET_SYNC_ENABLED = True

BASE_DIR = Path(__file__).resolve().parent.parent
GOOGLE_CREDENTIALS_FILE = BASE_DIR / "01_JSON_Files" / "harish_credentials.json"

SHEET_ID = "1q2bZhseDRclGmRW-_voZQCNV2nJt9JP75Xo8D-o2nNE"
WORKSHEET_GID = 481203106          # the tab in the screenshot (from the URL #gid=...)

SOURCE_TAG = "HARISH"              # written into "Source" -- marks rows this module owns
HTTP_TIMEOUT_SECS = 20
N_TARGETS = 10                     # Target 1..Target 10 columns in the sheet

# Columns that already exist in the Google Sheet (matched by name).
# Missing ones are simply skipped -- never created.
EXISTING_COLUMNS = [
    "Date", "Time", "Symbol", "Strike", "CE / PE", "Expiry", "Lots", "Status",
    "TradingSymbol", "Token", "Qty", "Entry Price", "Entry Time",
    "Buy Order ID", "LTP", "Peak LTP", "P&L", "Targets Hit", "Last Update",
    "Stop Loss", "TSL On", "Exit Price", "Exit Time", "Exit Reason",
    "Sell Order ID", *[f"Target {i}" for i in range(1, N_TARGETS + 1)],
]
# "Entry Above" is deliberately NOT written: HARISH enters at market on the
# signal, there is no breakout trigger price to show.

# New columns -- created at the END of the sheet if missing.
EXTRA_COLUMNS = [
    "Source", "Sync Key", "Trade Mode", "Signal", "Entry Type",
    "Signal Confirmed At", "Spot Price", "Initial Stop Loss", "Min LTP",
    "Remaining Qty", "Gross P/L", "Costs", "Risk Amount", "Capital Required",
    "Broker Stop Order ID", "Broker Stop Trigger",
]

# Text-only columns: prefixed with ' so Google doesn't turn a 15-digit order
# id into 2.60924E+14 or strip leading zeros off a token.
TEXT_COLUMNS = {"Buy Order ID", "Sell Order ID", "Token", "Broker Stop Order ID",
                "Sync Key", "TradingSymbol"}

KEY_COL = "Sync Key"
SOURCE_COL = "Source"


# ---------------------------------------------------------------------------
# Excel Orders row -> Google Sheet row
# ---------------------------------------------------------------------------
def _blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return str(v).strip() in ("", "nan", "NaT", "None")


def _v(row: pd.Series, col: str) -> Any:
    v = row.get(col, "")
    return "" if _blank(v) else v


def _num(row: pd.Series, col: str) -> float | None:
    v = _v(row, col)
    if v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _r2(x: float | None) -> Any:
    return "" if x is None else round(x, 2)


def _id(v: Any) -> str:
    """Order ids/tokens arrive as int, float (1.2e14) or str -- normalise."""
    if _blank(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def map_order_row(row: pd.Series, trade_date: date) -> dict[str, Any]:
    """One Excel Orders row -> {google header: value}."""
    signal = str(_v(row, "Signal"))
    ce_pe = "CE" if signal.endswith("CE") else "PE" if signal.endswith("PE") else ""

    units = _num(row, "Quantity (Units)") or 0
    entry_model = _num(row, "Entry LTP")
    entry_fill = _num(row, "Actual Fill Price")
    entry = entry_fill if entry_fill else entry_model
    gross = _num(row, "Gross P/L (Rs)")

    exit_reason = str(_v(row, "Exit Reason"))
    exit_time = str(_v(row, "Exit Time"))
    closed = bool(exit_reason and exit_time)

    # Exit price: the broker's actual price if there is one, otherwise the
    # quantity-weighted average of all partial exits, recovered from gross P/L
    # (gross = sum((exit - entry) * qty) and the exits sum to Quantity (Units)).
    exit_price = _num(row, "Actual Exit Price")
    if closed and not exit_price and entry_model and units and gross is not None:
        exit_price = entry_model + gross / units

    ltp = _num(row, "Current LTP")
    if closed and exit_price:
        ltp = exit_price

    t_hits = [str(_v(row, f"T{i} Hit")).upper() == "YES"
              for i in range(1, N_TARGETS + 1)]

    days_to_exp = _num(row, "Days To Expiry")
    expiry = (trade_date + timedelta(days=int(days_to_exp))).strftime("%d-%b-%y") \
        if days_to_exp is not None else ""

    if closed:
        status = f"EXITED - {exit_reason.upper()}"
    elif _num(row, "Remaining Qty") == 0:
        status = "CLOSED"
    else:
        status = "OPEN"

    strike = _num(row, "ATM Strike")
    trading_symbol = _id(_v(row, "Option Symbol"))
    entry_time = str(_v(row, "Entry Time"))

    out: dict[str, Any] = {
        "Date": trade_date.strftime("%d-%b-%y"),
        "Time": str(_v(row, "Signal Confirmed At")) or str(_v(row, "Entry Trigger Time")),
        "Symbol": _v(row, "Symbol"),
        "Strike": (int(strike) if strike is not None and strike.is_integer() else _r2(strike)),
        "CE / PE": ce_pe,
        "Expiry": expiry,
        "Lots": _v(row, "Quantity (Lots)"),
        "Status": status,
        "TradingSymbol": trading_symbol,
        "Token": _id(_v(row, "Option Token")),
        "Qty": _v(row, "Quantity (Units)"),
        "Entry Price": _r2(entry),
        "Entry Time": entry_time,
        "Buy Order ID": _id(_v(row, "Broker Order ID")) or _id(_v(row, "Order ID")),
        "LTP": _r2(ltp),
        "Peak LTP": _r2(_num(row, "Max LTP")),
        "P&L": _r2(_num(row, "Net P/L (Rs)")),
        "Targets Hit": sum(t_hits),
        "Last Update": datetime.now().strftime("%H:%M:%S"),
        "Stop Loss": _r2(_num(row, "Effective Stop")),
        "TSL On": "YES" if t_hits and t_hits[0] else "NO",     # TSL arms after T1
        "Exit Price": _r2(exit_price) if closed else "",
        "Exit Time": exit_time,
        "Exit Reason": exit_reason.upper(),
        "Sell Order ID": _id(_v(row, "Exit Order ID")),
        **{f"Target {i}": _r2(_num(row, f"Target {i} LTP"))
           for i in range(1, N_TARGETS + 1)},
        # ---- new columns (end of sheet) ----
        "Source": SOURCE_TAG,
        "Sync Key": f"{trade_date:%Y%m%d}|{trading_symbol}|{entry_time}",
        "Trade Mode": _v(row, "Trade Mode"),
        "Signal": signal,
        "Entry Type": _v(row, "Entry Type"),
        "Signal Confirmed At": _v(row, "Signal Confirmed At"),
        "Spot Price": _r2(_num(row, "Spot Price")),
        "Initial Stop Loss": _r2(_num(row, "Stop Loss LTP")),
        "Min LTP": _r2(_num(row, "Min LTP")),
        "Remaining Qty": _v(row, "Remaining Qty"),
        "Gross P/L": _r2(gross),
        "Costs": _r2(_num(row, "Costs (Rs)")),
        "Risk Amount": _r2(_num(row, "Risk Amount (Rs)")),
        "Capital Required": _r2(_num(row, "Capital Required (Rs)")),
        "Broker Stop Order ID": _id(_v(row, "Broker Stop Order ID")),
        "Broker Stop Trigger": _r2(_num(row, "Broker Stop Trigger")),
    }
    return out


def map_orders(orders_df: pd.DataFrame | None, trade_date: date) -> list[dict[str, Any]]:
    if orders_df is None or orders_df.empty:
        return []
    rows = []
    for _, r in orders_df.iterrows():
        if _blank(r.get("Option Symbol")) or _blank(r.get("Entry Time")):
            continue                          # not a real position row
        rows.append(map_order_row(r, trade_date))
    return rows


# ---------------------------------------------------------------------------
# Google side
# ---------------------------------------------------------------------------
_ws_cache = None


def _worksheet():
    global _ws_cache
    if _ws_cache is not None:
        return _ws_cache
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        str(GOOGLE_CREDENTIALS_FILE),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    try:
        gc.set_timeout(HTTP_TIMEOUT_SECS)
    except Exception:
        pass
    ws = gc.open_by_key(SHEET_ID).get_worksheet_by_id(WORKSHEET_GID)
    print(f"[gsheet] connected -> tab {ws.title!r}")
    _ws_cache = ws
    return ws


def _a1(row: int, col: int) -> str:
    letters, n = "", col
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def _cell_value(col_name: str, v: Any) -> Any:
    if _blank(v):
        return ""
    if col_name in TEXT_COLUMNS:
        return "'" + str(v)
    return v


def _ensure_headers(ws, headers: list[str]) -> dict[str, int]:
    """{lower header -> 1-based col}. Appends EXTRA_COLUMNS that are missing,
    at the end. Never touches existing header cells."""
    hmap = {h.strip().lower(): i + 1 for i, h in enumerate(headers) if h.strip()}
    missing = [c for c in EXTRA_COLUMNS if c.lower() not in hmap]
    if missing:
        last = max(hmap.values()) if hmap else 0
        need = last + len(missing)
        if need > ws.col_count:
            ws.add_cols(need - ws.col_count)
        ws.update(range_name=f"{_a1(1, last + 1)}:{_a1(1, need)}",
                  values=[missing], value_input_option="RAW")
        for i, name in enumerate(missing):
            hmap[name.lower()] = last + 1 + i
        print(f"[gsheet] added {len(missing)} new column(s) at end: {missing}")
    return hmap


def write_rows(rows: list[dict[str, Any]]) -> None:
    """Upsert every mapped row into the Google Sheet in ONE batch call."""
    if not rows:
        return
    ws = _worksheet()
    values = ws.get_all_values()
    headers = values[0] if values else []
    hmap = _ensure_headers(ws, headers)

    key_c, src_c = hmap[KEY_COL.lower()], hmap[SOURCE_COL.lower()]
    existing: dict[str, int] = {}
    for rnum, r in enumerate(values[1:], start=2):
        key = r[key_c - 1].strip().lstrip("'") if len(r) >= key_c else ""
        src = r[src_c - 1].strip() if len(r) >= src_c else ""
        if key and src == SOURCE_TAG:
            existing[key] = rnum

    # First empty row = after the last row that has ANY value.
    next_row = 1
    for rnum, r in enumerate(values, start=1):
        if any(c.strip() for c in r):
            next_row = rnum
    next_row += 1

    writable = [c for c in (*EXISTING_COLUMNS, *EXTRA_COLUMNS) if c.lower() in hmap]
    updates, added, updated = [], 0, 0
    for row in rows:
        target = existing.get(row["Sync Key"])
        if target is None:
            target = next_row
            next_row += 1
            existing[row["Sync Key"]] = target
            added += 1
        else:
            updated += 1
        for col in writable:
            if col in row:
                updates.append({"range": _a1(target, hmap[col.lower()]),
                                "values": [[_cell_value(col, row[col])]]})

    if next_row - 1 > ws.row_count:
        ws.add_rows(next_row - 1 - ws.row_count + 20)

    ws.batch_update(updates, value_input_option="USER_ENTERED")
    print(f"[gsheet] synced {len(rows)} trade(s): {added} new, {updated} updated")


# ---------------------------------------------------------------------------
# Background pusher -- the live loop never waits on Google
# ---------------------------------------------------------------------------
class _Pusher:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[dict[str, Any]] | None = None
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        self._disabled_reason = ""

    def submit(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._pending = rows          # latest snapshot wins -- older ones are stale
            self._idle.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="gsheet-sync",
                                            daemon=True)
            self._thread.start()
        self._wake.set()

    def _run(self) -> None:
        global _ws_cache
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                rows, self._pending = self._pending, None
            if rows is None:
                self._idle.set()
                continue
            for attempt in (1, 2):
                try:
                    write_rows(rows)
                    break
                except Exception as exc:
                    _ws_cache = None      # force a fresh connection next time
                    print(f"[gsheet] push failed (attempt {attempt}/2, Excel "
                          f"unaffected): {type(exc).__name__}: {exc}")
                    time.sleep(3)
            with self._lock:
                if self._pending is None:
                    self._idle.set()
                else:
                    self._wake.set()

    def flush(self, timeout: float = 60) -> None:
        if not self._idle.wait(timeout):
            print(f"[gsheet] flush timed out after {timeout:.0f}s")


_pusher = _Pusher()
_import_ok: bool | None = None


def _deps_ok() -> bool:
    global _import_ok
    if _import_ok is None:
        try:
            import gspread  # noqa: F401
            import google.oauth2.service_account  # noqa: F401
            _import_ok = True
        except ImportError:
            _import_ok = False
            print("[gsheet] DISABLED -- gspread not installed. Run once:  "
                  "py -m pip install gspread google-auth")
        if _import_ok and not GOOGLE_CREDENTIALS_FILE.exists():
            _import_ok = False
            print(f"[gsheet] DISABLED -- credentials not found: {GOOGLE_CREDENTIALS_FILE}")
    return _import_ok


# ---------------------------------------------------------------------------
# Public API -- what run_HARISH.py calls
# ---------------------------------------------------------------------------
def push(orders_df: pd.DataFrame | None, trade_date: date) -> None:
    """Queue this Orders snapshot for Google Sheets. Returns immediately.
    Safe to call every candle -- it can never raise."""
    try:
        if not GSHEET_SYNC_ENABLED or not _deps_ok():
            return
        rows = map_orders(orders_df, trade_date)
        if rows:
            _pusher.submit(rows)
    except Exception as exc:
        print(f"[gsheet] skipped this cycle (Excel unaffected): {exc}")


def flush(timeout: float = 60) -> None:
    """Wait for the last queued push to finish (call at session end)."""
    try:
        if GSHEET_SYNC_ENABLED and _import_ok:
            _pusher.flush(timeout)
    except Exception as exc:
        print(f"[gsheet] flush error: {exc}")


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------
def _date_from_name(name: str) -> date:
    m = re.match(r"(\d{2}-[A-Za-z]{3}-\d{2})", name)
    if not m:
        raise SystemExit(f"can't read a DD-Mon-YY date from {name!r}")
    return datetime.strptime(m.group(1), "%d-%b-%y").date()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    wb = Path(argv[1])
    if not wb.is_absolute() and not wb.exists():
        wb = BASE_DIR / wb
    trade_date = _date_from_name(wb.name)
    df = pd.read_excel(wb, sheet_name="Orders")
    rows = map_orders(df, trade_date)
    print(f"[gsheet] {wb.name}: {len(df)} Orders row(s) -> {len(rows)} trade(s)")
    if "--dry-run" in argv:
        for r in rows:
            print({k: v for k, v in r.items() if v != ""})
        return 0
    if not _deps_ok():
        return 1
    write_rows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

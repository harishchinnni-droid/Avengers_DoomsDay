"""
STEP 5 — Symbols -> Zerodha instrument tokens.

Resolves every row of the watchlist (the sheet config.WATCHLIST_SHEET points
to) against Kite's own NSE equity instrument dump and writes the
Zerodha_Token column back onto the sheet. Unresolved symbols get null,
reported never silently dropped -- same policy as
angel_scrip.resolve_angel_tokens.

Kite's instrument dump (NSE ~2,000 rows, NFO ~50-80k) is the same for every
date inside one calendar day, so it is cached to paths.INSTRUMENT_TOKEN_CACHE
keyed by download date, same date-blob convention as broker_auth's session
cache -- a multi-date BACKTEST run makes one Kite call per exchange, not one
per date.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

import config
import paths

_MEMORY_CACHE: dict[str, pd.DataFrame] = {}


def _fetch_instruments(kite, exchange: str) -> pd.DataFrame:
    """
    Kite's full instrument dump for one exchange, as a DataFrame.

    Cached twice: in-memory for the life of the process (order_engine and
    update_instrument_tokens both call this within the same run), and to
    disk in paths.INSTRUMENT_TOKEN_CACHE, one blob nesting every exchange
    dumped so far today, so a same-day re-run doesn't re-download the NFO
    dump just to resolve one symbol.
    """
    exchange = exchange.upper()
    if exchange in _MEMORY_CACHE:
        return _MEMORY_CACHE[exchange]

    today = date.today().isoformat()
    cache_file = paths.INSTRUMENT_TOKEN_CACHE

    blob: dict = {}
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, json.JSONDecodeError):
            blob = {}
    if blob.get("date") != today:
        blob = {"date": today}

    if exchange in blob:
        df = pd.DataFrame(blob[exchange])
        if "expiry" in df.columns:
            df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce")
        _MEMORY_CACHE[exchange] = df
        print(f"[token] {exchange} instruments: {len(df):,} row(s) "
              f"(disk cache, {today})")
        return df

    print(f"[token] downloading {exchange} instrument dump ...")
    rows = kite.instruments(exchange)
    df = pd.DataFrame(rows)

    blob[exchange] = rows
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, default=str)
    tmp.replace(cache_file)

    if "expiry" in df.columns:
        df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce")
    _MEMORY_CACHE[exchange] = df
    print(f"[token] {exchange} instruments: {len(df):,} row(s) (downloaded)")
    return df


def update_instrument_tokens(workbook: Path, kite, trade_date: date) -> pd.DataFrame:
    """
    Resolve every watchlist symbol's NSE equity instrument_token and write
    the Zerodha_Token column back onto the watchlist sheet.

    Every row in the Reference sheet is the underlying (FUTSTK in the
    Instrument Type column), not the option contract -- this is the token
    data_ingestion uses to pull the underlying's own 5-min candles for the
    matrix sheets. Option contract tokens are resolved later, per-trade, in
    option_chain.py.
    """
    import file_mgmt

    df_ref = file_mgmt.read_reference_sheet(workbook)

    nse = _fetch_instruments(kite, "NSE")
    nse = nse[nse["segment"] == "NSE"].copy()
    nse["tradingsymbol"] = nse["tradingsymbol"].astype(str).str.upper()
    nse_by_symbol = nse.drop_duplicates("tradingsymbol").set_index("tradingsymbol")

    tokens: list[int | None] = []
    failures: list[str] = []

    for _, row in df_ref.iterrows():
        sym = str(row[config.COL_SYMBOL]).upper()
        if sym in nse_by_symbol.index:
            tokens.append(int(nse_by_symbol.loc[sym, "instrument_token"]))
        else:
            tokens.append(None)
            failures.append(sym)

    df_ref[config.COL_ZERODHA_TOKEN] = tokens

    resolved = sum(t is not None for t in tokens)
    print(f"[token] Zerodha tokens resolved {resolved}/{len(df_ref)}")
    if failures:
        print(f"[token] unresolved: {', '.join(failures[:15])}"
              + (f" ... +{len(failures) - 15} more" if len(failures) > 15 else ""))

    file_mgmt.write_sheet(workbook, config.WATCHLIST_SHEET, df_ref)
    return df_ref


def add_day_ohlc(df_ref: pd.DataFrame, candles: dict[str, pd.DataFrame],
                 trade_date: date) -> pd.DataFrame:
    """
    Fill Opening / Closing / Change% on the watchlist DataFrame from that
    day's own candles -- the underlying's first open and latest close for
    `trade_date` specifically, not the multi-day lookback window `candles`
    also carries. So the watchlist shows which stocks actually moved today
    without cross-referencing the matrix sheets (Harish, 02-Aug-26).
    """
    df_ref = df_ref.copy()
    opens: list[float | None] = []
    closes: list[float | None] = []
    changes: list[float | None] = []

    for _, row in df_ref.iterrows():
        sym = str(row[config.COL_SYMBOL])
        day_df = candles.get(sym)
        if day_df is None or day_df.empty:
            opens.append(None)
            closes.append(None)
            changes.append(None)
            continue

        today_bars = day_df[day_df.index.date == trade_date]
        if today_bars.empty:
            opens.append(None)
            closes.append(None)
            changes.append(None)
            continue

        o = float(today_bars["open"].iloc[0])
        c = float(today_bars["close"].iloc[-1])
        opens.append(o)
        closes.append(c)
        # Raw ratio, NOT *100 -- excel_format.style_change_pct_column applies
        # Excel's native "0.00%" number format, which multiplies by 100 for
        # display on its own. Storing 0.0234 renders as 2.34%; storing 2.34
        # would render as 234.00%.
        changes.append(((c - o) / o) if o else None)

    df_ref[config.COL_DAY_OPEN] = opens
    df_ref[config.COL_DAY_CLOSE] = closes
    df_ref[config.COL_DAY_CHANGE_PCT] = changes
    return df_ref


if __name__ == "__main__":
    import broker_auth
    import file_mgmt
    import ist_clock

    paths.ensure_dirs()
    kite = broker_auth.initialize_zerodha()
    trade_date = ist_clock.today_ist()
    wb = file_mgmt.create_trade_file(trade_date, config.LIVE)
    ref = update_instrument_tokens(wb, kite, trade_date)
    print(ref.head())

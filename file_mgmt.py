"""
STEP 4 — Copy the source watchlist into a dated working workbook.

01_SourceFile.xlsx is the INPUT you maintain by hand. It is never written to.
Each run copies it to 'DD-Mon-YY FNO-L-TW-ALL.xlsx' (LIVE) or '-BT-TW-ALL' (BACKTEST) and
all output goes into the copy.

ONE SOURCE FILE, TWO SETS (02-Sep-26, Harish). The old pair of workbooks --
01_SourceFile_50.xlsx (48 symbols, LIVE) and 01_SourceFile_200.xlsx (206,
BACKTEST) -- is merged into this one file with a 'Live' Yes/No column. A
BACKTEST reads every row; a LIVE run reads only the Live=Yes rows (see
config.LIVE_RESTRICT_TO_LIVE_ROWS) so the 5-min loop still finishes inside
its window. Add a symbol once, mark it Yes when you're ready to trade it
for real -- there is no second file to keep in step.

Keeping the source read-only is not fussiness. Once a run starts appending
matrix sheets to the file it also reads its watchlist from, one bad run
corrupts the only clean copy you have.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pandas as pd

import config
import paths
from config import BACKTEST, LIVE


def create_trade_file(trade_date: date, mode: str,
                      overwrite: bool = False, path_fn=None) -> Path:
    """
    Copy 01_SourceFile.xlsx to the dated workbook and return its path.

    overwrite=False reuses an existing file for the same date and mode, which
    is what you want when re-running mid-session -- it preserves the sheets
    already built rather than wiping the morning's work.

    path_fn: override which naming scheme to use, e.g.
    paths.dated_workbook_path_macd for run_MACD.py. Defaults to
    paths.dated_workbook_path_tw_all (the original pipeline's naming), so
    every existing call site is unaffected.
    """
    if mode.upper() not in (LIVE, BACKTEST):
        raise ValueError(f"mode must be {LIVE!r} or {BACKTEST!r}, got {mode!r}")

    if not paths.SOURCE_FILE.exists():
        raise FileNotFoundError(
            f"source workbook missing: {paths.SOURCE_FILE}\n"
            f"This is the watchlist you maintain. It must sit at the project "
            f"root, one level above 02_Codes."
        )

    target = (path_fn or paths.dated_workbook_path_tw_all)(trade_date, mode)

    if target.exists() and not overwrite:
        print(f"[file] reusing existing workbook: {target.name}")
        return target

    if target.exists():
        backup = target.with_suffix(".bak.xlsx")
        shutil.copy2(target, backup)
        print(f"[file] existing workbook backed up -> {backup.name}")

    shutil.copy2(paths.SOURCE_FILE, target)
    print(f"[file] created {target.name} from {paths.SOURCE_FILE.name}")
    return target


def live_flags(df: pd.DataFrame) -> pd.Series:
    """
    Boolean 'this symbol may trade with real money' per row of a watchlist.

    Reads the merged source's 'Live' column (config.COL_LIVE). If the column
    is absent -- an older workbook, or a hand-made one -- every row counts
    as live, which keeps existing files working exactly as before rather
    than silently blocking every order.
    """
    if config.COL_LIVE not in df.columns:
        return pd.Series(True, index=df.index)
    return (df[config.COL_LIVE].astype(str).str.strip().str.upper()
            == config.LIVE_MARKER_YES)


def live_symbols(df: pd.DataFrame) -> set[str]:
    """The Live=Yes symbols of a watchlist, upper-cased. See live_flags."""
    flags = live_flags(df)
    return set(df.loc[flags, config.COL_SYMBOL].astype(str).str.strip().str.upper())


def read_reference_sheet(workbook: Path,
                         sheet: str | None = None,
                         mode: str | None = None) -> pd.DataFrame:
    """
    Load the watchlist. Validates before returning, because a silently empty
    or misnamed column here produces an entire run of missing data.

    mode: pass config.LIVE to get the live-tradeable subset only (the
    Live=Yes rows of the merged source, config.LIVE_RESTRICT_TO_LIVE_ROWS).
    Default None / BACKTEST returns every row. Deliberately explicit -- a
    module that forgets to pass it gets the FULL list, which costs time but
    never quietly shrinks a backtest.
    """
    sheet = sheet or config.WATCHLIST_SHEET

    try:
        df = pd.read_excel(workbook, sheet_name=sheet)
    except ValueError as exc:
        available = pd.ExcelFile(workbook).sheet_names
        raise ValueError(
            f"sheet {sheet!r} not found in {workbook.name}. Available: {available}"
        ) from exc

    required = [config.COL_SYMBOL, config.COL_EXPIRY, config.COL_INSTRUMENT_TYPE]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"{sheet!r} is missing required column(s) {missing}. "
            f"Found: {list(df.columns)}"
        )

    before = len(df)
    df = df[df[config.COL_SYMBOL].notna()].copy()
    df[config.COL_SYMBOL] = df[config.COL_SYMBOL].astype(str).str.strip().str.upper()
    df = df[df[config.COL_SYMBOL] != ""]

    dupes = df[config.COL_SYMBOL].duplicated().sum()
    if dupes:
        print(f"[file] WARNING: {dupes} duplicate symbol(s) in {sheet!r}, keeping first")
        df = df.drop_duplicates(subset=[config.COL_SYMBOL], keep="first")

    df = df.reset_index(drop=True)
    print(f"[file] {sheet!r}: {len(df)} symbols loaded "
          f"({before - len(df)} row(s) dropped as blank/duplicate)")

    # LIVE subset. Done AFTER validation and de-duplication so the counts
    # printed above always describe the file as maintained, not the slice.
    if (mode is not None and str(mode).upper() == LIVE
            and config.LIVE_RESTRICT_TO_LIVE_ROWS):
        if config.COL_LIVE not in df.columns:
            print(f"[file] WARNING: no {config.COL_LIVE!r} column in {sheet!r} "
                  f"-- running LIVE against all {len(df)} symbols. Add the "
                  f"column and mark the ones you actually trade.")
        else:
            df = df[live_flags(df)].reset_index(drop=True)
            print(f"[file] LIVE: restricted to {len(df)} symbol(s) marked "
                  f"{config.COL_LIVE}=Yes")
            if df.empty:
                raise ValueError(
                    f"no rows marked {config.COL_LIVE}=Yes in {sheet!r} of "
                    f"{workbook.name} -- a LIVE run has nothing to watch. "
                    f"Mark the symbols you trade, or set "
                    f"config.LIVE_RESTRICT_TO_LIVE_ROWS = False."
                )
    return df


def write_sheet(workbook: Path, sheet_name: str, df: pd.DataFrame,
                index: bool = False) -> None:
    """
    Write or replace one sheet, leaving every other sheet intact.

    openpyxl mode='a' with if_sheet_exists='replace' is the only combination
    that does not silently destroy the rest of the workbook.
    """
    with pd.ExcelWriter(workbook, engine="openpyxl", mode="a",
                        if_sheet_exists="replace") as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=index)
    print(f"[file] wrote sheet {sheet_name!r} ({len(df)} rows) -> {workbook.name}")


def list_sheets(workbook: Path) -> list[str]:
    return pd.ExcelFile(workbook).sheet_names


def sheet_has_rows(workbook: Path, sheet_name: str) -> bool:
    """
    True if `sheet_name` exists and already has at least one data row.

    Added 24-Aug-26 (Harish -- restart duplicate-order bug). Orders/
    Rejected/etc. were being reset to an empty skeleton on EVERY call to
    setup_live_day(), not just the pre-market one -- a mid-day restart
    (power fluctuation) called it again, wiped the morning's already-
    written Orders/Rejected rows off disk, and the live loop then had no
    record left that those signals had already been decided. Guard any
    "write an empty skeleton" call with this first, so it only ever fires
    on the genuine first run of the day.
    """
    if not workbook.exists():
        return False
    try:
        df = pd.read_excel(workbook, sheet_name=sheet_name)
    except (ValueError, KeyError):
        return False
    return len(df) > 0


if __name__ == "__main__":
    import ist_clock

    paths.ensure_dirs()
    problems = paths.verify_layout()
    if problems:
        for p in problems:
            print("  -", p)
        raise SystemExit(1)

    wb = create_trade_file(ist_clock.today_ist(), LIVE)
    ref = read_reference_sheet(wb)
    print(f"\nsheets: {list_sheets(wb)}")
    print(ref.head())

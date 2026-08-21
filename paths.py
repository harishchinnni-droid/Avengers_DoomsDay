"""
STEP 0a — Path discovery. Nothing is hardcoded to a drive letter.

How it works: this file lives inside 02_Codes. BASE_DIR is simply its parent.
Move the whole tree to another machine, another drive, another folder name --
the code still finds itself. No edits, no environment variables.

    F:\\02_Avengers_13-Aug-26\\02_Codes\\paths.py
    ^------------ BASE_DIR --------------^

Every other module imports paths from here. If you ever see an open() with a
literal "F:\\" in this project, it's a bug.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# BASE DIRECTORY
# --------------------------------------------------------------------------
CODE_DIR: Path = Path(__file__).resolve().parent
BASE_DIR: Path = CODE_DIR.parent

# --------------------------------------------------------------------------
# STANDARD SUBFOLDERS
# --------------------------------------------------------------------------
JSON_DIR: Path = BASE_DIR / "01_JSON_Files"
HIST_DIR: Path = BASE_DIR / "03_Historical_Data"
OPTION_HIST_DIR: Path = BASE_DIR / "04_Option_Historical_Data"
LOG_DIR: Path = BASE_DIR / "05_Logs"
# One CSV per trading day (oi_log.py) -- deliberately its own folder so it's
# a simple thing to copy or sync between machines (Harish works from an
# office laptop and a personal one) without dragging the whole project.
OI_DATA_DIR: Path = BASE_DIR / "05_OI_Data"
# Point-in-time option-chain capture (quote_history.py, 09-Aug-26): which
# contracts existed and what their quotes were, per symbol per day, so a
# later BACKTEST can replay a date exactly as LIVE saw it instead of asking
# today's scrip master/quotes about a past date. Same portability reasoning
# as OI_DATA_DIR -- one CSV per day, easy to copy between machines.
QUOTE_HIST_DIR: Path = BASE_DIR / "06_Quote_History"

SOURCE_FILE: Path = BASE_DIR / "01_SourceFile.xlsx"

# Credential files (structure already established in 01_JSON_Files)
ANGEL_CRED_FILE: Path = JSON_DIR / "harish_angel_one.json"
ZERODHA_CRED_FILE: Path = JSON_DIR / "harish_zerodha.json"

# LIVE TRADING KILL SWITCH (20-Aug-26). If this file exists, live_orders.py
# refuses to place any NEW entry order -- create it by hand (an empty file
# is enough, e.g. `type nul > STOP_LIVE_TRADING.flag` on Windows) to halt
# new live trades instantly without touching code or restarting the running
# process. Does not touch positions already open -- those still need their
# exits managed and placed for real. Delete the file to resume.
LIVE_KILL_SWITCH_FILE: Path = BASE_DIR / "STOP_LIVE_TRADING.flag"

# LIVE POSITION FAST-TRACK SNAPSHOT (20-Aug-26). Rewritten wholesale every
# LIVE_FAST_TRACK_INTERVAL_SECS by order_engine.fast_track_live_positions --
# one row per currently OPEN real position (never the whole watchlist), so
# Harish can watch SL/Target/TSL levels move without opening the dated
# workbook (which only updates once per 5-min cycle, and Excel locks the
# file for reading while it's mid-write anyway). CSV, not xlsx -- same
# reasoning as oi_log.py/quote_history.py: a CSV survives a torn read from
# being rewritten every 30-60s by a process that's also busy elsewhere; an
# xlsx is a zip archive that doesn't.
LIVE_STATUS_FILE: Path = BASE_DIR / "Live_Position_Tracker.csv"

# Caches, all date-keyed so a same-day re-run reuses instead of re-fetching
ZERODHA_TOKEN_CACHE: Path = JSON_DIR / "zerodha_access_token.json"
ANGEL_TOKEN_CACHE: Path = JSON_DIR / "angel_one_access_token.json"
ANGEL_SCRIP_MASTER: Path = JSON_DIR / "OpenAPIScripMaster.json"
INSTRUMENT_TOKEN_CACHE: Path = JSON_DIR / "instrument_tokens.json"

_REQUIRED_DIRS = (JSON_DIR, HIST_DIR, OPTION_HIST_DIR, LOG_DIR, OI_DATA_DIR,
                  QUOTE_HIST_DIR)


def ensure_dirs() -> None:
    """Create any missing working folders. Safe to call repeatedly."""
    for d in _REQUIRED_DIRS:
        d.mkdir(parents=True, exist_ok=True)


def verify_layout(require_source: bool = True) -> list[str]:
    """
    Return a list of problems with the folder layout. Empty list means fine.

    Called at pipeline startup so a misplaced install fails immediately with a
    readable message, rather than three modules later with a FileNotFoundError
    on a path nobody can interpret.
    """
    problems: list[str] = []

    # 09-Aug-26: added '05_Codes' -- this repo (spiderman-fno-pipeline) was
    # cloned into a folder by that name, separate from the 04_Codes checkout
    # of the other repo (spiderman_newbrandday). The check itself only
    # exists to fail fast with a readable message instead of a
    # FileNotFoundError three modules later -- it doesn't care what the
    # folder is named beyond confirming BASE_DIR (its parent) is really the
    # project root. If this project ever lives in yet another folder name,
    # add it here rather than removing the check.
    if CODE_DIR.name not in ("02_Codes", "04_Codes", "05_NewCode", "05_Codes", "03_Codes"):
        problems.append(
            f"expected this file to live in a folder named '02_Codes', "
            f"'04_Codes', '05_NewCode', '05_Codes' or '03_Codes', found "
            f"'{CODE_DIR.name}'. BASE_DIR resolved to {BASE_DIR} -- check "
            f"that is really your project root."
        )

    if require_source and not SOURCE_FILE.exists():
        problems.append(f"source workbook not found: {SOURCE_FILE}")

    if not JSON_DIR.exists():
        problems.append(f"credentials folder not found: {JSON_DIR}")
    else:
        if not ANGEL_CRED_FILE.exists():
            problems.append(f"Angel One credentials not found: {ANGEL_CRED_FILE}")
        if not ZERODHA_CRED_FILE.exists():
            problems.append(f"Zerodha credentials not found: {ZERODHA_CRED_FILE}")

    return problems


def describe() -> str:
    """Human-readable path dump for the startup banner."""
    lines = [
        "Resolved paths",
        f"  BASE_DIR        : {BASE_DIR}",
        f"  CODE_DIR        : {CODE_DIR}",
        f"  JSON_DIR        : {JSON_DIR}",
        f"  HIST_DIR        : {HIST_DIR}",
        f"  OPTION_HIST_DIR : {OPTION_HIST_DIR}",
        f"  SOURCE_FILE     : {SOURCE_FILE}"
        + ("" if SOURCE_FILE.exists() else "   <-- MISSING"),
    ]
    return "\n".join(lines)


def dated_workbook_path_tw_all(trade_date, mode: str) -> Path:
    """
    Output workbook name: 'DD-Mon-YY FNO-L-TW-ALL.xlsx' for LIVE, '-BT-TW-ALL' for BACKTEST.
    Example: '31-Jul-26 FNO-L-TW-ALL.xlsx'
    """
    suffix = "L" if str(mode).upper() == "LIVE" else "BT"
    stamp = trade_date.strftime("%d-%b-%y")
    return BASE_DIR / f"{stamp} FNO-{suffix}-TW-ALL.xlsx"


def dated_workbook_path_macd(trade_date, mode: str) -> Path:
    """
    Output workbook name for run_MACD.py (MACD in place of TW ALL):
    'DD-Mon-YY FNO-L-MACD.xlsx' for LIVE, '-BT-MACD' for BACKTEST.

    Deliberately a different suffix from dated_workbook_path_tw_all() above,
    so the two pipelines can be run on the same date without one overwriting
    the other's output.
    """
    suffix = "L" if str(mode).upper() == "LIVE" else "BT"
    stamp = trade_date.strftime("%d-%b-%y")
    return BASE_DIR / f"{stamp} FNO-{suffix}-MACD.xlsx"


def dated_workbook_path_ema_pivot(trade_date, mode: str) -> Path:
    """
    Output workbook name for run_EMA_PIVOT.py (EMA10 + Pivot confluence):
    'DD-Mon-YY FNO-L-EMA-PIVOT.xlsx' for LIVE, '-BT-EMA-PIVOT' for BACKTEST.

    Deliberately a different suffix from the other two pipelines' naming
    functions, so all three can be run on the same date without any one
    overwriting another's output.
    """
    suffix = "L" if str(mode).upper() == "LIVE" else "BT"
    stamp = trade_date.strftime("%d-%b-%y")
    return BASE_DIR / f"{stamp} FNO-{suffix}-EMA-PIVOT.xlsx"


if __name__ == "__main__":
    ensure_dirs()
    print(describe())
    issues = verify_layout()
    if issues:
        print("\nLayout problems:")
        for p in issues:
            print("  -", p)
        sys.exit(1)
    print("\nLayout OK.")

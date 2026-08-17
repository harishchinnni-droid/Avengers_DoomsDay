"""
STEP 3 — NSE trading calendar, and the LIVE / BACKTEST decision.

THE RULE THAT MATTERS
---------------------
Mode is chosen explicitly by the user and passed through every function call
in the pipeline. It is NEVER inferred from whether a date is in the past.
That inference is exactly how a backtest ends up placing a live order: a
re-run on a past date looks like history to the code and like a live session
to the broker API. Mode is an argument, always.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

import ist_clock
from config import BACKTEST, LIVE

# --------------------------------------------------------------------------
# NSE trading holidays.
#
# HARDCODED AND THEREFORE PERISHABLE. NSE publishes the list annually and
# amends it for unscheduled closures. Verify against the NSE circular each
# January. A wrong entry here means either a wasted run or, worse, a live
# session on a day the market never opened.
# --------------------------------------------------------------------------
NSE_HOLIDAYS: dict[int, list[str]] = {
    2026: [
        # TODO: replace with the official NSE 2026 holiday circular before
        # relying on this. Entries below are placeholders and are NOT verified.
        "2026-01-26",  # Republic Day
        "2026-08-15",  # Independence Day
        "2026-10-02",  # Gandhi Jayanti
        "2026-12-25",  # Christmas
    ],
}

HOLIDAYS_VERIFIED = False  # flip to True only after checking the NSE circular


def _holiday_set(year: int) -> set[date]:
    return {date.fromisoformat(s) for s in NSE_HOLIDAYS.get(year, [])}


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # 5 = Saturday, 6 = Sunday


def is_trading_holiday(d: date) -> bool:
    """True if NSE is closed on this date (weekend or listed holiday)."""
    return is_weekend(d) or d in _holiday_set(d.year)


def is_trading_day(d: date) -> bool:
    return not is_trading_holiday(d)


def holiday_reason(d: date) -> str:
    if is_weekend(d):
        return f"{d:%A}"
    if d in _holiday_set(d.year):
        return "NSE holiday"
    return ""


def trading_days_between(start: date, end: date) -> list[date]:
    """Every trading day in [start, end] inclusive, weekends/holidays removed."""
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    out, cur = [], start
    while cur <= end:
        if is_trading_day(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def previous_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    raise RuntimeError(f"no trading day found in the 30 days before {d}")


# --------------------------------------------------------------------------
# run configuration
# --------------------------------------------------------------------------
# Input date format (Harish, 01-Aug-26): 31-Jul-26, matching the output
# workbook names. Alternatives are still accepted so an old habit does not
# cost a retry, but the prompt only advertises the one.
DATE_FORMAT = "%d-%b-%y"
DATE_FORMAT_HINT = "DD-Mon-YY"
_ACCEPTED_FORMATS = ("%d-%b-%y", "%d-%b-%Y", "%d-%m-%Y", "%d-%m-%y", "%Y-%m-%d")


def parse_date(raw: str) -> date | None:
    """Parse a user-typed date. Returns None if nothing matches."""
    raw = raw.strip()
    if not raw:
        return None
    for fmt in _ACCEPTED_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def format_date(d: date) -> str:
    """31-Jul-26 -- the same stamp used in the output workbook names."""
    return d.strftime(DATE_FORMAT)


def _prompt_date(label: str) -> date:
    while True:
        raw = input(f"  {label} ({DATE_FORMAT_HINT}, e.g. 31-Jul-26): ").strip()
        parsed = parse_date(raw)
        if parsed is not None:
            return parsed
        print(f"    Bad format. Use {DATE_FORMAT_HINT}, e.g. 31-Jul-26")


def get_run_config(mode: str | None = None,
                   start: date | None = None,
                   end: date | None = None) -> tuple[str, list[date]]:
    """
    Return (mode, [dates]).

    Pass arguments to run non-interactively; omit them to be prompted.
    LIVE refuses to run on a non-trading day and exits rather than
    pretending. That refusal is deliberate: a LIVE run on a closed market
    silently produces empty data that looks like a flat session.
    """
    if not HOLIDAYS_VERIFIED:
        print("[calendar] WARNING: holiday list is unverified placeholder data. "
              "Check the NSE circular before trusting a run near a holiday.")

    if mode is None:
        print("\nSelect mode:")
        print("  1 = LIVE      (today only)")
        print("  2 = BACKTEST  (a date range)")
        choice = input("Choice (1/2): ").strip()
        mode = {"1": LIVE, "2": BACKTEST}.get(choice)
        if mode is None:
            print("[calendar] invalid choice")
            sys.exit(1)

    mode = mode.upper()
    if mode not in (LIVE, BACKTEST):
        raise ValueError(f"mode must be {LIVE!r} or {BACKTEST!r}, got {mode!r}")

    if mode == LIVE:
        today = ist_clock.today_ist()
        if is_trading_holiday(today):
            print(f"[calendar] {format_date(today)} is not a trading day "
                  f"({holiday_reason(today)}). LIVE mode refused.")
            sys.exit(1)
        print(f"[calendar] LIVE mode -- {format_date(today)}")
        return LIVE, [today]

    if start is None:
        print("\nBACKTEST date range:")
        start = _prompt_date("Start date")
    if end is None:
        end = _prompt_date("End date")

    if start > end:
        print(f"[calendar] start {format_date(start)} is after end {format_date(end)}")
        sys.exit(1)

    dates = trading_days_between(start, end)
    if not dates:
        print(f"[calendar] no trading days between {format_date(start)} "
              f"and {format_date(end)}")
        sys.exit(1)

    skipped = (end - start).days + 1 - len(dates)
    print(f"[calendar] BACKTEST mode -- {len(dates)} trading day(s) queued "
          f"({format_date(start)} to {format_date(end)}), "
          f"{skipped} non-trading day(s) skipped")
    return BACKTEST, dates


if __name__ == "__main__":
    today = ist_clock.today_ist()
    print(f"today {today:%d-%b-%Y} ({today:%A}) -> trading day: {is_trading_day(today)}")
    jan = date(2026, 1, 1)
    print(f"trading days in Jan 2026: {len(trading_days_between(jan, date(2026, 1, 31)))}")
    print(f"previous trading day: {previous_trading_day(today):%d-%b-%Y}")

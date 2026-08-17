"""
STEP 0b — Time. Every scheduling decision in this system depends on it.

Why this module exists instead of datetime.now(): the machine's local clock
may not be IST, and naive datetimes silently compare wrong against
tz-aware broker timestamps. Everything here is IST and tz-aware. Nothing
elsewhere in the project should call datetime.now() directly.

On Windows the timezone database is not bundled -- `pip install tzdata` is
required or ZoneInfo("Asia/Kolkata") raises. bootstrap.py handles that.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    raise RuntimeError("Python 3.9+ required for zoneinfo")

try:
    IST = ZoneInfo("Asia/Kolkata")
except Exception as exc:  # tzdata missing on Windows
    raise RuntimeError(
        "Could not load the Asia/Kolkata timezone. On Windows run:\n"
        "    pip install tzdata\n"
        "The timezone database is not bundled with Python on Windows and "
        "every scheduling decision in this system depends on it."
    ) from exc

# NSE equity/F&O session
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    """Current time, tz-aware, IST."""
    return datetime.now(IST)


def today_ist() -> date:
    """Today's date in IST, regardless of machine timezone."""
    return now_ist().date()


def to_ist(dt: datetime) -> datetime:
    """Convert any datetime to IST. Naive input is assumed to already be IST."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def combine_ist(d: date, t: time) -> datetime:
    """Build a tz-aware IST datetime from a date and a time."""
    return datetime.combine(d, t, tzinfo=IST)


def session_bounds(d: date) -> tuple[datetime, datetime]:
    """(open, close) as tz-aware IST datetimes for the given date."""
    return combine_ist(d, MARKET_OPEN), combine_ist(d, MARKET_CLOSE)


def is_market_hours(dt: datetime | None = None) -> bool:
    """True if the given moment is inside the NSE session. Ignores holidays."""
    dt = to_ist(dt) if dt else now_ist()
    return MARKET_OPEN <= dt.time() <= MARKET_CLOSE


def candle_slots(d: date, interval_minutes: int = 5,
                 end: time = time(15, 15)) -> list[str]:
    """
    Column headers for a matrix sheet: ['09:15', '09:20', ... '15:15'].

    Defaults reproduce the 73 columns in the sample workbook. The label is the
    candle's OPEN time, which matters: the '09:15' column holds the candle
    covering 09:15:00-09:19:59, and that candle is not usable until 09:20.
    """
    slots: list[str] = []
    cur = combine_ist(d, MARKET_OPEN)
    stop = combine_ist(d, end)
    step = timedelta(minutes=interval_minutes)
    while cur <= stop:
        slots.append(cur.strftime("%H:%M"))
        cur += step
    return slots


def last_closed_candle_open(dt: datetime | None = None,
                            interval_minutes: int = 5) -> datetime:
    """
    Open time of the most recent FULLY CLOSED candle.

    This is the single most important function for avoiding repainting. At
    09:22 the 09:20 candle is still forming; the last closed one opened at
    09:15. Signals must only ever be computed from candles at or before this
    timestamp.
    """
    dt = to_ist(dt) if dt else now_ist()
    minutes = dt.hour * 60 + dt.minute
    floored = (minutes // interval_minutes) * interval_minutes
    current_open = dt.replace(hour=floored // 60, minute=floored % 60,
                              second=0, microsecond=0)
    return current_open - timedelta(minutes=interval_minutes)


if __name__ == "__main__":
    n = now_ist()
    print("now_ist            :", n.strftime("%Y-%m-%d %H:%M:%S %Z"))
    print("today_ist          :", today_ist())
    print("in market hours    :", is_market_hours())
    print("last closed candle :", last_closed_candle_open().strftime("%H:%M"))
    slots = candle_slots(today_ist())
    print(f"candle slots       : {len(slots)} cols, {slots[0]} -> {slots[-1]}")

"""
Partial translation of Pivots.txt (TradingView built-in "Pivot Points
Standard", Pine v6, //@version=6, shorttitle "Pivots").

WHY A SEPARATE FILE
--------------------
Harish uploaded Pivots.txt and asked for its own code, same pattern as
rsi.py / macd.py / ema.py.

WHY "PARTIAL"
-------------
The script supports 6 pivot TYPES (Traditional, Fibonacci, Woodie, Classic,
DM, Camarilla), multiple anchor timeframes, and 5 levels of support/
resistance each (R1-R5/S1-S5) -- all driven by TradingView's
request.security() pulling data from a higher timeframe, which has no
direct equivalent here (this project computes everything from the 5-min
candles it already downloaded, not a separate daily feed).

Harish's rule ("crossing/above EMA10 AND above Pivot Line") only reads the
central Pivot (P), using the script's own defaults: Type="Traditional",
Pivots timeframe="Auto" (resolves to daily on an intraday chart), Use
daily-based values=True. That is what's translated here -- the Traditional
floor-trader pivot, computed once per day from the PRIOR trading day's
daily high/low/close, held constant through the current session. R1/S1
are included too (cheap, same formula family) as reference columns for
the sheet, but the RULE itself only reads P.

TRANSLATION NOTE
-----------------
No repainting risk in this subset: the pivot for TODAY is fixed from
YESTERDAY's already-closed daily bar the moment today's first candle
opens, and never changes intraday.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def daily_pivot_levels(df: pd.DataFrame) -> pd.DataFrame:
    """
    Traditional floor-trader pivot, daily-based:

        P  = (prevDayHigh + prevDayLow + prevDayClose) / 3
        R1 = 2*P - prevDayLow
        S1 = 2*P - prevDayHigh

    Held CONSTANT for every intraday bar of the CURRENT session --
    computed once per day from the PRIOR trading day's daily H/L/C, never
    the current (still-forming) day's. `df` must have a tz-aware
    DatetimeIndex and 'high'/'low'/'close' columns; may span many days.

    Returns ['P', 'R1', 'S1'], NaN for any session that has no prior day
    in `df` (the first day of whatever history was passed in).
    """
    if df.empty:
        return pd.DataFrame(columns=["P", "R1", "S1"], index=df.index)

    session = pd.Series(df.index.date, index=df.index)
    daily = df.groupby(session).agg(high=("high", "max"), low=("low", "min"),
                                    close=("close", "last")).sort_index()

    prev = daily.shift(1)
    prev_p = (prev["high"] + prev["low"] + prev["close"]) / 3.0
    prev_r1 = 2.0 * prev_p - prev["low"]
    prev_s1 = 2.0 * prev_p - prev["high"]

    return pd.DataFrame({
        "P": session.map(prev_p),
        "R1": session.map(prev_r1),
        "S1": session.map(prev_s1),
    }, index=df.index)


if __name__ == "__main__":
    # Two sessions, 09:15-09:35 each -- just enough to check the day-2
    # pivot is derived purely from day 1's H/L/C, not day 2's own bars.
    idx1 = pd.date_range("2026-08-13 09:15", periods=5, freq="5min", tz="Asia/Kolkata")
    idx2 = pd.date_range("2026-08-14 09:15", periods=5, freq="5min", tz="Asia/Kolkata")
    idx = idx1.append(idx2)

    day1 = pd.DataFrame({"high": [105, 106, 104, 103, 102],
                         "low": [100, 101, 99, 98, 97],
                         "close": [102, 103, 101, 100, 99]}, index=idx1)
    day2 = pd.DataFrame({"high": [999, 999, 999, 999, 999],
                         "low": [1, 1, 1, 1, 1],
                         "close": [500, 500, 500, 500, 500]}, index=idx2)
    df = pd.concat([day1, day2])

    levels = daily_pivot_levels(df)
    print(levels)

    day1_hi, day1_lo, day1_cl = 106.0, 97.0, 99.0   # day 1's actual H/L/C
    expected_p = (day1_hi + day1_lo + day1_cl) / 3.0
    expected_r1 = 2 * expected_p - day1_lo
    expected_s1 = 2 * expected_p - day1_hi

    day2_levels = levels.loc[idx2]
    assert (day2_levels["P"] == expected_p).all(), "day 2 pivot must come from day 1's H/L/C"
    assert (day2_levels["R1"] == expected_r1).all()
    assert (day2_levels["S1"] == expected_s1).all()
    assert levels.loc[idx1, "P"].isna().all(), "day 1 has no prior day -- must be NaN"

    print("\npivot_points.py self-check passed")

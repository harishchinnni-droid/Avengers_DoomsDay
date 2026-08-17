"""
Standalone translation of MACD.txt (TradingView built-in "Moving Average
Convergence Divergence", Pine v6, //@version=6, shorttitle "MACD").

WHY A SEPARATE FILE
--------------------
Not currently used anywhere in the pipeline -- indicators.py / config.py
have no MACD today. This is a free-standing module for whatever comes
next (a new confluence component, a standalone study, etc.), added
because Harish uploaded MACD.txt and asked for its own code, same as
rsi.py. Not used by run_TW_ALL.py; matrix_sheets_v2.py imports this module
for run_MACD.py's 4th confluence component.

TRANSLATION NOTES
------------------
No repainting risk here -- every line (fast MA, slow MA, MACD, signal,
histogram) only looks backward. Safe to use bar-by-bar in a live loop.

Pine ta.ema(x, n) == pandas x.ewm(span=n, adjust=False).mean()
Pine ta.sma(x, n) == pandas x.rolling(n).mean()
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length).mean()


def _ma(series: pd.Series, length: int, ma_type: str) -> pd.Series:
    """Script's ma(source, length, maType) => switch EMA/SMA."""
    if ma_type == "EMA":
        return _ema(series, length)
    if ma_type == "SMA":
        return _sma(series, length)
    raise ValueError(f"unknown ma_type {ma_type!r}, expected 'EMA' or 'SMA'")


def macd(source: pd.Series, fast_length: int = 12, slow_length: int = 26,
         signal_length: int = 9, osc_ma_type: str = "EMA",
         signal_ma_type: str = "EMA") -> pd.DataFrame:
    """
    Direct translation of the script's calculation block:
        maFast = ma(source, fastLen, oscType)
        maSlow = ma(source, slowLen, oscType)
        macd   = maFast - maSlow
        signal = ma(macd, sigLen, sigType)
        hist   = macd - signal

    Defaults (12, 26, 9, EMA, EMA) match the script's own input defaults.
    Returns columns ['macd', 'signal', 'hist'].
    """
    source = source.astype(float)
    ma_fast = _ma(source, fast_length, osc_ma_type)
    ma_slow = _ma(source, slow_length, osc_ma_type)
    macd_line = ma_fast - ma_slow
    signal_line = _ma(macd_line, signal_length, signal_ma_type)
    hist = macd_line - signal_line

    return pd.DataFrame({
        "macd": macd_line,
        "signal": signal_line,
        "hist": hist,
    })


def histogram_color(hist: pd.Series) -> pd.Series:
    """
    Script's hColor logic, as a category label instead of a hex colour --
    useful for reading the histogram's state programmatically:
        hist >= 0 & rising  -> "teal"   (#26a69a)
        hist >= 0 & falling -> "light_red"  (#b2dfdb is actually a light
                                teal in the script -- see note below)
        hist <  0 & rising  -> "light_red"  (#ffcdd2)
        hist <  0 & falling -> "red"    (#ff5252)

    NOTE on the script's own colour choice: for hist >= 0 the "falling"
    branch uses #b2dfdb, which is a PALE TEAL, not a pale red -- i.e. even
    a weakening positive histogram is still shown in a green family, and
    only a hist < 0 bar ever turns red. Reproduced faithfully below rather
    than "fixed", since the point is to match the script, not improve it.
    """
    rising = hist > hist.shift(1)
    out = pd.Series("", index=hist.index, dtype=object)
    out[(hist >= 0) & rising] = "teal"          # #26a69a
    out[(hist >= 0) & ~rising] = "pale_teal"    # #b2dfdb
    out[(hist < 0) & rising] = "pale_red"       # #ffcdd2
    out[(hist < 0) & ~rising] = "red"           # #ff5252
    return out


def alert_conditions(hist: pd.Series) -> pd.DataFrame:
    """
    Script's two alertcondition() calls, as boolean series:
        'Rising to falling' : hist[1] >= 0 and hist < 0   (crossed down through zero)
        'Falling to rising' : hist[1] <= 0 and hist > 0   (crossed up through zero)
    """
    prev = hist.shift(1)
    rising_to_falling = (prev >= 0) & (hist < 0)
    falling_to_rising = (prev <= 0) & (hist > 0)
    return pd.DataFrame({
        "rising_to_falling": rising_to_falling.fillna(False),
        "falling_to_rising": falling_to_rising.fillna(False),
    })


if __name__ == "__main__":
    idx = pd.date_range("2026-08-01 09:15", periods=200, freq="5min")
    rng = np.random.default_rng(5)
    close = pd.Series(1000 + np.cumsum(rng.normal(0, 1.2, 200)), index=idx)

    m = macd(close)
    print("columns:", list(m.columns))
    print(f"macd range: {m['macd'].dropna().min():.2f} to {m['macd'].dropna().max():.2f}")

    colors = histogram_color(m["hist"])
    print(f"histogram colour counts:\n{colors.value_counts()}")

    alerts = alert_conditions(m["hist"])
    print(f"zero-cross down: {int(alerts['rising_to_falling'].sum())}, "
          f"zero-cross up: {int(alerts['falling_to_rising'].sum())}")

    # sanity: hist == macd - signal everywhere it's defined
    diff = (m["hist"] - (m["macd"] - m["signal"])).dropna()
    assert (diff.abs() < 1e-9).all(), "hist must equal macd - signal"

    print("\nmacd.py self-check passed")

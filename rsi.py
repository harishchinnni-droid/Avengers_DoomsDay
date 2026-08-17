"""
Standalone translation of RSI.txt (TradingView built-in "Relative Strength
Index", Pine v6, //@version=6, shorttitle "RSI").

WHY A SEPARATE FILE
--------------------
indicators.py already has an RSI (rsi_wilder / rsi_multi_length, dispatched
via config.RSI_SOURCE) wired into the live pipeline's signal logic. This
file is NOT that -- it exists because Harish uploaded RSI.txt separately and
asked for its own standalone code. rsi_wilder() in indicators.py is actually
the same formula as this script's core RSI line (both are Wilder RMA-based),
but this module also covers the smoothing-MA options and the divergence
logic that indicators.py does not implement. Nothing here is imported by
run_TW_ALL.py or run_MACD.py -- it is a free-standing reference module.

TRANSLATION NOTES / REPAINTING WARNING
---------------------------------------
The core RSI line (rsi()) is safe to use bar-by-bar in a live loop: it only
looks backward.

The divergence detector (find_divergences()) is NOT safe to treat as a live
signal without accounting for repainting:
    - ta.pivotlow / ta.pivothigh with a lookback RIGHT of 5 can only confirm
      a pivot 5 bars AFTER it happened -- by construction it needs future
      bars relative to the pivot bar. A "divergence found at bar i" is only
      known at bar i + lookbackRight. Using it as if it were known exactly
      at bar i in a backtest is lookahead bias.
    - This is exactly the class of bug flagged in the algo-trading skill's
      indicator-translation guidance. find_divergences() returns the
      CONFIRMED time (pivot bar + lookbackRight) alongside the pivot bar
      itself so a backtest can act on the confirmation time, not the pivot
      time.

Pine's ta.rma(x, n) == pandas .ewm(alpha=1/n, adjust=False).mean() -- same
identity indicators.py's rsi_wilder() already relies on.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# core RSI  (script lines 8-11)
# --------------------------------------------------------------------------
def rma(series: pd.Series, length: int) -> pd.Series:
    """Pine ta.rma -- Wilder's smoothed moving average."""
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def rsi(source: pd.Series, length: int = 14) -> pd.Series:
    """
    Core RSI line, identical formula to the script:
        change = ta.change(source)
        up     = rma(max(change, 0), length)
        down   = rma(-min(change, 0), length)
        rsi    = down == 0 ? 100 : up == 0 ? 0 : 100 - 100/(1 + up/down)
    """
    change = source.astype(float).diff()
    up = rma(change.clip(lower=0), length)
    down = rma((-change.clip(upper=0)), length)

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = up / down
        out = 100.0 - 100.0 / (1.0 + rs)

    out = out.where(down != 0, 100.0)   # down == 0 -> 100
    out = out.where(~((down == 0) & (up == 0)), np.nan)  # both 0 on bar 1 -> na, matches Pine's na start
    out = out.where(up != 0, 0.0).where((down != 0) | (up != 0), out)  # up == 0 (and down != 0) -> 0
    return out


# --------------------------------------------------------------------------
# smoothing MA options  (script lines 25-44, the "Smoothing" group)
# --------------------------------------------------------------------------
def _wma(series: pd.Series, length: int) -> pd.Series:
    weights = np.arange(1, length + 1, dtype=float)
    return series.rolling(length).apply(
        lambda w: float(np.dot(w, weights) / weights.sum()), raw=True)


def _vwma(series: pd.Series, volume: pd.Series, length: int) -> pd.Series:
    pv = (series * volume).rolling(length).sum()
    v = volume.rolling(length).sum()
    return pv / v.replace(0, np.nan)


def rsi_smoothing_ma(rsi_series: pd.Series, ma_type: str = "SMA",
                     length: int = 14, volume: pd.Series | None = None,
                     bb_mult: float = 2.0) -> dict[str, pd.Series]:
    """
    ma() dispatch from the script, applied to the RSI line itself.

    ma_type: "None" | "SMA" | "SMA + Bollinger Bands" | "EMA" | "SMMA (RMA)"
             | "WMA" | "VWMA"

    Returns a dict with key "ma" always, plus "bb_upper"/"bb_lower" when
    ma_type is the Bollinger Bands variant. VWMA needs `volume`.
    """
    if ma_type == "None":
        return {"ma": pd.Series(np.nan, index=rsi_series.index)}

    if ma_type in ("SMA", "SMA + Bollinger Bands"):
        ma = rsi_series.rolling(length).mean()
    elif ma_type == "EMA":
        ma = rsi_series.ewm(span=length, adjust=False).mean()
    elif ma_type == "SMMA (RMA)":
        ma = rma(rsi_series, length)
    elif ma_type == "WMA":
        ma = _wma(rsi_series, length)
    elif ma_type == "VWMA":
        if volume is None:
            raise ValueError("VWMA requires the `volume` series")
        ma = _vwma(rsi_series, volume, length)
    else:
        raise ValueError(f"unknown ma_type {ma_type!r}")

    out = {"ma": ma}
    if ma_type == "SMA + Bollinger Bands":
        stdev = rsi_series.rolling(length).std(ddof=0) * bb_mult
        out["bb_upper"] = ma + stdev
        out["bb_lower"] = ma - stdev
    return out


# --------------------------------------------------------------------------
# divergence  (script lines 49-136)
# --------------------------------------------------------------------------
def _pivot_low(series: pd.Series, left: int, right: int) -> pd.Series:
    """
    True at bar i if series[i] is the minimum over [i-left, i+right].
    NEEDS `right` future bars, so this is only knowable at i+right --
    see the module docstring's repainting warning.
    """
    n = len(series)
    out = np.zeros(n, dtype=bool)
    vals = series.to_numpy()
    for i in range(left, n - right):
        window = vals[i - left: i + right + 1]
        if np.isnan(vals[i]):
            continue
        if vals[i] == np.nanmin(window) and np.sum(window == vals[i]) == 1:
            out[i] = True
    return pd.Series(out, index=series.index)


def _pivot_high(series: pd.Series, left: int, right: int) -> pd.Series:
    n = len(series)
    out = np.zeros(n, dtype=bool)
    vals = series.to_numpy()
    for i in range(left, n - right):
        window = vals[i - left: i + right + 1]
        if np.isnan(vals[i]):
            continue
        if vals[i] == np.nanmax(window) and np.sum(window == vals[i]) == 1:
            out[i] = True
    return pd.Series(out, index=series.index)


def find_divergences(close: pd.Series, low: pd.Series, high: pd.Series,
                     rsi_series: pd.Series, lookback_left: int = 5,
                     lookback_right: int = 5, range_lower: int = 5,
                     range_upper: int = 60) -> pd.DataFrame:
    """
    Regular bullish / bearish divergence, same rule as the script:

        Regular Bullish: RSI makes a Higher Low while price makes a Lower Low
        Regular Bearish: RSI makes a Lower High while price makes a Higher High

    Returns a DataFrame indexed like `close` with two boolean columns,
    'bull_confirmed' and 'bear_confirmed', TRUE on the bar where the
    divergence is CONFIRMED (pivot bar + lookback_right) -- not on the pivot
    bar itself. Use the confirmed bar as the earliest point a backtest may
    act on this signal.
    """
    n = len(close)
    pl = _pivot_low(rsi_series, lookback_left, lookback_right)
    ph = _pivot_high(rsi_series, lookback_left, lookback_right)

    bull_confirmed = np.zeros(n, dtype=bool)
    bear_confirmed = np.zeros(n, dtype=bool)

    pl_idx = list(np.where(pl.to_numpy())[0])
    ph_idx = list(np.where(ph.to_numpy())[0])
    rsi_v = rsi_series.to_numpy()
    low_v = low.to_numpy()
    high_v = high.to_numpy()

    for k in range(1, len(pl_idx)):
        prev_i, cur_i = pl_idx[k - 1], pl_idx[k]
        bars_between = cur_i - prev_i
        if not (range_lower <= bars_between <= range_upper):
            continue
        if rsi_v[cur_i] > rsi_v[prev_i] and low_v[cur_i] < low_v[prev_i]:
            confirm_at = cur_i + lookback_right
            if confirm_at < n:
                bull_confirmed[confirm_at] = True

    for k in range(1, len(ph_idx)):
        prev_i, cur_i = ph_idx[k - 1], ph_idx[k]
        bars_between = cur_i - prev_i
        if not (range_lower <= bars_between <= range_upper):
            continue
        if rsi_v[cur_i] < rsi_v[prev_i] and high_v[cur_i] > high_v[prev_i]:
            confirm_at = cur_i + lookback_right
            if confirm_at < n:
                bear_confirmed[confirm_at] = True

    return pd.DataFrame({
        "bull_confirmed": bull_confirmed,
        "bear_confirmed": bear_confirmed,
    }, index=close.index)


if __name__ == "__main__":
    idx = pd.date_range("2026-08-01 09:15", periods=200, freq="5min")
    rng = np.random.default_rng(3)
    close = pd.Series(1000 + np.cumsum(rng.normal(0, 1.0, 200)), index=idx)
    high = close + 1.0
    low = close - 1.0
    volume = pd.Series(rng.integers(100, 1000, 200), index=idx)

    r = rsi(close, 14)
    print(f"RSI range: {r.dropna().min():.2f} - {r.dropna().max():.2f}")
    assert r.dropna().between(0, 100).all(), "RSI escaped 0-100"

    sm = rsi_smoothing_ma(r, "SMA + Bollinger Bands", 14)
    assert "bb_upper" in sm and "bb_lower" in sm

    div = find_divergences(close, low, high, r)
    print(f"bull confirmations: {int(div['bull_confirmed'].sum())}, "
          f"bear confirmations: {int(div['bear_confirmed'].sum())}")

    print("rsi.py self-check passed")

"""
Standalone translation of EMA.txt (TradingView built-in "Moving Average
Exponential", Pine v6, //@version=6, shorttitle "EMA").

WHY A SEPARATE FILE
--------------------
Not currently used anywhere in indicators.py / config.py (which already
has its own private _ema() helper, used internally by several indicators).
This is a free-standing module because Harish uploaded EMA.txt separately
and asked for its own code, same as rsi.py / macd.py.

LENGTH OVERRIDE (15-Aug-26)
----------------------------
The script's own default is `len = input.int(9, ...)`. Harish's request
("EMA 10") overrides that default, same pattern as EMA_VWAP_LENGTH already
overriding a different script's own emaLen=9 default -- his own number
wins, the script default is not what's actually used. See
config.EMA_PIVOT_EMA_LENGTH.

TRANSLATION NOTES
------------------
Pine ta.ema(x, n) == pandas x.ewm(span=n, adjust=False).mean(). No
repainting risk -- an EMA only ever looks backward.

The script's "Smoothing" section (an optional second MA of the EMA line
itself, off by default -- maTypeInput default "None") is not translated:
it is inactive in the script's own default state and nothing in Harish's
rule references it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(source: pd.Series, length: int = 10) -> pd.Series:
    """
    Direct translation of the script's calculation:
        out = ta.ema(src, len)

    Default length is 10, not the script's own default of 9 -- see the
    module docstring's LENGTH OVERRIDE note.
    """
    return source.astype(float).ewm(span=length, adjust=False).mean()


if __name__ == "__main__":
    idx = pd.date_range("2026-08-01 09:15", periods=200, freq="5min")
    rng = np.random.default_rng(4)
    close = pd.Series(1000 + np.cumsum(rng.normal(0, 1.2, 200)), index=idx)

    e = ema(close, 10)
    print(f"EMA(10) range: {e.dropna().min():.2f} to {e.dropna().max():.2f}")
    assert e.notna().all(), "EMA should have no NaN once seeded (ewm adjust=False)"

    print("\nema.py self-check passed")

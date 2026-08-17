"""
STEP 8b (EMA-Pivot) — Matrix sheet for run_EMA_PIVOT.py.

Same layout and warm-up discipline as matrix_sheets.py (see that file's
docstring for the column-timing rule -- it applies unchanged here). The
confluence set:

    original (matrix_sheets.py)    : TW ALL, RSI, ADX, EMA VWAP  -> Final
    v2 (matrix_sheets_v2.py)        : RSI, MACD                   -> Final
    this file (EMA-Pivot)           : EMA10 + Pivot (one combined rule) -> Final

TW ALL/RSI/ADX/EMA-VWAP/MACD are all absent -- this is a brand new,
independent third pipeline, not a variant of either existing one. No
option_audit gates either (config.EMA_PIVOT_AUDIT_ENABLED), same starting
point as the MACD pipeline: straight from Final Recomm to the order sheet,
built up step by step from here.

This file does not modify matrix_sheets.py or indicators.py -- it reuses
indicators.compute_all() for Close only, and computes EMA10 (ema.py) and
the daily Pivot (pivot_points.py) itself. The generic build_matrix()
assembly function is imported from matrix_sheets.py rather than
duplicated, since it has no TW-specific logic.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import config
import ema as ema_mod
import pivot_points
from config import SIGNAL_BUY_CE, SIGNAL_BUY_PE, SIGNAL_WAIT
from matrix_sheets import build_matrix, final_recommendation


def ema_pivot_recommendation(close: pd.Series, ema10: pd.Series,
                             pivot_p: pd.Series) -> pd.Series:
    """
    Harish's rule (15-Aug-26): "Whenever a candle is crossing/above EMA10
    line and above Pivot Line at the same time then it should be Long.
    Vice versa for Short."

        Long  (BUY CE): close > EMA10 AND close > Pivot P
        Short (BUY PE): close < EMA10 AND close < Pivot P
        else:           WAIT

    A state read (close currently above/below both lines), not a one-bar
    crossover event -- same convention as every other component in this
    project (TW ribbon, RSI ema_cross, ADX DI+/DI-, EMA20-VWAP, MACD).
    Both conditions must agree on the SAME side: close above EMA10 but
    below Pivot (or vice versa) is a disagreement between the two lines,
    which is WAIT, not a coin-flip pick of one over the other -- same
    principle as ema_vwap_recommendation in matrix_sheets.py.
    """
    out = pd.Series(SIGNAL_WAIT, index=close.index, dtype=object)
    out[(close > ema10) & (close > pivot_p)] = SIGNAL_BUY_CE
    out[(close < ema10) & (close < pivot_p)] = SIGNAL_BUY_PE
    valid = close.notna() & ema10.notna() & pivot_p.notna()
    return out.where(valid, "")


def compute_symbol_frames_ema_pivot(candles: dict[str, pd.DataFrame],
                                    rules: config.SignalRules | None = None,
                                    warmup_bars: int | None = None
                                    ) -> dict[str, pd.DataFrame]:
    """
    Run the EMA10 + Pivot rule for every symbol.

    Returns {symbol: DataFrame} with columns:
        Close, EMA10, Pivot P, Pivot R1, Pivot S1,
        EMA-Pivot Recomm, Final Recomm

    `rules` is accepted for signature parity with the other two pipelines'
    compute_symbol_frames functions (final_recommendation takes it), but
    with a single component the confluence rule ("all"/"majority") makes
    no difference here.
    """
    rules = rules or config.RULES
    warmup_bars = config.WARMUP_BARS if warmup_bars is None else warmup_bars
    out: dict[str, pd.DataFrame] = {}
    skipped: list[str] = []

    for symbol, df in candles.items():
        if df is None or df.empty or len(df) < warmup_bars:
            skipped.append(f"{symbol}({0 if df is None else len(df)} bars)")
            continue

        close = df["close"].astype(float)
        pivots = pivot_points.daily_pivot_levels(df)

        ind = pd.DataFrame({
            "Close": close,
            "EMA10": ema_mod.ema(close, config.EMA_PIVOT_EMA_LENGTH),
            "Pivot P": pivots["P"],
            "Pivot R1": pivots["R1"],
            "Pivot S1": pivots["S1"],
        })

        # Blank the warm-up region BEFORE deriving signals, exactly as
        # every other pipeline does -- no signal may be produced from an
        # indicator value that hasn't settled yet. The Pivot is already
        # NaN for the first session in `df` regardless (no prior day to
        # derive it from); this additionally blanks EMA10's own warm-up.
        if warmup_bars:
            ind.iloc[:warmup_bars] = np.nan

        ind["EMA-Pivot Recomm"] = ema_pivot_recommendation(
            ind["Close"], ind["EMA10"], ind["Pivot P"])
        ind["Final Recomm"] = final_recommendation(
            [ind["EMA-Pivot Recomm"]], rules)
        out[symbol] = ind

    if skipped:
        print(f"[matrix-ema-pivot] {len(skipped)} symbol(s) skipped, too few "
              f"bars for a {warmup_bars}-bar warm-up: {', '.join(skipped[:10])}"
              + (f" ... +{len(skipped) - 10} more" if len(skipped) > 10 else ""))

    print(f"[matrix-ema-pivot] indicators computed for {len(out)} symbol(s)")
    return out


def build_all_sheets_ema_pivot(workbook: Path, candles: dict[str, pd.DataFrame],
                               trade_date: date,
                               rules: config.SignalRules | None = None
                               ) -> dict[str, pd.DataFrame]:
    """Compute everything and write the EMA-Pivot matrix sheet into the workbook."""
    import file_mgmt

    frames = compute_symbol_frames_ema_pivot(candles, rules)
    if not frames:
        raise RuntimeError("no symbol produced indicator data -- nothing to write")

    built: dict[str, pd.DataFrame] = {}

    for sheet_name, metrics in config.MATRIX_SHEETS_EMA_PIVOT.items():
        df = build_matrix(sheet_name, frames, trade_date, metrics)
        file_mgmt.write_sheet(workbook, sheet_name, df)
        built[sheet_name] = df

    final_df = build_matrix(config.FINAL_SHEET_NAME, frames, trade_date,
                            config.FINAL_ROWS_EMA_PIVOT)
    file_mgmt.write_sheet(workbook, config.FINAL_SHEET_NAME, final_df)
    built[config.FINAL_SHEET_NAME] = final_df

    return built


if __name__ == "__main__":
    # Self-check with synthetic candles: verifies sheet shape, that no
    # signal leaks into the blanked warm-up region, and that a Long only
    # ever fires when close is above BOTH EMA10 and Pivot P (mirror for
    # Short).
    import ist_clock

    rng = np.random.default_rng(11)
    td = date(2026, 7, 31)
    # 3 sessions so the 3rd has a real prior-day pivot to test against.
    idx = pd.date_range(f"{td - pd.Timedelta(days=2)} 09:15", periods=225,
                        freq="5min", tz=ist_clock.IST)

    fake = {}
    for sym in ("AAA", "BBB"):
        close = pd.Series(1000 + np.cumsum(rng.normal(0, 3, 225)), index=idx)
        fake[sym] = pd.DataFrame({
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 3, "low": close - 3, "close": close, "volume": 500,
        }, index=idx)

    frames = compute_symbol_frames_ema_pivot(fake, warmup_bars=20)

    for name, metrics in config.MATRIX_SHEETS_EMA_PIVOT.items():
        m = build_matrix(name, frames, td, metrics)
        assert m.shape[1] == 75, f"{name}: expected 75 columns, got {m.shape[1]}"

    m = build_matrix(config.FINAL_SHEET_NAME, frames, td, config.FINAL_ROWS_EMA_PIVOT)
    warm = m[m["Metrics"] == "Final Recomm"].iloc[0, 2:22]
    assert all(v == "" for v in warm), "signal leaked into the warm-up region"

    for sym, ind in frames.items():
        long_ok = ((ind["EMA-Pivot Recomm"] == config.SIGNAL_BUY_CE)
                  & ~((ind["Close"] > ind["EMA10"]) & (ind["Close"] > ind["Pivot P"])))
        short_ok = ((ind["EMA-Pivot Recomm"] == config.SIGNAL_BUY_PE)
                   & ~((ind["Close"] < ind["EMA10"]) & (ind["Close"] < ind["Pivot P"])))
        assert long_ok.sum() == 0 and short_ok.sum() == 0, (
            f"{sym}: EMA-Pivot Recomm disagrees with the raw EMA10/Pivot condition")

    print("matrix_sheets_ema_pivot self-check passed")

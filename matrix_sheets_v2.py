"""
STEP 8b (v2) — Matrix sheets for run_MACD.py.

Same layout and warm-up discipline as matrix_sheets.py (see that file's
docstring for the column-timing rule -- it applies unchanged here). The
confluence set:

    original (matrix_sheets.py) : TW ALL, RSI, ADX, EMA VWAP  -> Final
    v2 (this file)               : RSI, MACD                   -> Final

TW ALL is dropped and MACD (macd.py) takes its place, per Harish's request.
ADX and EMA VWAP REMOVED from the v2 confluence (15-Aug-26) -- straight
from Final Recomm to the order sheet, no option_audit gates either (see
config.MACD_AUDIT_ENABLED in order_engine.process_date). Both being
rebuilt step by step from here rather than carried over from the original
pipeline's tuning.

This file does not modify matrix_sheets.py or indicators.py -- it reuses
indicators.compute_all() for RSI (and, unused for signal generation here,
ADX/EMA20/VWAP/TW) and adds MACD on top with its own macd.py module, then
assembles Final Recomm from RSI + MACD. The generic build_matrix()
assembly function is imported from matrix_sheets.py rather than
duplicated, since it has no TW-specific logic.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import config
import indicators
import macd as macd_mod
from config import SIGNAL_BUY_CE, SIGNAL_BUY_PE, SIGNAL_WAIT
from matrix_sheets import (
    build_matrix,
    final_recommendation,
)


def rsi_midline_recommendation(rsi_series: pd.Series) -> pd.Series:
    """
    RSI vs its own 50 midline (15-Aug-26, Harish, matching the uploaded
    "Relative Strength Index" Pine v6 script's own 'RSI Middle Band'
    hline(50)): RSI above 50 = BUY CE, RSI below 50 = BUY PE, else WAIT.

    MACD-pipeline-specific -- independent of matrix_sheets.rsi_recommendation
    (ema_cross/level/direction modes, driven by shared config.RULES.rsi_mode)
    which run_TW_ALL.py still uses unchanged. The RSI VALUE itself is
    unaffected -- indicators.rsi_wilder() (RMA-smoothed, period 14) already
    matches this script's own rsi = 100 - 100/(1+up/down) calculation
    exactly, only the recommendation RULE differs here.
    """
    out = pd.Series(SIGNAL_WAIT, index=rsi_series.index, dtype=object)
    out[rsi_series > 50.0] = SIGNAL_BUY_CE
    out[rsi_series < 50.0] = SIGNAL_BUY_PE
    return out.where(rsi_series.notna(), "")


def macd_recommendation(macd_line: pd.Series, signal_line: pd.Series,
                        hist: pd.Series) -> pd.Series:
    """
    MACD confluence rule, upgraded 15-Aug-26 (Harish) to also read the
    histogram, not just the line-vs-signal state:

        BUY CE: MACD line (blue) above Signal line (orange) AND histogram
                is bright green -- hist >= 0 AND rising, matching the
                script's own #26a69a ("teal") colour state.
        BUY PE: mirror -- MACD below Signal AND histogram bright red
                (hist < 0 AND falling, the script's #ff5252 "red" state).
        else:   WAIT

    Mathematically hist >= 0 is IDENTICAL to macd_line >= signal_line
    (hist = macd - signal), so the histogram term only adds information
    through its RISING/FALLING half -- a positive-but-fading histogram
    ("pale_teal", #b2dfdb in the script) does NOT confirm a CE, and a
    negative-but-fading one ("pale_red", #ffcdd2) does NOT confirm a PE.
    That fading half is genuinely new information: momentum has to be
    building, not just be on the right side of zero. See
    macd.histogram_color() for the 4-way colour state this reads.
    """
    colors = macd_mod.histogram_color(hist)
    out = pd.Series(SIGNAL_WAIT, index=macd_line.index, dtype=object)
    out[(macd_line > signal_line) & (colors == "teal")] = SIGNAL_BUY_CE
    out[(macd_line < signal_line) & (colors == "red")] = SIGNAL_BUY_PE
    valid = macd_line.notna() & signal_line.notna() & hist.notna()
    return out.where(valid, "")


def compute_symbol_frames_v2(candles: dict[str, pd.DataFrame],
                             rules: config.SignalRules | None = None,
                             warmup_bars: int | None = None) -> dict[str, pd.DataFrame]:
    """
    Run every indicator and signal rule for every symbol, v2 confluence set.

    Returns {symbol: DataFrame} with columns:
        Close, RSI, RSI EMA9, DI+, DI-, ADX, EMA20, VWAP, ATR,
        MACD, Signal, Hist,
        RSI Recomm, MACD Recomm, Final Recomm

    ADX and EMA20-VWAP Recomm REMOVED from confluence (15-Aug-26) -- Final
    Recomm is now RSI + MACD only, straight through to the order sheet with
    no option_audit gates (config.MACD_AUDIT_ENABLED). Both being rebuilt
    step by step from here.

    (indicators.compute_all() also returns TW's N-Line/MHULL/SHULL/TREND
    and DI+/DI-/ADX/EMA20/VWAP themselves -- left in place but unused for
    signal generation here, since dropping them from the shared
    indicators.py function would affect the original pipeline too.)
    """
    rules = rules or config.RULES
    warmup_bars = config.WARMUP_BARS if warmup_bars is None else warmup_bars
    out: dict[str, pd.DataFrame] = {}
    skipped: list[str] = []

    for symbol, df in candles.items():
        if df is None or df.empty or len(df) < warmup_bars:
            skipped.append(f"{symbol}({0 if df is None else len(df)} bars)")
            continue

        ind = indicators.compute_all(df).copy()

        macd_df = macd_mod.macd(
            df["close"].astype(float),
            fast_length=config.MACD_FAST_LENGTH,
            slow_length=config.MACD_SLOW_LENGTH,
            signal_length=config.MACD_SIGNAL_LENGTH,
            osc_ma_type=config.MACD_OSC_MA_TYPE,
            signal_ma_type=config.MACD_SIGNAL_MA_TYPE,
        )
        ind["MACD"] = macd_df["macd"]
        ind["Signal"] = macd_df["signal"]
        ind["Hist"] = macd_df["hist"]

        # Blank the warm-up region BEFORE deriving signals, exactly as the
        # original pipeline does -- no signal may be produced from an
        # indicator value that hasn't settled yet.
        if warmup_bars:
            ind.iloc[:warmup_bars] = np.nan

        ind["RSI Recomm"] = rsi_midline_recommendation(ind["RSI"])
        ind["MACD Recomm"] = macd_recommendation(
            ind["MACD"], ind["Signal"], ind["Hist"])
        ind["Final Recomm"] = final_recommendation(
            [ind["RSI Recomm"], ind["MACD Recomm"]], rules
        )
        out[symbol] = ind

    if skipped:
        print(f"[matrix-v2] {len(skipped)} symbol(s) skipped, too few bars for a "
              f"{warmup_bars}-bar warm-up: {', '.join(skipped[:10])}"
              + (f" ... +{len(skipped) - 10} more" if len(skipped) > 10 else ""))

    print(f"[matrix-v2] indicators computed for {len(out)} symbol(s)")
    return out


def build_all_sheets_v2(workbook: Path, candles: dict[str, pd.DataFrame],
                        trade_date: date,
                        rules: config.SignalRules | None = None) -> dict[str, pd.DataFrame]:
    """Compute everything and write each v2 matrix sheet into the workbook."""
    import file_mgmt

    frames = compute_symbol_frames_v2(candles, rules)
    if not frames:
        raise RuntimeError("no symbol produced indicator data -- nothing to write")

    built: dict[str, pd.DataFrame] = {}

    for sheet_name, metrics in config.MATRIX_SHEETS_V2.items():
        df = build_matrix(sheet_name, frames, trade_date, metrics)
        file_mgmt.write_sheet(workbook, sheet_name, df)
        built[sheet_name] = df

    final_df = build_matrix(config.FINAL_SHEET_NAME, frames, trade_date,
                            config.FINAL_ROWS_V2)
    file_mgmt.write_sheet(workbook, config.FINAL_SHEET_NAME, final_df)
    built[config.FINAL_SHEET_NAME] = final_df

    return built


if __name__ == "__main__":
    # Self-check with synthetic candles: verifies sheet shape, that no
    # signal leaks into the blanked warm-up region, and that Final Recomm
    # only fires when every v2 component agrees.
    import ist_clock

    rng = np.random.default_rng(11)
    td = date(2026, 7, 31)
    idx = pd.date_range(f"{td} 09:15", periods=75, freq="5min", tz=ist_clock.IST)

    fake = {}
    for sym in ("AAA", "BBB"):
        close = pd.Series(1000 + np.cumsum(rng.normal(0, 3, 75)), index=idx)
        fake[sym] = pd.DataFrame({
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 3, "low": close - 3, "close": close, "volume": 500,
        }, index=idx)

    frames = compute_symbol_frames_v2(fake, warmup_bars=20)

    for name, metrics in config.MATRIX_SHEETS_V2.items():
        m = build_matrix(name, frames, td, metrics)
        assert m.shape[1] == 75, f"{name}: expected 75 columns, got {m.shape[1]}"
        assert "TW" not in name.upper(), f"{name}: TW should not exist in v2"

    m = build_matrix(config.FINAL_SHEET_NAME, frames, td, config.FINAL_ROWS_V2)
    warm = m[m["Metrics"] == "Final Recomm"].iloc[0, 2:22]
    assert all(v == "" for v in warm), "signal leaked into the warm-up region"

    # Every Final Recomm cell that isn't blank/WAIT must agree with every
    # component row in the same column, for the same symbol.
    for sym in frames:
        comp_rows = m[(m["Symbol"] == sym) &
                     (m["Metrics"].isin(config.FINAL_ROWS_V2[:-1]))]
        final_row = m[(m["Symbol"] == sym) & (m["Metrics"] == "Final Recomm")]
        for col in m.columns[2:]:
            fv = final_row[col].iloc[0]
            if fv in ("", config.SIGNAL_WAIT):
                continue
            comps = comp_rows[col].tolist()
            assert all(c == fv for c in comps), (
                f"{sym} {col}: Final={fv} but components={comps}")

    print("matrix_sheets_v2 self-check passed")

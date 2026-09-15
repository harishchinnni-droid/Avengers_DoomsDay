"""
STEP 8b (Harish) — Matrix sheet for run_HARISH.py.

Same layout and warm-up discipline as matrix_sheets.py (see that file's
docstring for the column-timing rule -- it applies unchanged here). The
confluence set:

    original (matrix_sheets.py)     : TW ALL, RSI, ADX, EMA VWAP -> Final
    v2 (matrix_sheets_v2.py)        : RSI, MACD                   -> Final
    EMA-Pivot (matrix_sheets_ema_pivot.py) : EMA10 + Pivot        -> Final
    this file (Harish)              : Harish TW EMA + VWAP (one rule) -> Final

TW ALL/RSI/ADX/MACD/EMA-Pivot are all absent -- this is a brand new,
independent fourth pipeline, not a variant of any existing one. No
option_audit gates either (config.HARISH_AUDIT_ENABLED), same starting
point as MACD/EMA-Pivot: straight from Final Recomm to the order sheet,
built up step by step from here.

This file does not modify matrix_sheets.py, indicators.py, or config's
MATRIX_SHEETS/FINAL_ROWS (the ones run_TW_ALL.py reads) -- it reuses
indicators.compute_harish() for the raw indicator columns and
indicators.harish_recommendation() for the signal rule, both added
alongside compute_all()/tw_recommendation() as standalone entry points,
not changes to them. The generic build_matrix()/final_recommendation()
assembly functions are imported from matrix_sheets.py rather than
duplicated, since they have no TW-specific logic (matrix_sheets_ema_pivot.py
does the same).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import config
import indicators
from matrix_sheets import build_matrix, final_recommendation


def compute_symbol_frames_harish(candles: dict[str, pd.DataFrame],
                                 rules: config.SignalRules | None = None,
                                 warmup_bars: int | None = None
                                 ) -> dict[str, pd.DataFrame]:
    """
    Run the Harish TW EMA + VWAP rule for every symbol.

    Returns {symbol: DataFrame} with columns:
        Close, Open, EMA9, N-Line, MHULL, SHULL, TREND, Dot/Triangle,
        Harish Recomm, Final Recomm

    `rules` is accepted for signature parity with the other pipelines'
    compute_symbol_frames functions (final_recommendation takes it), but
    with a single component the confluence rule ("all"/"majority") makes
    no difference here -- same note as matrix_sheets_ema_pivot.py.

    Warm-up bars are blanked BEFORE the Recomm row is derived, same
    discipline as every other pipeline: an EMA9/Hull/N-Line computed from
    too few bars is not a real value, and no signal may be produced from
    one.
    """
    rules = rules or config.RULES
    warmup_bars = config.WARMUP_BARS if warmup_bars is None else warmup_bars
    out: dict[str, pd.DataFrame] = {}
    skipped: list[str] = []

    for symbol, df in candles.items():
        if df is None or df.empty or len(df) < warmup_bars:
            skipped.append(f"{symbol}({0 if df is None else len(df)} bars)")
            continue

        ind = indicators.compute_harish(df)

        if warmup_bars:
            ind.iloc[:warmup_bars] = np.nan

        ind["Harish Recomm"] = indicators.harish_recommendation(ind)
        ind["Final Recomm"] = final_recommendation([ind["Harish Recomm"]], rules)
        out[symbol] = ind

    if skipped:
        print(f"[matrix-harish] {len(skipped)} symbol(s) skipped, too few "
              f"bars for a {warmup_bars}-bar warm-up: {', '.join(skipped[:10])}"
              + (f" ... +{len(skipped) - 10} more" if len(skipped) > 10 else ""))

    print(f"[matrix-harish] indicators computed for {len(out)} symbol(s)")
    return out


def build_all_sheets_harish(workbook: Path, candles: dict[str, pd.DataFrame],
                            trade_date: date,
                            rules: config.SignalRules | None = None
                            ) -> dict[str, pd.DataFrame]:
    """Compute everything and write the Harish matrix sheet into the workbook."""
    import file_mgmt

    frames = compute_symbol_frames_harish(candles, rules)
    if not frames:
        raise RuntimeError("no symbol produced indicator data -- nothing to write")

    built: dict[str, pd.DataFrame] = {}

    for sheet_name, metrics in config.MATRIX_SHEETS_HARISH.items():
        df = build_matrix(sheet_name, frames, trade_date, metrics)
        file_mgmt.write_sheet(workbook, sheet_name, df)
        built[sheet_name] = df

    final_df = build_matrix(config.FINAL_SHEET_NAME, frames, trade_date,
                            config.FINAL_ROWS_HARISH)
    file_mgmt.write_sheet(workbook, config.FINAL_SHEET_NAME, final_df)
    built[config.FINAL_SHEET_NAME] = final_df

    return built


if __name__ == "__main__":
    # Self-check with synthetic candles: verifies sheet shape, that no
    # signal leaks into the blanked warm-up region, and that every Harish
    # Recomm bar independently re-satisfies the SIMPLIFIED rule (12-Sep-26):
    # marker + previous-bar marker both the same colour, this bar's candle
    # closing the matching direction. See indicators.harish_recommendation's
    # own docstring/revision history.
    import ist_clock

    rng = np.random.default_rng(5)
    td = date(2026, 7, 31)
    idx = pd.date_range(f"{td} 09:15", periods=300, freq="5min", tz=ist_clock.IST)

    fake = {}
    for sym in ("AAA", "BBB"):
        close = pd.Series(1000 + np.cumsum(rng.normal(0, 3, 300)), index=idx)
        fake[sym] = pd.DataFrame({
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 3, "low": close - 3, "close": close, "volume": 500,
        }, index=idx)

    frames = compute_symbol_frames_harish(fake, warmup_bars=20)

    for name, metrics in config.MATRIX_SHEETS_HARISH.items():
        m = build_matrix(name, frames, td, metrics)
        assert m.shape[1] == 75, f"{name}: expected 75 columns, got {m.shape[1]}"

    m = build_matrix(config.FINAL_SHEET_NAME, frames, td, config.FINAL_ROWS_HARISH)
    warm = m[m["Metrics"] == "Final Recomm"].iloc[0, 2:22]
    assert all(v == "" for v in warm), "signal leaked into the warm-up region"

    for sym, ind in frames.items():
        marker = ind["Dot/Triangle"]

        # 12-Sep-26 (2nd follow-up, WIPRO bug): Dot and Triangle are
        # genuinely independent conditions and CAN disagree on the same
        # bar (confirmed on real WIPRO data). Such a bar keeps showing
        # Dot's colour in the "Dot/Triangle" display column (Harish's
        # explicit choice), but must count as NEITHER colour for the
        # entry rule -- re-derive the rule the same conflict-aware way
        # indicators.harish_recommendation() does, not off the raw
        # marker column, so this check actually exercises the fix
        # instead of silently passing a weaker superset test.
        dot = ind["Dot"]
        triangle = ind["Triangle"]
        conflict = (dot != "") & (triangle != "") & (dot != triangle)
        rule_marker = marker.where(~conflict, "")
        prev_marker = rule_marker.shift(1)

        # SAME-DAY GUARD (12-Sep-26, AXISBANK follow-up): a Dot on one
        # day's last bar pairing with a Triangle on the next day's first
        # bar is an "adjacent row", not an adjacent trading moment -- see
        # indicators.harish_recommendation()'s own same-day guard, which
        # this re-derivation must mirror or it would pass a weaker check
        # than the real rule.
        day = pd.Series(ind.index.date, index=ind.index)
        prev_marker = prev_marker.where(day == day.shift(1), "")

        ce_mask = ind["Harish Recomm"] == config.SIGNAL_BUY_CE
        pe_mask = ind["Harish Recomm"] == config.SIGNAL_BUY_PE

        ce_ok = ((rule_marker == "Green") & (prev_marker == "Green")
                & (ind["Close"] > ind["Open"]))
        pe_ok = ((rule_marker == "Red") & (prev_marker == "Red")
                & (ind["Close"] < ind["Open"]))

        assert (ce_mask & ~ce_ok).sum() == 0, f"{sym}: BUY CE without adjacent same-day Green rule-markers + Bullish close"
        assert (pe_mask & ~pe_ok).sum() == 0, f"{sym}: BUY PE without adjacent same-day Red rule-markers + Bearish close"

        # Dot/Triangle marker is an event (Green/Red on the crossing bar
        # only), not held state -- regression guard for the 12-Sep-26 bug
        # where every bar came back coloured. See
        # indicators._cross_event's docstring.
        n_bars = len(ind)
        marker_events = (marker != "").sum()
        assert marker_events < n_bars * 0.5, f"{sym}: Dot/Triangle firing on >=50% of bars"

    # WIPRO-style regression: a same-bar Dot/Triangle conflict must not
    # anchor a BUY CE/PE on the following bar, even though the display
    # column keeps showing Dot's colour on that conflicting bar.
    sample_sym = next(iter(frames))
    conflict_idx = frames[sample_sym].index[:6]
    synth = pd.DataFrame({
        "Close":  [100.0, 100.0, 99.0, 98.5, 98.0, 97.5],
        "Open":   [100.0, 100.2, 100.0, 99.0, 98.5, 98.0],
        "Dot/Triangle": ["", "Red", "Red", "", "", ""],
        "Dot":          ["", "Red", "",    "", "", ""],
        "Triangle":     ["", "Green", "Red", "", "", ""],
    }, index=conflict_idx)
    synth_recomm = indicators.harish_recommendation(synth)
    assert synth_recomm.iloc[2] != config.SIGNAL_BUY_PE, (
        "WIPRO-style conflict regression: a same-bar Dot/Triangle "
        "disagreement must not anchor a BUY PE on the following bar")

    # AXISBANK-style regression: a Red Dot on the PREVIOUS day's last
    # candle must not pair with a Red Triangle on the NEXT day's very
    # first candle to anchor a BUY PE, even with a Bearish close.
    cross_day_idx = pd.DatetimeIndex([
        pd.Timestamp("2026-07-01 15:20"), pd.Timestamp("2026-07-01 15:25"),
        pd.Timestamp("2026-07-02 09:15"),
    ])
    cross_day = pd.DataFrame({
        "Close":  [101.0, 100.0, 98.0],
        "Open":   [101.0, 100.5, 99.0],
        "Dot/Triangle": ["", "Red", "Red"],
        "Dot":          ["", "Red", ""],
        "Triangle":     ["", "",    "Red"],
    }, index=cross_day_idx)
    cross_day_recomm = indicators.harish_recommendation(cross_day)
    assert cross_day_recomm.iloc[2] != config.SIGNAL_BUY_PE, (
        "AXISBANK-style cross-day regression: a Dot from the previous "
        "trading day must not pair with a Triangle on the next day's "
        "first candle to anchor a BUY PE")

    print("matrix_sheets_harish self-check passed")

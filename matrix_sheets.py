"""
STEP 8b — Build the matrix sheets.

LAYOUT (copied from the sample workbook 31-Jul-26 FNO-L.xlsx)

    Symbol   | Metrics     | 09:15 | 09:20 | ... | 15:15
    ADANIENT | Close       | ...   | ...   |     |
    ADANIENT | RSI         | ...   | ...   |     |
    ADANIENT | RSI Recomm  | BUY CE| BUY PE|     |
    ADANIPORTS | Close     | ...   |

Two index columns, then one column per 5-minute slot. 73 slots, 09:15 to
15:15. Each symbol contributes one block of metric rows.

THE COLUMN LABEL IS THE CANDLE'S OPEN TIME. The '09:15' column holds the
candle covering 09:15:00-09:19:59, which is not complete until 09:20. Signals
in that column are only actionable from 09:20 onward. This is stated here
because reading the sheet as "the signal at 09:15" and acting on it at 09:15
is look-ahead bias with extra steps.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import config
import ist_clock
import indicators
import macd as macd_mod
from config import SIGNAL_BUY_CE, SIGNAL_BUY_PE, SIGNAL_WAIT

IDX_COLS = ["Symbol", "Metrics"]


# --------------------------------------------------------------------------
# signal rules  (defaults are INFERRED, see config.SignalRules)
# --------------------------------------------------------------------------
def rsi_recommendation(rsi_series: pd.Series,
                       rules: config.SignalRules | None = None,
                       rsi_ema_series: pd.Series | None = None) -> pd.Series:
    """BUY CE / BUY PE / WAIT from RSI."""
    rules = rules or config.RULES

    if rules.rsi_mode == "ema_cross":
        # 05-Aug-26 rule: "If RSI (blue) is above EMA9 (yellow) then BUY CE.
        # If RSI is below EMA9 then BUY PE. Else WAIT." A level comparison
        # against a moving line, not a one-bar crossover event -- it holds
        # BUY CE/PE for as long as RSI stays on that side of its EMA9, which
        # is what "if X then Y" reads as, and matches how the TW ribbon and
        # ADX/DI rules already work (state, not event).
        if rsi_ema_series is None:
            raise ValueError("rsi_mode == 'ema_cross' needs the RSI EMA9 series")
        out = pd.Series(SIGNAL_WAIT, index=rsi_series.index, dtype=object)
        out[rsi_series > rsi_ema_series] = SIGNAL_BUY_CE
        out[rsi_series < rsi_ema_series] = SIGNAL_BUY_PE
        valid = rsi_series.notna() & rsi_ema_series.notna()
        return out.where(valid, "")

    if rules.rsi_mode == "level":
        out = pd.Series(SIGNAL_WAIT, index=rsi_series.index, dtype=object)
        out[rsi_series >= rules.rsi_upper] = SIGNAL_BUY_CE
        out[rsi_series <= rules.rsi_lower] = SIGNAL_BUY_PE
        return out.where(rsi_series.notna(), "")

    # "direction" -- rising vs the previous bar
    delta = rsi_series.diff()
    out = pd.Series(SIGNAL_WAIT, index=rsi_series.index, dtype=object)
    tol = rules.rsi_flat_tolerance
    out[delta > tol] = SIGNAL_BUY_CE
    out[delta < -tol] = SIGNAL_BUY_PE
    # first bar has no previous value -- the sample shows BUY CE there, which
    # is simply the absence of a comparison. Left as WAIT deliberately: an
    # unjustified signal on the opening bar is not a signal.
    return out.where(rsi_series.notna(), "")


def adx_recommendation(adx_df: pd.DataFrame,
                       rules: config.SignalRules | None = None) -> pd.Series:
    """
    ADX & DI combination, per Harish's rule.

    ADX measures trend STRENGTH and is direction-blind, so it cannot pick a
    side on its own -- that is what DI+ / DI- are for. Below the threshold
    (20, from `th = input(20)` in his script) there is no trend worth trading
    and the answer is WAIT regardless of which DI is on top.
    """
    rules = rules or config.RULES
    adx_line, di_plus, di_minus = adx_df["ADX"], adx_df["DI+"], adx_df["DI-"]

    out = pd.Series(SIGNAL_WAIT, index=adx_df.index, dtype=object)
    strong = adx_line >= rules.adx_min
    out[strong & (di_plus > di_minus)] = SIGNAL_BUY_CE
    out[strong & (di_minus > di_plus)] = SIGNAL_BUY_PE
    return out.where(adx_line.notna(), "")


def tw_recommendation(frame: pd.DataFrame) -> pd.Series:
    """
    TW rule (rebuilt 16-Aug-26, blended with the old-folder version):
    pre-entry is the Hull crossover event itself, entry bar's close beyond
    the crossover bar's close = BUY CE, mirrored for PE. Delegates to
    indicators.tw_recommendation so the logic lives in one place.
    """
    return indicators.tw_recommendation(
        frame[["Close", "MHULL", "SHULL", "N-Line"]],
        frame["Close"]
    )


def ema_vwap_recommendation(close: pd.Series, ema: pd.Series,
                            vwap: pd.Series) -> pd.Series:
    """
    EMA20-VWAP Ribbon rule (05-Aug-26): "if candle is above both Yellow
    (EMA20) & Blue (VWAP) then BUY CE. If below both then BUY PE. Else
    WAIT." Both conditions must hold on the SAME side -- close above EMA20
    but below VWAP (or vice versa) is a disagreement between the two lines,
    which is WAIT, not a coin-flip pick of one line over the other.
    """
    out = pd.Series(SIGNAL_WAIT, index=close.index, dtype=object)
    out[(close > ema) & (close > vwap)] = SIGNAL_BUY_CE
    out[(close < ema) & (close < vwap)] = SIGNAL_BUY_PE
    valid = close.notna() & ema.notna() & vwap.notna()
    return out.where(valid, "")


def macd_recommendation(macd_line: pd.Series, signal_line: pd.Series,
                        hist: pd.Series) -> pd.Series:
    """
    MACD confluence rule (added to the main pipeline 20-Aug-26, Harish:
    "enable MACD indicator for confluence"). Same rule matrix_sheets_v2.py
    already uses for run_MACD.py's RSI+MACD pipeline -- canonical
    definition lives here now, v2 imports it instead of duplicating.

        BUY CE: MACD line (blue) above Signal line (orange) AND histogram
                is bright green -- hist >= 0 AND rising, matching the
                script's own #26a69a ("teal") colour state.
        BUY PE: mirror -- MACD below Signal AND histogram bright red
                (hist < 0 AND falling, the script's #ff5252 "red" state).
        else:   WAIT

    Mathematically hist >= 0 is IDENTICAL to macd_line >= signal_line
    (hist = macd - signal), so the histogram term only adds information
    through its RISING/FALLING half -- a positive-but-fading histogram
    ("pale_teal") does NOT confirm a CE, a negative-but-fading one
    ("pale_red") does NOT confirm a PE. See macd.histogram_color().
    """
    colors = macd_mod.histogram_color(hist)
    out = pd.Series(SIGNAL_WAIT, index=macd_line.index, dtype=object)
    out[(macd_line > signal_line) & (colors == "teal")] = SIGNAL_BUY_CE
    out[(macd_line < signal_line) & (colors == "red")] = SIGNAL_BUY_PE
    valid = macd_line.notna() & signal_line.notna() & hist.notna()
    return out.where(valid, "")


def final_recommendation(components: list[pd.Series],
                         rules: config.SignalRules | None = None) -> pd.Series:
    """
    Combine component signals.

    "all"      -> every component must agree, otherwise WAIT (reproduces the
                  sample workbook, where any disagreement gave WAIT)
    "majority" -> strict majority wins
    """
    rules = rules or config.RULES
    if not components:
        raise ValueError("no component signals supplied")

    frame = pd.concat(components, axis=1)
    out = pd.Series(SIGNAL_WAIT, index=frame.index, dtype=object)

    for signal in (SIGNAL_BUY_CE, SIGNAL_BUY_PE):
        votes = (frame == signal).sum(axis=1)
        if rules.confluence == "all":
            out[votes == frame.shape[1]] = signal
        else:
            out[votes > frame.shape[1] / 2] = signal

    # If any component is blank the bar is still warming up. Blank, not WAIT --
    # "WAIT" is a decision, and there is nothing here to decide from yet.
    incomplete = (frame == "").any(axis=1) | frame.isna().any(axis=1)
    return out.mask(incomplete, "")


def relax_confluence_lag(final: pd.Series, tw: pd.Series, macd: pd.Series,
                         rsi: pd.Series, adx: pd.Series) -> pd.Series:
    """
    RELAXED CONFLUENCE, RSI/ADX one-bar grace (24-Aug-26, Harish -- KOTAKBANK
    20-Aug-26 example: TW ALL and MACD already agreed BUY CE, RSI had agreed
    since the open, only ADX was still WAIT at 09:30 -- ADX caught up at
    09:35 and the 2-bar qualification only started counting from there,
    costing the trade a full candle of entry lag it didn't need).

    Gated by config.RELAXED_CONFLUENCE_ENABLED -- OFF by default. `final` is
    the ordinary 4-way "all agree" result from final_recommendation();
    everywhere that's already an actionable signal is returned untouched.

    TW ALL and MACD are treated as load-bearing: both must already show the
    SAME signal on bar N, or nothing here applies -- this only ever bridges
    the RSI/ADX pair, never substitutes for TW or MACD. On such a bar, if
    RSI and/or ADX are still WAIT/blank (not yet agreeing, but not actively
    disagreeing either) AND the VERY NEXT bar shows full 4-way agreement on
    that same signal, bar N is credited with the signal too -- so a 2-bar
    run can complete a bar earlier than waiting for RSI/ADX to repeat their
    confirmation.

    Never bridges an ACTIVE opposite vote from RSI or ADX (e.g. RSI reading
    BUY PE while TW+MACD read BUY CE) -- that is a real disagreement, not a
    component still catching up, and is left as WAIT exactly as today. Never
    bridges across a day boundary (bar N's "next bar" must be the same
    trade_date) -- the underlying frame is a multi-day lookback series, and
    09:15 of the following session is not "the next candle" for 15:10 of
    this one.

    No lookahead: by the time this can act on bar N (the entry decision for
    a run starting at N is only made once bar N+1 has CLOSED -- see
    order_engine's "decided when the candle closes" rule), bar N+1's
    indicator values already exist. Nothing here reads a bar before it has
    closed.

    UNTESTED as a live rule -- run it against the same backtest days as the
    unrelaxed version and compare profit factor before trusting it with
    real capital.
    """
    core = pd.Series("", index=final.index, dtype=object)
    core[(tw == SIGNAL_BUY_CE) & (macd == SIGNAL_BUY_CE)] = SIGNAL_BUY_CE
    core[(tw == SIGNAL_BUY_PE) & (macd == SIGNAL_BUY_PE)] = SIGNAL_BUY_PE

    next_final = final.shift(-1)
    same_day = pd.Series(final.index.date, index=final.index)
    next_is_same_day = same_day == same_day.shift(-1)

    relaxed = final.copy()
    for signal, opposite in ((SIGNAL_BUY_CE, SIGNAL_BUY_PE),
                             (SIGNAL_BUY_PE, SIGNAL_BUY_CE)):
        bridge = (
            (final == SIGNAL_WAIT)
            & (core == signal)
            & (rsi != opposite) & (adx != opposite)
            & (next_final == signal)
            & next_is_same_day
        )
        relaxed[bridge] = signal

    return relaxed


# --------------------------------------------------------------------------
# matrix assembly
# --------------------------------------------------------------------------
def _to_slots(series: pd.Series, slots: list[str], trade_date: date) -> list:
    """
    Reindex a time series onto the fixed slot columns for one trading day.

    Only candles belonging to trade_date are used. A 30-day lookback is
    fetched for indicator warm-up, but the sheet shows one session.
    """
    if series.empty:
        return [""] * len(slots)

    day = series[series.index.date == trade_date]
    if day.empty:
        return [""] * len(slots)

    by_slot = {ts.strftime("%H:%M"): val for ts, val in day.items()}
    out = []
    for s in slots:
        v = by_slot.get(s, "")
        if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
            v = ""
        out.append(v)
    return out


def build_matrix(sheet_name: str, indicator_frames: dict[str, pd.DataFrame],
                 trade_date: date, metrics: list[str] | None = None,
                 slots: list[str] | None = None) -> pd.DataFrame:
    """
    Build one matrix sheet.

    indicator_frames: {symbol: DataFrame of indicator columns}
    Returns a DataFrame with columns ['Symbol', 'Metrics', '09:15', ...].
    """
    metrics = metrics or config.MATRIX_SHEETS.get(sheet_name)
    if not metrics:
        raise ValueError(f"no metric list defined for sheet {sheet_name!r}")

    slots = slots or ist_clock.candle_slots(
        trade_date, config.INTERVAL_MINUTES,
        pd.Timestamp(config.MATRIX_LAST_SLOT).time(),
    )

    rows = []
    for symbol in sorted(indicator_frames):
        frame = indicator_frames[symbol]
        for metric in metrics:
            if metric not in frame.columns:
                rows.append([symbol, metric] + [""] * len(slots))
                continue
            rows.append([symbol, metric] + _to_slots(frame[metric], slots, trade_date))

    df = pd.DataFrame(rows, columns=IDX_COLS + slots)
    print(f"[matrix] {sheet_name}: {len(indicator_frames)} symbols x "
          f"{len(metrics)} metrics = {len(df)} rows, {len(slots)} time columns")
    return df


def compute_symbol_frames(candles: dict[str, pd.DataFrame],
                          rules: config.SignalRules | None = None,
                          warmup_bars: int | None = None) -> dict[str, pd.DataFrame]:
    """
    Run every indicator and signal rule for every symbol.

    Returns {symbol: DataFrame} with columns:
        Close, RSI, RSI EMA9, DI+, DI-, ADX, N-Line, MHULL, SHULL, TREND,
        EMA20, VWAP, ATR, MACD, Signal, Hist, TW ALL Recomm, RSI Recomm,
        ADX Recomm, MACD Recomm, Final Recomm

    EMA20-VWAP Recomm removed from confluence (16-Aug-26). MACD Recomm
    added (20-Aug-26) -- Final Recomm is now a 4-way TW ALL / RSI / ADX /
    MACD vote. EMA20/VWAP columns are still computed (indicators.compute_all
    is shared with the other pipelines) but nothing here reads them into a
    signal.

    Warm-up bars are blanked rather than published. An ema(100) computed from
    30 bars is not an ema(100), and printing it as one invites a trade on a
    number that means nothing.
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

        # Blank the warm-up region BEFORE deriving signals, so no signal can
        # be produced from an indicator value that hasn't settled yet.
        if warmup_bars:
            ind.iloc[:warmup_bars] = np.nan

        ind["TW ALL Recomm"] = tw_recommendation(ind)
        ind["RSI Recomm"] = rsi_recommendation(
            ind["RSI"], rules, ind.get("RSI EMA9"))
        ind["ADX Recomm"] = adx_recommendation(ind[["DI+", "DI-", "ADX"]], rules)
        ind["MACD Recomm"] = macd_recommendation(
            ind["MACD"], ind["Signal"], ind["Hist"])
        ind["Final Recomm"] = final_recommendation(
            [ind["TW ALL Recomm"], ind["RSI Recomm"], ind["ADX Recomm"],
             ind["MACD Recomm"]], rules
        )
        if config.RELAXED_CONFLUENCE_ENABLED:
            ind["Final Recomm"] = relax_confluence_lag(
                ind["Final Recomm"], ind["TW ALL Recomm"], ind["MACD Recomm"],
                ind["RSI Recomm"], ind["ADX Recomm"])
        out[symbol] = ind

    if skipped:
        print(f"[matrix] {len(skipped)} symbol(s) skipped, too few bars for a "
              f"{warmup_bars}-bar warm-up: {', '.join(skipped[:10])}"
              + (f" ... +{len(skipped) - 10} more" if len(skipped) > 10 else ""))

    print(f"[matrix] indicators computed for {len(out)} symbol(s)")
    return out


def build_all_sheets(workbook: Path, candles: dict[str, pd.DataFrame],
                     trade_date: date,
                     rules: config.SignalRules | None = None) -> dict[str, pd.DataFrame]:
    """Compute everything and write each matrix sheet into the workbook."""
    import file_mgmt

    frames = compute_symbol_frames(candles, rules)
    if not frames:
        raise RuntimeError("no symbol produced indicator data -- nothing to write")

    built: dict[str, pd.DataFrame] = {}

    for sheet_name, metrics in config.MATRIX_SHEETS.items():
        df = build_matrix(sheet_name, frames, trade_date, metrics)
        built[sheet_name] = df
        file_mgmt.write_sheet(workbook, sheet_name, df)

    final_df = build_matrix(config.FINAL_SHEET_NAME, frames, trade_date,
                            config.FINAL_ROWS)
    file_mgmt.write_sheet(workbook, config.FINAL_SHEET_NAME, final_df)
    built[config.FINAL_SHEET_NAME] = final_df

    return built


if __name__ == "__main__":
    # Self-check with synthetic candles: verifies sheet shape and that no
    # signal appears inside the blanked warm-up region.
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

    frames = compute_symbol_frames(fake, warmup_bars=20)

    for name, metrics in config.MATRIX_SHEETS.items():
        m = build_matrix(name, frames, td, metrics)
        assert m.shape[1] == 75, f"{name}: expected 75 columns, got {m.shape[1]}"

    m = build_matrix("TW ALL", frames, td, config.MATRIX_SHEETS["TW ALL"])
    print(f"\nTW ALL shape {m.shape}")
    print(m.iloc[:6, :5].to_string(index=False))

    warm = m[m["Metrics"] == "TW ALL Recomm"].iloc[0, 2:22]
    assert all(v == "" for v in warm), "signal leaked into the warm-up region"
    print("\nmatrix self-check passed")

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
        EMA20, VWAP, ATR, TW ALL Recomm, RSI Recomm, ADX Recomm, Final Recomm

    EMA20-VWAP Recomm removed from confluence (16-Aug-26) -- Final Recomm
    is back to the 3-way TW ALL / RSI / ADX vote. EMA20/VWAP columns are
    still computed (indicators.compute_all is shared with the other
    pipelines) but nothing here reads them into a signal any more.

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

        # Blank the warm-up region BEFORE deriving signals, so no signal can
        # be produced from an indicator value that hasn't settled yet.
        if warmup_bars:
            ind.iloc[:warmup_bars] = np.nan

        ind["TW ALL Recomm"] = tw_recommendation(ind)
        ind["RSI Recomm"] = rsi_recommendation(
            ind["RSI"], rules, ind.get("RSI EMA9"))
        ind["ADX Recomm"] = adx_recommendation(ind[["DI+", "DI-", "ADX"]], rules)
        ind["Final Recomm"] = final_recommendation(
            [ind["TW ALL Recomm"], ind["RSI Recomm"], ind["ADX Recomm"]], rules
        )
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

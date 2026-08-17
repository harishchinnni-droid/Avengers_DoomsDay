"""
STEP 8a — Indicators. Direct translations of Harish's three Pine scripts.

SOURCE SCRIPTS
--------------
  ADX & DI.txt                  (c) BeikabuOyaji, Pine v4
  RSI Multi Length_LuxAlgo.txt  (c) LuxAlgo, Pine v5, CC BY-NC-SA 4.0
  TW All In One.txt             Pine v4

TRANSLATION POLICY
------------------
These reproduce what HIS CHART DOES, not what the textbook version of each
indicator does. Where his Pine deviates from convention, the deviation is
reproduced and flagged in a comment. That is the only way the Python numbers
will agree with what he sees on TradingView.

Two deviations found and preserved:

  1. TW's EHMA is written as
         ema(2 * ema(src, len) - ema(src, len), round(sqrt(len)))
     Both terms use `len`, so 2*x - x = x and the whole expression collapses
     to ema(ema(src, len), sqrt(len)) -- an ordinary double-EMA. The standard
     Hull formula uses len/2 in the first term. Verified numerically: the two
     differ by up to ~10 points on a 1000-level series. See TW_USE_STANDARD_EHMA
     in config.py to switch. Default reproduces his chart.

  2. ADX is smoothed with sma(DX, len), not Wilder's rma(DX, len).
     TradingView's built-in ta.adx uses Wilder. His script uses SMA, so this
     does too. An SMA-smoothed ADX reacts faster and crosses the threshold
     sooner than the built-in.

Pine ema(x, n) == pandas ewm(span=n, adjust=False). Verified equivalent.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _ema(series: pd.Series, length: int) -> pd.Series:
    """Pine ta.ema / v4 ema()."""
    return series.ewm(span=length, adjust=False).mean()


def _wma(series: pd.Series, length: int) -> pd.Series:
    """Pine ta.wma / v4 wma(). Linearly weighted."""
    weights = np.arange(1, length + 1, dtype=float)
    return series.rolling(length).apply(
        lambda w: float(np.dot(w, weights) / weights.sum()), raw=True
    )


def _wilder_running_sum(values: np.ndarray, length: int) -> np.ndarray:
    """
    The exact recursion used in the ADX script:

        sm := nz(sm[1]) - nz(sm[1]) / len + x

    This is Wilder's running SUM, not his mean. It equals len * rma(x, len)
    once the transient settles. Implemented literally rather than as an ewm
    because the initial bars differ, and matching the chart on early bars is
    the whole point of doing this translation by hand.
    """
    out = np.empty_like(values, dtype=float)
    prev = 0.0
    for i, x in enumerate(values):
        xi = 0.0 if not np.isfinite(x) else float(x)
        prev = prev - prev / length + xi
        out[i] = prev
    return out


# --------------------------------------------------------------------------
# 1. ADX & DI  --  BeikabuOyaji, Pine v4
# --------------------------------------------------------------------------
def adx_di(df: pd.DataFrame, length: int | None = None) -> pd.DataFrame:
    """
    Returns columns ['DI+', 'DI-', 'ADX'].

    Line-by-line from the Pine:
        TrueRange = max(max(high-low, abs(high-nz(close[1]))), abs(low-nz(close[1])))
        DMPlus    = high-nz(high[1]) > nz(low[1])-low ? max(high-nz(high[1]), 0) : 0
        DMMinus   = nz(low[1])-low > high-nz(high[1]) ? max(nz(low[1])-low, 0) : 0
        DIPlus    = SmoothedDMPlus  / SmoothedTrueRange * 100
        DIMinus   = SmoothedDMMinus / SmoothedTrueRange * 100
        DX        = abs(DIPlus-DIMinus) / (DIPlus+DIMinus) * 100
        ADX       = sma(DX, len)          <-- SMA, not Wilder. Preserved.

    Note nz(): on the first bar Pine treats close[1]/high[1]/low[1] as 0, which
    makes bar 0 produce a huge TrueRange. Reproduced so early bars match.
    """
    length = length or config.ADX_PERIOD
    if df.empty:
        return pd.DataFrame(columns=["DI+", "DI-", "ADX"], index=df.index)

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    prev_close = close.shift(1).fillna(0.0)  # Pine nz()
    prev_high = high.shift(1).fillna(0.0)
    prev_low = low.shift(1).fillna(0.0)

    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    up_move = high - prev_high
    down_move = prev_low - low

    dm_plus = np.where(up_move > down_move, np.maximum(up_move, 0.0), 0.0)
    dm_minus = np.where(down_move > up_move, np.maximum(down_move, 0.0), 0.0)

    sm_tr = _wilder_running_sum(true_range.to_numpy(), length)
    sm_dm_plus = _wilder_running_sum(dm_plus, length)
    sm_dm_minus = _wilder_running_sum(dm_minus, length)

    with np.errstate(divide="ignore", invalid="ignore"):
        di_plus = np.where(sm_tr != 0, sm_dm_plus / sm_tr * 100.0, np.nan)
        di_minus = np.where(sm_tr != 0, sm_dm_minus / sm_tr * 100.0, np.nan)
        di_sum = di_plus + di_minus
        dx = np.where(di_sum != 0, np.abs(di_plus - di_minus) / di_sum * 100.0, np.nan)

    dx_s = pd.Series(dx, index=df.index)
    adx_line = dx_s.rolling(length).mean()  # sma(DX, len) -- deliberate

    return pd.DataFrame({
        "DI+": pd.Series(di_plus, index=df.index),
        "DI-": pd.Series(di_minus, index=df.index),
        "ADX": adx_line,
    })


# --------------------------------------------------------------------------
# 2. RSI Multi Length  --  LuxAlgo, Pine v5
# --------------------------------------------------------------------------
def rsi_multi_length(close: pd.Series, min_length: int | None = None,
                     max_length: int | None = None) -> pd.Series:
    """
    Average RSI across every length from min_length to max_length.

    From the Pine, per length i:
        alpha   = 1/i
        num_rma = alpha*diff      + (1-alpha)*num_prev
        den_rma = alpha*|diff|    + (1-alpha)*den_prev
        rsi_i   = 50*num_rma/den_rma + 50
    then avg_rsi = mean(rsi_i over i).

    This is an RSI written in "net change over total change" form. It is
    mathematically the same family as Wilder's RSI but averaged over 11
    lookbacks (10..20 by default), so it is smoother than a single RSI(14)
    and will not equal one.

    Both accumulators start at 0, exactly as `array.new_float(N, 0)` does.
    """
    min_length = min_length or config.RSI_MIN_LENGTH
    max_length = max_length or config.RSI_MAX_LENGTH
    if max_length < min_length:
        raise ValueError("max_length must be >= min_length")
    if close.empty:
        return pd.Series(dtype=float, index=close.index)

    diff = close.astype(float).diff().fillna(0.0).to_numpy()  # Pine nz()
    abs_diff = np.abs(diff)

    lengths = np.arange(min_length, max_length + 1, dtype=float)
    alphas = 1.0 / lengths
    n_len = len(lengths)

    num = np.zeros(n_len)
    den = np.zeros(n_len)
    out = np.empty(len(diff), dtype=float)

    for t in range(len(diff)):
        num = alphas * diff[t] + (1.0 - alphas) * num
        den = alphas * abs_diff[t] + (1.0 - alphas) * den
        with np.errstate(divide="ignore", invalid="ignore"):
            rsi_each = np.where(den != 0, 50.0 * num / den + 50.0, np.nan)
        out[t] = np.nanmean(rsi_each) if np.isfinite(rsi_each).any() else np.nan

    return pd.Series(out, index=close.index)


def rsi_wilder(close: pd.Series, period: int | None = None) -> pd.Series:
    """
    Classic single-length Wilder RSI. Matches TradingView's built-in ta.rsi().

    Provided because the chart screenshot shows a pane labelled 'RSI 14 close',
    which is the built-in, while the uploaded script is the LuxAlgo multi-length
    version. config.RSI_SOURCE decides which one the pipeline uses.
    """
    period = period or config.RSI_PERIOD
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.where(avg_loss != 0, 100.0)


def rsi(close: pd.Series) -> pd.Series:
    """Dispatch on config.RSI_SOURCE."""
    if config.RSI_SOURCE == "wilder":
        return rsi_wilder(close)
    return rsi_multi_length(close)


# --------------------------------------------------------------------------
# 3. TW All in One  --  Pine v4
# --------------------------------------------------------------------------
def _hull(src: pd.Series, length: int, mode: str,
          use_standard_ehma: bool) -> pd.Series:
    """
    The three MA modes from the script.

        HMA  = wma(2*wma(src, len/2) - wma(src, len), round(sqrt(len)))
        EHMA = ema(2*ema(src, len  ) - ema(src, len), round(sqrt(len)))
        THMA = wma(wma(src, len/3)*3 - wma(src, len/2) - wma(src, len), len)

    Look at EHMA: both inner terms use `len`, so it reduces to
    ema(ema(src, len), sqrt(len)). That is what his chart plots. The standard
    Hull form uses len/2 in the first term; use_standard_ehma=True switches
    to it, but then the output will NOT match his TradingView.
    """
    root = int(round(np.sqrt(length)))

    if mode == "Hma":
        return _wma(2 * _wma(src, length // 2) - _wma(src, length), root)

    if mode == "Ehma":
        if use_standard_ehma:
            return _ema(2 * _ema(src, length // 2) - _ema(src, length), root)
        # as written in his script -- the doubled term cancels
        return _ema(2 * _ema(src, length) - _ema(src, length), root)

    if mode == "Thma":
        half = max(length // 2, 1)
        return _wma(_wma(src, max(half // 3, 1)) * 3 - _wma(src, max(half // 2, 1))
                    - _wma(src, half), half)

    raise ValueError(f"unknown hull mode {mode!r}")


def tw_all_in_one(df: pd.DataFrame, length: int | None = None,
                  mode: str | None = None,
                  nline_length: int | None = None,
                  use_standard_ehma: bool | None = None) -> pd.DataFrame:
    """
    Returns ['Close', 'N-Line', 'MHULL', 'SHULL', 'TREND'].

    From the Pine:
        HULL   = selectedMA(src, length)
        MHULL  = HULL[0]          -- current value
        SHULL  = HULL[2]          -- value two bars ago
        colour = HULL > HULL[2] ? BLUE : RED
        N-Line = ema(close, 100)

    TREND is the ribbon colour as a word: 'Bullish' when blue (MHULL > SHULL),
    'Bearish' when red. The sample workbook uses exactly those two words.
    """
    length = length or config.TW_LENGTH
    mode = mode or config.TW_MODE
    nline_length = nline_length or config.TW_NLINE_LENGTH
    if use_standard_ehma is None:
        use_standard_ehma = config.TW_USE_STANDARD_EHMA

    if df.empty:
        return pd.DataFrame(columns=["Close", "N-Line", "MHULL", "SHULL", "TREND"],
                            index=df.index)

    src = df["close"].astype(float)
    hull = _hull(src, length, mode, use_standard_ehma)

    mhull = hull
    shull = hull.shift(2)

    trend = pd.Series(np.where(mhull > shull, "Bullish", "Bearish"), index=df.index)
    trend = trend.where(shull.notna() & mhull.notna(), "")

    return pd.DataFrame({
        "Close": src,
        "N-Line": _ema(src, nline_length),
        "MHULL": mhull,
        "SHULL": shull,
        "TREND": trend,
    })


def tw_recommendation(tw: pd.DataFrame,
                      close: pd.Series | None = None,
                      require_nline: bool = True) -> pd.Series:
    """
    Harish's rule, rebuilt AGAIN 16-Aug-26 (ADANIENT 09:55 chart check):
    back to a plain STATE READ, held for as long as the condition holds --
    not a one-shot crossover+confirmation event. His own description:
    "When candle is above Blue Ribbon and N-Line, it should be BUY CE till
    the candle reaches on ribbon or below N-Line" -- i.e. the signal
    reverts to WAIT automatically the moment either condition breaks,
    which a plain per-bar state read already does; no separate "hold"
    machinery needed.

    Both the crossover-trigger version (earlier 16-Aug-26) and the
    2-candle+RSI-trend version (15-Aug-26) were rejected by this same
    real-chart check: ADANIENT's 09:55 candle sat clearly above both the
    ribbon and the N-Line, and both of those versions still read WAIT
    there because neither one fires on a bar that's simply, currently,
    above the lines -- they need a fresh crossover or a freshly-confirming
    2nd candle right at that bar. A plain state read doesn't have that
    problem: any bar satisfying the condition reads the signal, for as
    long as it keeps satisfying it.

        BUY CE : close ABOVE the blue ribbon AND above the N-Line
        BUY PE : close BELOW the red ribbon AND below the N-Line
        else   : WAIT

    "Above the ribbon" means above the TOP of the MHULL/SHULL band, not
    merely above one edge. require_nline=False drops the N-Line half of
    the condition, ribbon alone, for comparison.
    """
    close = tw["Close"] if close is None else close

    mhull, shull = tw["MHULL"], tw["SHULL"]
    band_top = pd.concat([mhull, shull], axis=1).max(axis=1)
    band_bottom = pd.concat([mhull, shull], axis=1).min(axis=1)
    is_blue = mhull > shull

    long_ok = is_blue & (close > band_top)
    short_ok = (~is_blue) & (close < band_bottom)
    valid = mhull.notna() & shull.notna()

    if require_nline:
        nline = tw.get("N-Line")
        if nline is None:
            raise ValueError(
                "N-Line column required when require_nline=True -- pass the "
                "full frame from tw_all_in_one(), not just the band columns."
            )
        long_ok &= close > nline
        short_ok &= close < nline
        valid &= nline.notna()

    out = pd.Series(config.SIGNAL_WAIT, index=tw.index, dtype=object)
    out[long_ok] = config.SIGNAL_BUY_CE
    out[short_ok] = config.SIGNAL_BUY_PE
    return out.where(valid, "")


# --------------------------------------------------------------------------
# 4. EMA + VWAP Ribbon  --  Pine v6, 05-Aug-26
# --------------------------------------------------------------------------
def vwap(df: pd.DataFrame, source: str | None = None) -> pd.Series:
    """
    Session-anchored VWAP: cumulative(price*volume) / cumulative(volume),
    resetting at the start of every trading day. Matches Pine's built-in
    ta.vwap() on an intraday chart, which auto-resets on session/day
    boundaries. `df` may span many days (warm-up lookback) -- each
    calendar date gets its own cumulative sum, never leaking into the next.

    source: "hlc3" (script default, (H+L+C)/3) or "close".
    """
    source = source or config.VWAP_SOURCE
    if df.empty:
        return pd.Series(dtype=float, index=df.index)

    if source == "close":
        price = df["close"].astype(float)
    else:
        price = (df["high"].astype(float) + df["low"].astype(float)
                 + df["close"].astype(float)) / 3.0
    volume = df["volume"].astype(float)

    day = pd.Series(df.index.date, index=df.index)
    cum_pv = (price * volume).groupby(day).cumsum()
    cum_vol = volume.groupby(day).cumsum()
    return cum_pv / cum_vol.replace(0, np.nan)


# --------------------------------------------------------------------------
# ATR -- not from any of his scripts, needed for position sizing later
# --------------------------------------------------------------------------
def atr(df: pd.DataFrame, period: int | None = None) -> pd.Series:
    period = period or config.ATR_PERIOD
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


# --------------------------------------------------------------------------
def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """
    Every indicator for one symbol, aligned to df.index.

    Columns: Close, Open, High, Low, RSI, RSI EMA9, DI+, DI-, ADX, N-Line,
    MHULL, SHULL, TREND, EMA20, VWAP, ATR

    Open/High/Low (15-Aug-26) are carried alongside Close so
    tw_recommendation()'s two-candle entry confirmation can compare a full
    OHLC bar against the previous one -- every other indicator here only
    ever needed Close.
    """
    if df.empty:
        return pd.DataFrame(index=df.index)

    out = pd.DataFrame(index=df.index)
    out["Close"] = df["close"].astype(float)
    out["Open"] = df["open"].astype(float)
    out["High"] = df["high"].astype(float)
    out["Low"] = df["low"].astype(float)
    out["RSI"] = rsi(df["close"])
    # EMA9 of the RSI line itself -- Pine ta.ema(rsi, 9) equivalent. Feeds
    # the RSI/EMA9 crossover rule in matrix_sheets.rsi_recommendation()
    # (rsi_mode == "ema_cross", 05-Aug-26): RSI above this = BUY CE, below
    # = BUY PE. NaN wherever RSI itself is still NaN (warm-up), so it
    # can't manufacture a signal ema() alone wouldn't otherwise have.
    out["RSI EMA9"] = _ema(out["RSI"], config.RSI_EMA_LENGTH)

    adx_df = adx_di(df)
    out["DI+"] = adx_df["DI+"]
    out["DI-"] = adx_df["DI-"]
    out["ADX"] = adx_df["ADX"]

    tw = tw_all_in_one(df)
    out["N-Line"] = tw["N-Line"]
    out["MHULL"] = tw["MHULL"]
    out["SHULL"] = tw["SHULL"]
    out["TREND"] = tw["TREND"]

    # EMA20-VWAP Ribbon (05-Aug-26): feeds ema_vwap_recommendation() in
    # matrix_sheets.py -- close above BOTH = BUY CE, below BOTH = BUY PE,
    # else WAIT. EMA20 on Close (script's own emaLen=9 default overridden
    # to 20 per Harish's explicit instruction), VWAP resets every session.
    out["EMA20"] = _ema(out["Close"], config.EMA_VWAP_LENGTH)
    out["VWAP"] = vwap(df, config.VWAP_SOURCE)

    out["ATR"] = atr(df)
    return out


if __name__ == "__main__":
    idx = pd.date_range("2026-07-31 09:15", periods=300, freq="5min")
    rng = np.random.default_rng(7)
    close = pd.Series(1470 + np.cumsum(rng.normal(0, 1.2, 300)), index=idx)
    frame = pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close + 1.5, "low": close - 1.5, "close": close, "volume": 1000,
    }, index=idx)

    res = compute_all(frame)
    print("columns:", list(res.columns))

    r = res["RSI"].dropna()
    print(f"\nRSI ({config.RSI_SOURCE}) range {r.min():.2f} - {r.max():.2f}")
    assert r.between(0, 100).all(), "RSI escaped 0-100"

    a = res["ADX"].dropna()
    print(f"ADX range {a.min():.2f} - {a.max():.2f}")
    assert a.between(0, 100).all(), "ADX escaped 0-100"

    tw = tw_all_in_one(frame)
    from collections import Counter
    print(f"\nTREND: {Counter(tw['TREND'].tolist())}")

    full = res.copy()
    ribbon_only = tw_recommendation(full, require_nline=False)
    with_nline = tw_recommendation(full, require_nline=True)
    print(f"TW Recomm, ribbon only     : {Counter(ribbon_only.tolist())}")
    print(f"TW Recomm, ribbon + N-Line : {Counter(with_nline.tolist())}")

    sig_before = (ribbon_only.isin([config.SIGNAL_BUY_CE, config.SIGNAL_BUY_PE])).sum()
    sig_after = (with_nline.isin([config.SIGNAL_BUY_CE, config.SIGNAL_BUY_PE])).sum()
    print(f"N-Line filter removed {sig_before - sig_after} of {sig_before} signals "
          f"({(1 - sig_after / max(sig_before, 1)) * 100:.0f}%)")

    # A BUY CE must never occur while the ribbon is red, and vice versa --
    # same bar this time, since the rule is a plain state read again, not
    # a shifted crossover+confirmation event.
    bad_ce = ((with_nline == config.SIGNAL_BUY_CE) & (tw["TREND"] == "Bearish")).sum()
    bad_pe = ((with_nline == config.SIGNAL_BUY_PE) & (tw["TREND"] == "Bullish")).sum()
    assert bad_ce == 0 and bad_pe == 0, "ribbon colour and signal disagree"

    # Direct re-derivation, independent of tw_recommendation()'s own code:
    # every BUY CE must have close above BOTH the ribbon top and the
    # N-Line, on the SAME bar. Mirror for PE.
    mhull, shull = full["MHULL"], full["SHULL"]
    band_top = pd.concat([mhull, shull], axis=1).max(axis=1)
    band_bottom = pd.concat([mhull, shull], axis=1).min(axis=1)
    close, nline = full["Close"], full["N-Line"]

    ce_mask = with_nline == config.SIGNAL_BUY_CE
    pe_mask = with_nline == config.SIGNAL_BUY_PE
    ce_confirmed = (close > band_top) & (close > nline)
    pe_confirmed = (close < band_bottom) & (close < nline)
    assert (ce_mask & ~ce_confirmed).sum() == 0, "BUY CE without close above ribbon AND N-Line"
    assert (pe_mask & ~pe_confirmed).sum() == 0, "BUY PE without close below ribbon AND N-Line"

    # A held state should sustain across consecutive bars while price
    # keeps clearing both lines -- unlike the crossover-event version,
    # back-to-back identical signals are expected, not a bug.
    consecutive_ce = (with_nline == config.SIGNAL_BUY_CE) & (with_nline.shift(1) == config.SIGNAL_BUY_CE)
    print(f"consecutive BUY CE bars (state held): {int(consecutive_ce.sum())}")

    print("\nindicator self-check passed")

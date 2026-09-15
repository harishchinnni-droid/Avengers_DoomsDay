"""
STEP 8a — Indicators. Direct translations of Harish's Pine scripts.

SOURCE SCRIPTS
--------------
  ADX & DI.txt                  (c) BeikabuOyaji, Pine v4
  RSI Multi Length_LuxAlgo.txt  (c) LuxAlgo, Pine v5, CC BY-NC-SA 4.0
  TW All In One.txt             Pine v4
  Harish TW EMA + VWAP.txt      Pine v6, 12-Sep-26 -- same Hull/N-Line as
                                TW All In One, plus an EMA9 line and
                                Dot/Triangle cross-event markers (see
                                harish_dot_triangle / harish_recommendation)

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
                      require_nline: bool = True,
                      require_target_line: bool = False) -> pd.Series:
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

    require_target_line=True (config.TW_TARGET_LINE_CONFIRM_REQUIRED, added
    09-Sep-26) adds a THIRD gate on top of ribbon + N-Line: the quick
    target/stop-loss pivot pair (see tw_target_lines -- Level1 Color /
    Level2 Color, both "green"/"red") must also agree with the signal
    direction --

        BUY CE also needs BOTH Level1 Color and Level2 Color == "green"
        BUY PE also needs BOTH Level1 Color and Level2 Color == "red"

    `tw` must then carry those two columns (compute_all()/tw_target_lines()
    merges them in) -- missing columns raise, same as N-Line above.
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

    if require_target_line:
        level1_col = tw.get("Level1 Color")
        level2_col = tw.get("Level2 Color")
        if level1_col is None or level2_col is None:
            raise ValueError(
                "Level1 Color / Level2 Color columns required when "
                "require_target_line=True -- pass a frame that has been "
                "through tw_target_lines() / indicators.compute_all()."
            )
        long_ok &= (level1_col == "green") & (level2_col == "green")
        short_ok &= (level1_col == "red") & (level2_col == "red")
        valid &= level1_col.notna() & level2_col.notna()

    out = pd.Series(config.SIGNAL_WAIT, index=tw.index, dtype=object)
    out[long_ok] = config.SIGNAL_BUY_CE
    out[short_ok] = config.SIGNAL_BUY_PE
    return out.where(valid, "")


# --------------------------------------------------------------------------
# 3b. TW target/stop-loss lines -- quick pivots (level1 / level2), added
#     09-Sep-26 per Harish: "check the color of the Thin RED & Green line,
#     which is target line. If BUY CE then this line should be Green, if
#     BUY PE then line should be RED." Only the QUICK pair (his choice --
#     see config.TW_TARGET_LEFT / TW_TARGET_QUICK_RIGHT) is translated;
#     the script also plots 12 slower levels (level3..level14, right=21)
#     that this does not touch.
# --------------------------------------------------------------------------
def _pivot_high(src: pd.Series, left: int, right: int) -> pd.Series:
    """
    ta.pivothigh(src, left, right) -- a bar is a pivot high when it is the
    max of the window [i-left, i+right] (inclusive). Value sits at the
    PIVOT bar's own index (matching Pine), so it is only knowable once
    `right` bars later have closed -- NaN for the last `right` bars of the
    series and the first `left` bars, same as Pine's own warm-up/repaint
    behaviour.

    Ties (two bars in the window sharing the exact max) are both flagged --
    Pine's own tie-break is rarely hit on real float prices, and is not
    worth the extra complexity here.
    """
    back = src.rolling(left + 1).max()          # max of src[i-left .. i]
    fwd = src[::-1].rolling(right + 1).max()[::-1]  # max of src[i .. i+right]
    valid = back.notna() & fwd.notna()
    window_max = pd.concat([back, fwd], axis=1).max(axis=1)
    return src.where(valid & (src >= window_max))


def _pivot_low(src: pd.Series, left: int, right: int) -> pd.Series:
    """Mirror of _pivot_high -- ta.pivotlow(src, left, right)."""
    back = src.rolling(left + 1).min()
    fwd = src[::-1].rolling(right + 1).min()[::-1]
    valid = back.notna() & fwd.notna()
    window_min = pd.concat([back, fwd], axis=1).min(axis=1)
    return src.where(valid & (src <= window_min))


def _valuewhen0(condition_notna: pd.Series, source: pd.Series) -> pd.Series:
    """
    valuewhen(condition, source, 0) -- the value of `source` on the most
    recent bar where `condition` was non-na, held constant (forward-filled)
    until a newer occurrence replaces it. NaN before the first occurrence.
    """
    return source.where(condition_notna).ffill()


def tw_target_lines(df: pd.DataFrame, left: int | None = None,
                    quick_right: int | None = None) -> pd.DataFrame:
    """
    Level1 / Level2 -- the "quick" pivot pair from the target/stop-loss
    section of the Pine script (src_auto_sr == "Close", so both pivots are
    found on CLOSE, not high/low):

        quick_pivot_high = pivothigh(close, left, quick_right)
        quick_pivot_lows = pivotlow(close, left, quick_right)
        level1 = valuewhen(quick_pivot_high, close[quick_right], 0)
        level2 = valuewhen(quick_pivot_lows, close[quick_right], 0)
        level1_col = close >= level1 ? green : red
        level2_col = close >= level2 ? green : red

    Note the script's own close[quick_right] offset -- the value HELD by
    valuewhen is the close from `quick_right` bars before the pivot bar
    itself, not the pivot bar's own close. Reproduced literally here rather
    than "corrected", so this matches what actually plots on his chart.

    Returns ['Level1', 'Level2', 'Level1 Color', 'Level2 Color'], colors as
    the strings "green" / "red" (na wherever the level itself is still na).
    """
    left = config.TW_TARGET_LEFT if left is None else left
    quick_right = (config.TW_TARGET_QUICK_RIGHT if quick_right is None
                  else quick_right)

    if df.empty:
        return pd.DataFrame(columns=["Level1", "Level2", "Level1 Color",
                                     "Level2 Color"], index=df.index)

    close = df["close"].astype(float)
    shifted_close = close.shift(quick_right)

    quick_pivot_high = _pivot_high(close, left, quick_right)
    quick_pivot_low = _pivot_low(close, left, quick_right)

    level1 = _valuewhen0(quick_pivot_high.notna(), shifted_close)
    level2 = _valuewhen0(quick_pivot_low.notna(), shifted_close)

    level1_color = pd.Series(np.where(close >= level1, "green", "red"),
                             index=df.index).where(level1.notna())
    level2_color = pd.Series(np.where(close >= level2, "green", "red"),
                             index=df.index).where(level2.notna())

    return pd.DataFrame({
        "Level1": level1,
        "Level2": level2,
        "Level1 Color": level1_color,
        "Level2 Color": level2_color,
    })


# --------------------------------------------------------------------------
# 3c. Harish TW EMA + VWAP  --  Pine v6, 12-Sep-26
# --------------------------------------------------------------------------
# The new script layers two EVENT markers on top of the same Hull ribbon +
# N-Line the TW All in One section above already computes:
#
#   plotshape(buySignal/sellSignal, triangleup/triangledown)
#       buySignal  = ta.crossunder(SHULL, MHULL)   -- ribbon just flipped
#                    Bullish (blue) this bar
#       sellSignal = ta.crossover(SHULL, MHULL)    -- ribbon just flipped
#                    Bearish (red) this bar
#   plotshape(priceAboveEMA/priceBelowEMA, circle)
#       priceAboveEMA = ta.crossover(close, ema9)  -- close just crossed
#                       above EMA9 this bar
#       priceBelowEMA = ta.crossunder(close, ema9) -- close just crossed
#                       below EMA9 this bar
#
# Both are one-bar EVENTS (crossover/crossunder), not held states like
# TREND -- true only on the bar the cross happens, blank otherwise. Encoded
# here as a single string column each ("Green"/"Red"/"") rather than two
# booleans, to read the same way TREND already does.
def _cross_event(condition_now: pd.Series, valid: pd.Series) -> pd.Series:
    """
    True only on the bar `condition_now` turns True after being False on
    the immediately preceding (valid) bar -- ta.crossover/crossunder's
    "first bar of" semantics. `valid` gates out bars where the underlying
    comparison is meaningless (warm-up NaNs), on both this bar and the one
    before it, so a NaN-driven False doesn't get misread as a real cross.

    BUG FIXED 12-Sep-26 (Harish: "DOT and Triangle is coming for every row"):
    Series.shift() on a bool column upcasts to object dtype so it can hold
    the NaN it introduces at the edge. `~` on an object-dtype Series then
    dispatches to Python's bitwise ~ on the underlying True/False objects --
    ~True == -2, ~False == -1, BOTH NONZERO, i.e. both truthy. So the old
    "prev_false = ~condition_now.shift(1).fillna(True)" line was truthy on
    EVERY bar regardless of the actual previous value, silently turning this
    into a plain state read (colour on every bar current condition holds)
    instead of a one-bar crossover/crossunder event. `.astype(bool)` after
    `.fillna()` forces a real boolean dtype so `~` negates correctly.
    """
    prev_valid = valid.shift(1).fillna(False).astype(bool)
    prev_condition = condition_now.shift(1).fillna(True).astype(bool)
    return valid & prev_valid & condition_now & ~prev_condition


def harish_dot_triangle(close: pd.Series, ema9: pd.Series,
                        mhull: pd.Series, shull: pd.Series
                        ) -> tuple[pd.Series, pd.Series]:
    """
    Returns (Dot, Triangle) -- each a string Series, "Green"/"Red"/"".

    Dot      : close crossing EMA9 (script's coloured circle)
    Triangle : Hull ribbon flipping colour (script's coloured triangle)
    """
    ema_valid = close.notna() & ema9.notna()
    above_ema = close > ema9
    green_dot = _cross_event(above_ema, ema_valid)
    red_dot = _cross_event(~above_ema, ema_valid)

    hull_valid = mhull.notna() & shull.notna()
    is_blue = mhull > shull
    green_tri = _cross_event(is_blue, hull_valid)
    red_tri = _cross_event(~is_blue, hull_valid)

    dot = pd.Series("", index=close.index, dtype=object)
    dot[green_dot] = "Green"
    dot[red_dot] = "Red"

    triangle = pd.Series("", index=close.index, dtype=object)
    triangle[green_tri] = "Green"
    triangle[red_tri] = "Red"

    return dot, triangle


def harish_recommendation(ind: pd.DataFrame) -> pd.Series:
    """
    Harish's rule for the new "Harish TW EMA + VWAP" script, SIMPLIFIED
    12-Sep-26 (see revision history below).

    Current rule: Dot and Triangle showing up on ADJACENT bars, in EITHER
    order, with the SECOND of those two bars closing in the matching
    direction:

        marker[bar] == marker[bar-1] == "Green", bar closes Bullish
            -> BUY CE, signalled on `bar`
        marker[bar] == marker[bar-1] == "Red", bar closes Bearish
            -> BUY PE, signalled on `bar`

    `marker` is the merged "Dot/Triangle" event column (see
    compute_harish/the merge below) -- "Green"/"Red" on whichever bar an
    EMA9 cross (Dot) or Hull ribbon flip (Triangle) happens, blank
    otherwise, with Dot's colour winning display when BOTH fire on the
    SAME bar (see the CONFLICT note below -- that same-bar case is real,
    not theoretical, and this function treats it specially for the entry
    rule even though the sheet still shows Dot's colour).

    CONFLICT (found 12-Sep-26, WIPRO real workbook: sheet showed Red at
    10:15 and 10:20, Harish Recomm fired BUY PE at 10:20, but the actual
    TradingView chart showed a GREEN triangle around 10:15, not red --
    "however I do not see any Red triangle in the chart"). Traced against
    the real cached WIPRO 11-Sep-26 candles: at 10:15, Dot fired Red AND
    Triangle fired Green on the SAME bar -- both conditions are genuinely
    independent (an EMA9 cross and a Hull-ribbon flip can point opposite
    ways on the same candle) and NOT rare the way the original merge
    comment assumed. compute_harish's "Dot/Triangle" display column picks
    Dot's colour on that bar (Red), which is why the sheet read Red while
    the chart's own triangle plotshape was genuinely green -- the sheet
    was not wrong about the Dot, it just couldn't show a second,
    conflicting Triangle colour in the same cell.
    Per Harish's decision on this exact case: the sheet KEEPS showing
    Dot's colour on a conflict (no display change), but such a bar counts
    as NEITHER colour for the two-adjacent-bars entry rule below -- a bar
    where Dot and Triangle actively disagree is not the clean "both show
    up matching" pattern the rule is built on, so it must not anchor a
    BUY CE/PE on either side of it. This needed Dot and Triangle kept as
    separate raw columns (both now on `ind`, see compute_harish) purely so
    this function can detect the conflict -- the merge itself is
    unchanged.

    REVISION HISTORY
    -----------------
    12-Sep-26 (first version): required Dot on candle A, THEN Triangle on
    the very next candle B specifically (not the reverse order), AND B
    closing in the matching direction, AND B's close beyond the ribbon top/
    bottom AND N-Line AND EMA9 -- six conditions all at once. Harish's own
    follow-up, after reviewing real output: "if both comes next to each
    other where 2nd candle is Bullish then place an order. No need to wait
    for 2 BUY signals now." -- i.e. drop the ribbon/N-Line/EMA9 gates
    entirely, and accept EITHER order (Triangle-then-Dot counts same as
    Dot-then-Triangle), since requiring all six to line up made the signal
    fire too rarely to be useful as an entry trigger. The "2 BUY signals"
    line refers to the SEPARATE order_engine qualification gate
    (config.CONSECUTIVE_SIGNALS_REQUIRED, normally 2 consecutive identical
    Final Recomm bars before an order is placed) -- since this rule is
    already a one-bar decisive event, run_HARISH.py relaxes that gate to
    config.HARISH_CONSECUTIVE_SIGNALS_REQUIRED (=1) for its own process
    only, never touching the shared default other pipelines use.
    12-Sep-26 (second follow-up, same day): added the same-bar CONFLICT
    handling documented above, after the WIPRO false-signal report --
    without it, a Dot/Triangle disagreement on one bar could silently
    anchor a BUY CE/PE that the real chart never actually supported.
    12-Sep-26 (third follow-up, same day, AXISBANK): Harish caught a
    second variant of the same underlying issue -- "Red Triangle with
    Bullish candle at 9.15am, the Red dot was actually on previous day...
    Both Dot & Triangle should be from same day". The two-adjacent-bars
    rule was pairing bars by DataFrame position only, so a Dot on
    yesterday's last candle could pair with a Triangle on today's very
    first candle and fire an entry the two events never actually agreed
    on together (they're 1 minute apart in row terms, but a full
    overnight gap apart in market terms). Added a same-calendar-day guard
    that blanks `prev_marker` whenever the previous row is from a
    different date -- see the code below. This makes the first bar of
    every trading day unable to complete a pair, which is correct: it has
    no legitimate previous bar within the same session to pair with.

    `ind` must carry Close, Open, the merged "Dot/Triangle" column, and the
    raw "Dot"/"Triangle" columns -- the shape indicators.compute_harish()
    produces.
    """
    required = ["Close", "Open", "Dot/Triangle", "Dot", "Triangle"]
    missing = [c for c in required if c not in ind.columns]
    if missing:
        raise ValueError(f"harish_recommendation missing column(s): {missing}")

    close, open_ = ind["Close"], ind["Open"]
    marker = ind["Dot/Triangle"]
    dot, triangle = ind["Dot"], ind["Triangle"]

    # A bar where Dot and Triangle BOTH fired but disagree on colour counts
    # as neither, for the entry rule only -- the display column above is
    # untouched. See the CONFLICT note in this function's own docstring.
    conflict = (dot != "") & (triangle != "") & (dot != triangle)
    rule_marker = marker.where(~conflict, "")
    prev_marker = rule_marker.shift(1)  # the OTHER bar of the adjacent pair

    # SAME-DAY GUARD (12-Sep-26, AXISBANK follow-up): "adjacent bar" above
    # means adjacent ROW, which says nothing about the calendar -- the very
    # first bar of a trading day sits right after the previous day's last
    # bar in the DataFrame. Without this guard, a Dot left over from
    # yesterday's close could pair with a fresh Triangle on today's 09:15
    # candle and fire an entry neither event actually agreed on together.
    # Harish's explicit instruction: "Both Dot & Triangle should be from
    # same day" -- so `prev_marker` is blanked whenever the previous row
    # belongs to a different calendar day, which makes the first bar of
    # every session unable to complete a pair (correct: there is no
    # legitimate "previous bar" for it to pair with).
    day = pd.Series(ind.index.date, index=ind.index)
    same_day = day == day.shift(1)
    prev_marker = prev_marker.where(same_day, "")

    bullish = close > open_
    bearish = close < open_

    buy_ce = (rule_marker == "Green") & (prev_marker == "Green") & bullish
    buy_pe = (rule_marker == "Red") & (prev_marker == "Red") & bearish

    valid = close.notna() & open_.notna()

    out = pd.Series(config.SIGNAL_WAIT, index=ind.index, dtype=object)
    out[buy_ce] = config.SIGNAL_BUY_CE
    out[buy_pe] = config.SIGNAL_BUY_PE
    return out.where(valid, "")


def compute_harish(df: pd.DataFrame) -> pd.DataFrame:
    """
    Full indicator set for the standalone Harish TW EMA + VWAP pipeline
    (run_HARISH.py / matrix_sheets_harish.py) -- Close, Open, EMA9, N-Line,
    MHULL, SHULL, TREND, the merged "Dot/Triangle" event column, and the two
    raw "Dot"/"Triangle" columns (unmerged).

    "Dot/Triangle" merges the two event types Harish asked to combine into
    one row (12-Sep-26): whichever of the two fired ("Green"/"Red"), Dot
    taking priority when BOTH fire on the same bar. Both firing on the same
    bar turned out NOT to be rare (found 12-Sep-26 on real WIPRO data, Dot
    Red + Triangle Green on the same candle) -- see harish_recommendation's
    CONFLICT note for how that case is handled for the entry rule, and why
    the display column still just shows Dot's colour regardless.

    "Dot" and "Triangle" (added 12-Sep-26) are kept separately, unmerged,
    for two independent reasons: (1) order_engine._harish_dot_fast_exit
    watches "Dot" alone -- Harish asked for an immediate exit the moment a
    single OPPOSITE-colour Dot appears, faster/looser than the full Harish
    Recomm reversal -- and (2) harish_recommendation needs BOTH raw columns
    to detect the same-bar conflict above. Neither is added to
    MATRIX_SHEETS_HARISH -- the visible per-symbol sheet still only shows
    the merged column, per Harish's "keep Dot and Triangle in the same row"
    instruction -- but "Dot" IS added to FINAL_ROWS_HARISH so it lands in
    the Final sheet where order_engine already reads per-bar slot values
    (same shape as MACD Recomm/Harish Recomm). "Triangle" stays internal to
    this DataFrame -- only harish_recommendation reads it, so it never
    needed a Final-sheet slot.

    Deliberately kept OUT of compute_all() / the main run_TW_ALL.py
    pipeline (Harish, 12-Sep-26: "I wanted a separate code, do not touch
    run_TW_ALL.py"). Same pattern as ema.py/pivot_points.py feeding
    matrix_sheets_ema_pivot.py as their own pipeline, alongside compute_all()
    feeding the original -- this is a third, independent entry point into
    the same tw_all_in_one() Hull/N-Line math, not a change to it.
    """
    if df.empty:
        return pd.DataFrame(columns=["Close", "Open", "EMA9", "N-Line",
                                     "MHULL", "SHULL", "TREND",
                                     "Dot/Triangle", "Dot", "Triangle"],
                            index=df.index)

    out = pd.DataFrame(index=df.index)
    out["Close"] = df["close"].astype(float)
    out["Open"] = df["open"].astype(float)

    tw = tw_all_in_one(df)
    out["N-Line"] = tw["N-Line"]
    out["MHULL"] = tw["MHULL"]
    out["SHULL"] = tw["SHULL"]
    out["TREND"] = tw["TREND"]

    out["EMA9"] = _ema(out["Close"], config.HARISH_EMA_LENGTH)
    dot, triangle = harish_dot_triangle(out["Close"], out["EMA9"],
                                       out["MHULL"], out["SHULL"])
    out["Dot/Triangle"] = dot.where(dot != "", triangle)
    out["Dot"] = dot
    out["Triangle"] = triangle
    return out


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
    MHULL, SHULL, TREND, Level1, Level2, Level1 Color, Level2 Color, EMA20,
    VWAP, ATR

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

    target = tw_target_lines(df)
    out["Level1"] = target["Level1"]
    out["Level2"] = target["Level2"]
    out["Level1 Color"] = target["Level1 Color"]
    out["Level2 Color"] = target["Level2 Color"]

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

    # Harish TW EMA + VWAP: Dot and Triangle are each EVENTS, not held state
    # -- each must be blank most bars and never repeat ITS OWN colour on two
    # consecutive bars (that would mean the underlying condition never
    # actually flipped away and back in between). Regression guard for the
    # 12-Sep-26 bug where ~bool_series.shift(1) silently became a no-op
    # (object-dtype bitwise NOT instead of boolean negation), turning every
    # event column into a state column -- colour on every bar. See
    # _cross_event's docstring for the full explanation.
    #
    # NOTE (12-Sep-26, rule simplification): the merged "Dot/Triangle" column
    # itself CAN legitimately show the same colour on two consecutive bars --
    # that is exactly Harish's entry pattern (Dot on bar A, Triangle on bar
    # B, or vice versa, both the same colour). So the no-repeat check below
    # is done on Dot and Triangle SEPARATELY, before merging, not on the
    # merged column.
    harish = compute_harish(frame)
    dot, triangle = harish_dot_triangle(harish["Close"], harish["EMA9"],
                                        harish["MHULL"], harish["SHULL"])
    n_bars = len(harish)
    marker_events = (harish["Dot/Triangle"] != "").sum()
    print(f"\nHarish Dot/Triangle events: {marker_events}/{n_bars} "
          f"({marker_events / n_bars:.1%})")
    assert marker_events < n_bars * 0.5, "Dot/Triangle firing on >=50% of bars -- state, not event"
    for name, series in (("Dot", dot), ("Triangle", triangle)):
        for colour in ("Green", "Red"):
            repeat = (series == colour) & (series.shift(1) == colour)
            assert not repeat.any(), f"{name} shows {colour} on two consecutive bars"

    # compute_harish's own "Dot"/"Triangle" columns (added 12-Sep-26 for
    # order_engine._harish_dot_fast_exit and harish_recommendation's
    # conflict check, respectively) must be byte-identical to the raw
    # `dot`/`triangle` computed independently just above -- same events,
    # exposed unmerged specifically so those two checks can watch them.
    assert (harish["Dot"] == dot).all(), "compute_harish's Dot column diverges from harish_dot_triangle's own dot output"
    assert (harish["Triangle"] == triangle).all(), "compute_harish's Triangle column diverges from harish_dot_triangle's own triangle output"

    # Direct re-derivation of harish_recommendation, independent of its own
    # code: every BUY CE must have marker=="Green" on THIS bar and the bar
    # before it (with NEITHER bar a Dot/Triangle conflict -- see below),
    # and this bar closing Bullish. Mirror for PE. (12-Sep-26, simplified
    # rule -- no ribbon/N-Line/EMA9 gates anymore.)
    harish["Harish Recomm"] = harish_recommendation(harish)
    marker = harish["Dot/Triangle"]
    conflict = (dot != "") & (triangle != "") & (dot != triangle)
    rule_marker = marker.where(~conflict, "")
    prev_rule_marker = rule_marker.shift(1)
    day = pd.Series(harish.index.date, index=harish.index)
    prev_rule_marker = prev_rule_marker.where(day == day.shift(1), "")
    ce_mask = harish["Harish Recomm"] == config.SIGNAL_BUY_CE
    pe_mask = harish["Harish Recomm"] == config.SIGNAL_BUY_PE
    ce_ok = (rule_marker == "Green") & (prev_rule_marker == "Green") & (harish["Close"] > harish["Open"])
    pe_ok = (rule_marker == "Red") & (prev_rule_marker == "Red") & (harish["Close"] < harish["Open"])
    assert (ce_mask & ~ce_ok).sum() == 0, "BUY CE without adjacent same-day (non-conflicting) Green markers + Bullish close"
    assert (pe_mask & ~pe_ok).sum() == 0, "BUY PE without adjacent same-day (non-conflicting) Red markers + Bearish close"
    print(f"Harish Recomm: {int(ce_mask.sum())} BUY CE, {int(pe_mask.sum())} BUY PE")
    if conflict.any():
        print(f"Dot/Triangle conflicts (both fired, disagreeing colour) on "
              f"{int(conflict.sum())} synthetic bar(s) -- confirmed none of "
              f"them anchored a BUY CE/PE above.")

    # Targeted regression test for the WIPRO false-signal case (12-Sep-26):
    # Dot=Red and Triangle=Green on the SAME bar, immediately followed by a
    # clean Triangle=Red on the next bar with a Bearish close -- the naive
    # merge (Dot wins the conflicted bar -> "Red") would read as two
    # adjacent Red bars and fire BUY PE. It must NOT.
    conflict_idx = harish.index[:6]
    synth = pd.DataFrame({
        "Close":  [100.0, 100.0, 99.0, 98.5, 98.0, 97.5],
        "Open":   [100.0, 100.2, 100.0, 99.0, 98.5, 98.0],
        "Dot/Triangle": ["", "Red", "Red", "", "", ""],
        "Dot":          ["", "Red", "",    "", "", ""],
        "Triangle":     ["", "Green", "Red", "", "", ""],
    }, index=conflict_idx)
    synth_recomm = harish_recommendation(synth)
    assert synth_recomm.iloc[2] != config.SIGNAL_BUY_PE, (
        "WIPRO-style conflict regression: a same-bar Dot/Triangle "
        "disagreement must not anchor a BUY PE on the following bar")
    print("WIPRO-style Dot/Triangle conflict regression test passed "
          "(conflicting bar correctly did not anchor a BUY PE)")

    # Targeted regression test for the AXISBANK cross-day case (12-Sep-26):
    # a Red Dot on the PREVIOUS day's last candle followed by a Red
    # Triangle on the NEXT day's very first candle, that first candle
    # closing Bearish -- same colour, "adjacent" by row position, but from
    # two different trading sessions. Must NOT fire BUY PE.
    day1_idx = pd.date_range("2026-07-01 15:20", periods=2, freq="5min")
    day2_idx = pd.date_range("2026-07-02 09:15", periods=1, freq="5min")
    cross_day_idx = day1_idx.append(day2_idx)
    cross_day = pd.DataFrame({
        "Close":  [101.0, 100.0, 98.0],
        "Open":   [101.0, 100.5, 99.0],
        "Dot/Triangle": ["", "Red", "Red"],
        "Dot":          ["", "Red", ""],
        "Triangle":     ["", "",    "Red"],
    }, index=cross_day_idx)
    cross_day_recomm = harish_recommendation(cross_day)
    assert cross_day_recomm.iloc[2] != config.SIGNAL_BUY_PE, (
        "AXISBANK-style cross-day regression: a Dot from the previous "
        "trading day must not pair with a Triangle on the next day's "
        "first candle to anchor a BUY PE")
    print("AXISBANK-style cross-day Dot/Triangle regression test passed "
          "(previous-day Dot correctly did not pair with next-day Triangle)")

    print("\nindicator self-check passed")

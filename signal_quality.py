"""
STEP 10b — Signal quality: volatility filter, index regime gate, ranking.

Why this module exists, in one line each, all measured on the 83-trade
backtest of 28-31 Jul 2026:

  VOLATILITY -- only 14% of trades ever reached +10%. The signal fired in
  stocks that were not moving. An ATM option needs movement to beat theta.

  REGIME -- 29-Jul ran 22 trades at a 9.1% win rate on a directionless index
  day. When NIFTY itself is chopping, individual stock breakouts die into it.

  RANKING -- 21 trades/day paid ~Rs 5,600/day in friction. When five symbols
  qualify at once, the weakest three are subsidising the market.

HONESTY CONSTRAINT ON RANKING
-----------------------------
Signals are ranked only WITHIN one candle slot -- the set that fired
together. Ranking across slots ("take the day's best 3") is look-ahead in a
backtest: at 09:40 you cannot know what will fire at 14:00. The daily cap in
the order engine then limits how many slots get to trade at all,
first-come-first-served, which is exactly how it would work live.

Every judgement uses candles up to the last CLOSED bar before the decision
moment. Nothing here reads the future.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

import config
import indicators
import ist_clock


def _closed_slice(candles: pd.DataFrame, at: datetime) -> pd.DataFrame:
    """Bars fully closed at `at`. The same rule as everywhere else."""
    if candles is None or candles.empty:
        return pd.DataFrame()
    cutoff = candles.index + timedelta(minutes=config.INTERVAL_MINUTES)
    return candles[cutoff <= at]


# --------------------------------------------------------------------------
# 1. volatility filter (per symbol)
# --------------------------------------------------------------------------
def volatility_ok(candles: pd.DataFrame, at: datetime) -> tuple[bool, str]:
    """
    ATR(14) now vs its own recent average. Below the ratio, the stock is
    quieter than usual and the premium-move this strategy needs (+7% in
    minutes) has no fuel.

    Returns (ok, note). Missing data fails CLOSED -- an unmeasurable
    volatility is not a pass.
    """
    if not config.VOL_FILTER_ENABLED:
        return True, "vol filter disabled"

    df = _closed_slice(candles, at)
    need = config.ATR_PERIOD + config.VOL_ATR_LOOKBACK + 5
    if len(df) < need:
        return False, f"vol filter: only {len(df)} closed bars, need {need}"

    atr_series = indicators.atr(df, config.ATR_PERIOD)
    current = float(atr_series.iloc[-1])
    # Baseline EXCLUDES the most recent bars. Including them compares the
    # current ATR mostly against itself and pins the ratio near 1.0 no
    # matter what the stock is doing -- caught by the self-test, where a
    # genuinely tripled volatility scored 1.08. The gap makes the baseline
    # the PRIOR regime, which is the thing worth comparing against.
    gap = config.VOL_BASELINE_GAP
    window = atr_series.iloc[-(config.VOL_ATR_LOOKBACK + gap):-gap]
    baseline = float(window.mean())
    if baseline <= 0:
        return False, "vol filter: zero baseline ATR"

    ratio = current / baseline
    if ratio < config.VOL_MIN_RATIO:
        return False, (f"volatility too low: ATR ratio {ratio:.2f} < "
                       f"{config.VOL_MIN_RATIO:.2f} -- no fuel for the move")

    # Absolute floor. The ratio asks "livelier than usual?"; this asks
    # "moving enough to matter at all?". HINDUNILVR at 1.2x its usual crawl
    # still fails here, which is the point -- all 15 no-follow-through exits
    # in the July backtest were in names this floor excludes.
    price = float(df["close"].iloc[-1])
    atr_pct = current / price if price > 0 else 0.0
    # 1e-6 tolerance: Wilder ATR converges to the true range from below, so
    # a stock sitting exactly on the floor reads a hair under it.
    if atr_pct < config.VOL_MIN_ATR_PCT - 1e-6:
        return False, (f"low momentum stock: ATR {atr_pct * 100:.3f}% of price "
                       f"< {config.VOL_MIN_ATR_PCT * 100:.2f}% -- a +7% premium "
                       f"move is not plausible here")
    return True, f"ATR ratio {ratio:.2f}, ATR {atr_pct * 100:.3f}% of price"


# --------------------------------------------------------------------------
# 1b. RSI-extreme rejection (per symbol) -- added 06-Aug-26 at Harish's
# request, straight off his F&O checklist ("RSI not extreme against the
# trade"). This is a plausibility/exhaustion guard, NOT something the
# 8-day correlation study behind it proved out: |RSI-50| vs realised P&L
# on that sample came back at 0.10 -- weak, and not distinguishable from
# noise at n=234. Keeping it anyway because it matches accepted practice
# (don't buy an already-stretched move) and it's a cheap guardrail either
# way; but it should NOT be sold to him as something the data confirmed.
# Re-check this note once a bigger sample exists.
# --------------------------------------------------------------------------
def rsi_value(candles: pd.DataFrame, at: datetime) -> float | None:
    """
    Current RSI(14) from closed bars only, or None if there isn't a full
    warm-up yet. Factored out of rsi_extreme_ok (15-Aug-26) so a caller
    that just needs the NUMBER -- not a pass/fail gate -- doesn't have to
    duplicate the closed-bar slicing.
    """
    df = _closed_slice(candles, at)
    if len(df) < config.WARMUP_BARS:
        return None
    return float(indicators.rsi(df["close"]).iloc[-1])


def rsi_extreme_ok(candles: pd.DataFrame, at: datetime,
                   signal: str) -> tuple[bool, str]:
    """
    Rejects a BUY CE when RSI is already deep into overbought (the move is
    stretched, chasing it risks buying the top) and a BUY PE when RSI is
    already deep into oversold (same logic, mirrored). Direction-aware --
    RSI extreme WITH the trade (e.g. RSI 25 on a BUY PE) is not rejected,
    only RSI extreme AGAINST it.

    Missing data fails CLOSED, same convention as volatility_ok.
    """
    if not config.RSI_EXTREME_FILTER_ENABLED:
        return True, "RSI-extreme filter disabled"

    rsi_now = rsi_value(candles, at)
    if rsi_now is None:
        return False, "RSI-extreme filter: insufficient closed bars"

    if signal == config.SIGNAL_BUY_CE and rsi_now >= config.RSI_EXTREME_OVERBOUGHT:
        return False, (f"RSI {rsi_now:.0f} >= {config.RSI_EXTREME_OVERBOUGHT:.0f} "
                       f"-- already overbought, buying CE here chases a "
                       f"stretched move")
    if signal == config.SIGNAL_BUY_PE and rsi_now <= config.RSI_EXTREME_OVERSOLD:
        return False, (f"RSI {rsi_now:.0f} <= {config.RSI_EXTREME_OVERSOLD:.0f} "
                       f"-- already oversold, buying PE here chases a "
                       f"stretched move")
    return True, f"RSI {rsi_now:.0f}"


# --------------------------------------------------------------------------
# 1c. candle follow-through between the two trigger candles -- added
# 15-Aug-26, Harish, from watching BAJFINANCE (14-Aug-26) fail: 09:15
# printed a strong BUY CE candle with long wicks, but 09:20 -- the SECOND
# trigger candle, now the last one required since CONSECUTIVE_SIGNALS_
# REQUIRED dropped to 2 -- came in LOWER across every OHLC value, and the
# trade still qualified on indicator confluence alone and stopped out
# almost immediately. Indicator agreement across 2 candles says the SIGNAL
# repeated; it says nothing about whether price itself kept moving.
# --------------------------------------------------------------------------
def candle_momentum_ok(candles: pd.DataFrame, first_at: datetime,
                       second_at: datetime, signal: str) -> tuple[bool, str]:
    """
    Requires the confirming (2nd trigger) candle's CLOSE to clear the
    leading (1st trigger) candle's close, in the signal's direction: BUY CE
    needs close2 > close1, BUY PE needs close2 < close1.

    Loosened from an ALL-OHLC check (15-Aug-26 -> 15-Aug-26, same day,
    Harish's own backtest evidence): requiring every one of open/high/low/
    close to clear was strict enough to reject NESTLEIND and ASIANPAINT --
    that day's only two WINNERS from the prior run -- for the same reason
    it correctly caught BAJFINANCE ("no candle follow-through"). A single
    wick dipping below the prior candle while the close keeps climbing is
    normal inside a real trend, not a stall; close-over-close still catches
    a genuine reversal/stall without vetoing that. 94 of 411 rejections
    that day were this gate -- the single largest rejection reason -- so
    getting the criterion right matters more than most.

    first_at/second_at are the OPEN timestamps of the two trigger candles
    (SignalRun.trigger_slots[0] and [1], converted to datetimes by the
    caller) -- both already closed by the time qualification runs, so no
    closed-bar filtering is needed here the way the other gates need it.
    """
    if not config.CANDLE_MOMENTUM_ENABLED:
        return True, "candle momentum filter disabled"
    if candles is None or candles.empty:
        return False, "candle momentum filter: no candles"

    try:
        close1 = float(candles.loc[first_at, "close"])
        close2 = float(candles.loc[second_at, "close"])
    except KeyError:
        return False, "candle momentum filter: trigger candle(s) not found"

    if signal == config.SIGNAL_BUY_CE:
        if not close2 > close1:
            return False, (f"no candle follow-through: {second_at:%H:%M} close "
                           f"{close2:g} not above {first_at:%H:%M} close {close1:g}")
        return True, "candle follow-through confirmed (higher close)"
    if signal == config.SIGNAL_BUY_PE:
        if not close2 < close1:
            return False, (f"no candle follow-through: {second_at:%H:%M} close "
                           f"{close2:g} not below {first_at:%H:%M} close {close1:g}")
        return True, "candle follow-through confirmed (lower close)"
    return True, ""


# --------------------------------------------------------------------------
# 2. index regime gate (once per decision moment)
# --------------------------------------------------------------------------
def vix_ok(vix_candles: pd.DataFrame | None,
           at: datetime) -> tuple[bool, str]:
    """
    India VIX ceiling for long-premium entries.

    Adopted from F:\\06_Claude_v2 (VIX_MAX = 18.0). Distinct from the
    per-option IV gate: that one asks whether a particular contract is
    expensive, this asks whether the whole market is. On a high-VIX day
    every strike is dear and a premium buyer is paying up regardless of
    which one they pick.

    Missing feed SKIPS the gate rather than failing it -- but says so, so a
    silently absent VIX never masquerades as a passing one.
    """
    if not config.VIX_FILTER_ENABLED:
        return True, "VIX filter disabled"
    if vix_candles is None or vix_candles.empty:
        return True, "VIX filter SKIPPED -- no India VIX feed"

    df = _closed_slice(vix_candles, at)
    if df.empty:
        return True, "VIX filter SKIPPED -- no closed VIX bars yet"

    vix = float(df["close"].iloc[-1])
    if vix > config.VIX_MAX:
        return False, (f"India VIX {vix:.2f} > {config.VIX_MAX:.1f} -- premium "
                       f"too rich market-wide for a long-option entry")
    return True, f"India VIX {vix:.2f}"


def regime_ok(index_candles: pd.DataFrame | None,
              at: datetime) -> tuple[bool, str]:
    """
    NIFTY ADX(14) on 5-min bars must clear the floor. A chopping index is
    a no-trade day for an index-correlated breakout system.

    index_candles=None means the feed was unavailable. That is reported and
    the gate is SKIPPED (not failed) -- a missing index feed should not
    silently halt all trading, but it must be visible in the log.
    """
    if not config.REGIME_FILTER_ENABLED:
        return True, "regime filter disabled"
    if index_candles is None or index_candles.empty:
        return True, "regime filter SKIPPED -- no index candles supplied"

    df = _closed_slice(index_candles, at)
    if len(df) < config.ADX_PERIOD * 3:
        return True, f"regime filter SKIPPED -- only {len(df)} index bars closed"

    adx_now = float(indicators.adx_di(df, config.ADX_PERIOD)["ADX"].iloc[-1])
    if adx_now < config.REGIME_MIN_ADX:
        return False, (f"index chopping: NIFTY ADX {adx_now:.1f} < "
                       f"{config.REGIME_MIN_ADX:.0f} -- stand down")
    return True, f"NIFTY ADX {adx_now:.1f}"


# --------------------------------------------------------------------------
# 3. signal scoring and per-slot ranking
# --------------------------------------------------------------------------
@dataclass
class ScoredSignal:
    run: object                    # order_sheet.SignalRun
    score: float
    parts: str                     # human-readable breakdown for the log


def score_signal(candles: pd.DataFrame, at: datetime,
                 signal: str) -> tuple[float, str]:
    """
    Strength of one qualified signal, from closed bars only.

    Components, each normalised to roughly 0-1 then weighted:
      ADX        -- trend strength, the strongest single discriminator
      RSI push   -- distance from 50 in the signal's direction
      ATR ratio  -- how alive the stock is right now

    The absolute number means nothing; only the ordering within a slot is
    used. Weights are stated so tuning them later is honest and visible.
    """
    df = _closed_slice(candles, at)
    if len(df) < config.WARMUP_BARS:
        return 0.0, "insufficient bars"

    adx_now = float(indicators.adx_di(df, config.ADX_PERIOD)["ADX"].iloc[-1])
    rsi_now = float(indicators.rsi(df["close"]).iloc[-1])
    atr_series = indicators.atr(df, config.ATR_PERIOD)
    baseline = float(atr_series.iloc[-config.VOL_ATR_LOOKBACK:].mean())
    atr_ratio = float(atr_series.iloc[-1]) / baseline if baseline > 0 else 0.0

    directional_rsi = (rsi_now - 50.0) if signal == config.SIGNAL_BUY_CE \
        else (50.0 - rsi_now)

    adx_part = min(adx_now / 50.0, 1.0)          # 50+ ADX = saturated
    rsi_part = max(min(directional_rsi / 30.0, 1.0), 0.0)
    vol_part = min(max(atr_ratio - 1.0, 0.0) / 0.5, 1.0)

    score = 0.5 * adx_part + 0.3 * rsi_part + 0.2 * vol_part
    parts = (f"ADX {adx_now:.0f}, RSI {rsi_now:.0f}, ATRx {atr_ratio:.2f} "
             f"-> {score:.3f}")
    return score, parts


def rank_within_slots(runs: list, candles: dict[str, pd.DataFrame],
                      entry_at_fn) -> tuple[list, list[tuple]]:
    """
    Group qualified runs by entry slot, score each, keep the top
    config.RANK_PER_SLOT per slot.

    Returns (kept_runs_in_time_order, dropped) where dropped is
    [(run, reason)] for the Rejected sheet. Never compares across slots.
    """
    by_slot: dict[str, list[ScoredSignal]] = {}
    for run in runs:
        at = entry_at_fn(run)
        score, parts = score_signal(candles.get(run.symbol), at, run.signal)
        by_slot.setdefault(run.entry_slot, []).append(
            ScoredSignal(run, score, parts))

    kept, dropped = [], []
    for slot in sorted(by_slot):
        group = sorted(by_slot[slot], key=lambda s: s.score, reverse=True)
        for i, scored in enumerate(group):
            if i < config.RANK_PER_SLOT:
                kept.append(scored.run)
            else:
                best = group[0]
                dropped.append((scored.run,
                                f"outranked in {slot}: {scored.parts} vs "
                                f"{best.run.symbol} ({best.parts})"))
    kept.sort(key=lambda r: (r.entry_slot, r.symbol))
    return kept, dropped


if __name__ == "__main__":
    import numpy as np
    from datetime import date

    td = date(2026, 7, 30)
    idx = pd.date_range(f"{td - timedelta(days=3)} 09:15", periods=300,
                        freq="5min", tz=ist_clock.IST)
    rng = np.random.default_rng(9)

    def mk(vol):
        c = pd.Series(1000 + np.cumsum(rng.normal(0.2, vol, 300)), index=idx)
        return pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]),
                             "high": c + vol * 2, "low": c - vol * 2,
                             "close": c, "volume": 1000}, index=idx)

    at = idx[-1].to_pydatetime() + timedelta(minutes=10)
    quiet, lively = mk(0.5), mk(3.0)

    ok_q, note_q = volatility_ok(quiet, at)
    ok_l, note_l = volatility_ok(lively, at)
    print(f"quiet stock : {ok_q} ({note_q})")
    print(f"lively stock: {ok_l} ({note_l})")

    ok_r, note_r = regime_ok(lively, at)
    print(f"regime on trending index: {ok_r} ({note_r})")
    ok_n, note_n = regime_ok(None, at)
    print(f"regime with no feed     : {ok_n} ({note_n})")

    s1, p1 = score_signal(lively, at, config.SIGNAL_BUY_CE)
    s2, p2 = score_signal(quiet, at, config.SIGNAL_BUY_CE)
    print(f"score lively: {p1}")
    print(f"score quiet : {p2}")

    # no-lookahead check: truncating the future must not change the score
    earlier = idx[200].to_pydatetime() + timedelta(minutes=10)
    full, _ = score_signal(lively, earlier, config.SIGNAL_BUY_CE)
    trunc, _ = score_signal(lively.iloc[:220], earlier, config.SIGNAL_BUY_CE)
    assert abs(full - trunc) < 1e-9, "future data leaked into the score"
    print("no-lookahead check passed: truncating the future leaves the score unchanged")
    print("signal_quality self-check passed")

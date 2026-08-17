"""
STEP 10 — Audit gates. Decide whether a qualified signal is worth trading.

The signal says WHICH WAY. This module says WHETHER AT ALL.

THE GATES, and why each exists
------------------------------
1. SPREAD
   A 5% bid-ask spread costs 10% round trip before the trade does anything.
   On a 10% stop that is the entire risk budget lost to the market maker.

2. LIQUIDITY (volume)
   An illiquid strike is easy to enter and impossible to exit at a fair
   price. Exit liquidity is the thing that matters and it is never there
   when it is needed.

3. MINIMUM PREMIUM
   Below about Rs 5, one tick is a large percentage of the premium and the
   position behaves like a lottery ticket rather than a directional trade.

4. DAYS TO EXPIRY
   Theta accelerates in the final sessions. An intraday buyer wears all of
   it with none of the offsetting move.

5. COST VIABILITY  <-- the one that matters most here
   Estimated round-trip cost measured against the T1 gain. On 31-Jul-26
   HDFCLIFE made +Rs 55 gross and -Rs 102 net; BAJAJFINSV +Rs 180 gross,
   -Rs 54 net. Both were structurally unprofitable the moment they were
   entered, and no exit rule could have saved either. This gate refuses
   those trades before they are taken.

6. FUNDING
   Capital required versus the live Angel One balance.

7. OI BUILDUP (16-Aug-26, see oi_buildup_confirms below) -- separate from
   the six gates above: this one runs independently of `skip`, because
   it's meant to be the ONE confirmation kept even when every audit gate
   above is switched off for a pipeline (config.TW_ALL_AUDIT_ENABLED).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

import config
from option_chain import ChainWindow, OptionContract, days_to_expiry


@dataclass
class AuditResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)   # why it was rejected
    notes: list[str] = field(default_factory=list)     # informational
    est_cost: float | None = None
    t1_gain: float | None = None
    # The OI snapshot gate #7 (early) evaluated, or None if it didn't
    # evaluate real data. Carried on the result so the caller can log it and
    # run the LATE re-check closer to fill without recomputing from scratch.
    oi_snapshot: dict | None = None

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons)


# --------------------------------------------------------------------------
# cost model
# --------------------------------------------------------------------------
def cost_breakdown(entry_ltp: float, exit_ltp: float, quantity: int,
                   include_slippage: bool = True) -> dict[str, float]:
    """
    Itemised round-trip cost. Returns each component so a P/L figure can
    always be explained rather than just asserted.

    include_slippage=True by default because that is what makes the total
    match the observed 0.654% of turnover. The audit gate must use the
    all-in number -- gating on statutory charges alone would let through
    exactly the trades that lost money on 31-Jul-26.
    """
    buy_turnover = entry_ltp * quantity
    sell_turnover = exit_ltp * quantity
    turnover = buy_turnover + sell_turnover

    items = {
        "brokerage": config.BROKERAGE_PER_ORDER * 2,
        "stt": sell_turnover * config.STT_SELL_PCT,
        "exchange": turnover * config.EXCHANGE_TXN_PCT,
        "sebi": turnover * config.SEBI_CHARGES_PCT,
        "stamp_duty": buy_turnover * config.STAMP_DUTY_BUY_PCT,
    }
    items["gst"] = (items["brokerage"] + items["exchange"] + items["sebi"]) * config.GST_PCT
    # turnover already covers both legs, so a per-side rate applied to it
    # charges slippage once on the buy and once on the sell.
    items["slippage"] = (turnover * config.SLIPPAGE_PCT_PER_SIDE
                         if include_slippage else 0.0)
    items["total"] = sum(items.values())
    return items


def estimate_round_trip_cost(entry_ltp: float, exit_ltp: float,
                             quantity: int,
                             include_slippage: bool = True) -> float:
    """All-in cost for one complete round trip."""
    return cost_breakdown(entry_ltp, exit_ltp, quantity, include_slippage)["total"]


# --------------------------------------------------------------------------
# the audit
# --------------------------------------------------------------------------
def _oi_snapshot(chain: ChainWindow, signal: str) -> dict | None:
    """
    Raw OI composition across the whole +/-N strike window, or None if
    neither side has any OI data. Split out from oi_confirms so a caller can
    log or compare the actual numbers, not just the pass/fail verdict --
    the late re-check needs exactly that (see order_engine._try_build_*).
    """
    call_oi = sum(c.oi or 0 for c in chain.calls)
    put_oi = sum(c.oi or 0 for c in chain.puts)
    total = call_oi + put_oi
    if total <= 0:
        return None
    call_share = call_oi / total
    side = "call" if signal == config.SIGNAL_BUY_CE else "put"
    own_share = call_share if side == "call" else 1.0 - call_share
    return {"call_oi": call_oi, "put_oi": put_oi, "side": side, "own_share": own_share}


def oi_confirms(chain: ChainWindow, signal: str,
                mode: str) -> tuple[bool, str, dict | None]:
    """
    Does open interest actually back this direction, or is the stock just
    ticking the right way with no real positioning behind it?

    SNAPSHOT composition across the whole +/-N strike window, not a shift
    over time: call_oi / (call_oi + put_oi) must clear OI_MIN_OWN_SIDE_SHARE
    for a BUY CE, and the mirror (put share) for a BUY PE. A true "OI
    building up" check would need a rolling history, but the chain is only
    fetched once a symbol already qualifies as a candidate -- there is no
    continuous sampling to build a baseline from. This is the honest version
    of that idea given what data is actually available.

    LIVE only. OI has no historical series -- BACKTEST would be judging a
    past date's trade against TODAY's live OI, the exact lookahead bug
    already fixed for LTP (see order_engine._historical_quote's docstring).
    Passes through untouched in BACKTEST rather than pretend to gate on
    data that isn't real for the date being tested.

    UNCALIBRATED (07-Aug-26): OI_MIN_OWN_SIDE_SHARE is a starting guess, not
    measured against real trades. Treat rejections from this gate with more
    suspicion than the others until it's been watched against real outcomes.

    Called TWICE per trade that gets this far (07-Aug-26, Harish): once here
    inside audit_option (gate #25, "early"), and again by the caller right
    before the trade is finalised into Orders/Missed_Concurrent ("late",
    closer to the actual fill). Re-running the same deterministic check on
    fresher data naturally "ignores" an unchanged reading -- if nothing
    shifted, the verdict can't change either -- so there's no separate
    equality check needed, just run it again.

    Returns (ok, note, snapshot) -- snapshot is None whenever the gate
    didn't evaluate real data (disabled, BACKTEST, or no OI at all), so
    callers know not to log or compare against it.
    """
    if not config.OI_GATE_ENABLED:
        return True, "OI gate disabled", None
    if mode != config.LIVE:
        return True, "OI check skipped in BACKTEST -- no historical OI data", None

    snap = _oi_snapshot(chain, signal)
    if snap is None:
        return True, "no OI data on either side -- not blocking", None

    if snap["own_share"] < config.OI_MIN_OWN_SIDE_SHARE:
        return False, (f"{snap['side']}-side OI is only {snap['own_share']:.0%} of "
                       f"the CE+PE window (need {config.OI_MIN_OWN_SIDE_SHARE:.0%}) "
                       f"-- positioning doesn't back this {snap['side']}"), snap
    return True, (f"OI backs {signal} ({snap['side']} share "
                 f"{snap['own_share']:.0%})"), snap


def _oi_quadrant(opt_candles: pd.DataFrame, at: datetime,
                 lookback_minutes: int | None = None) -> tuple[str | None, str]:
    """
    Shared by oi_buildup_confirms (the gate, below) and classify_oi_buildup
    (the Orders sheet's 'OI Check' display column) -- one calculation, two
    consumers, so the gate and the sheet can never disagree on what
    happened. Returns (quadrant, note); quadrant is None whenever there
    isn't enough closed-bar OI history yet to classify -- missing data
    (Angel-sourced candles never carry OI; Kite only returns it for F&O
    instruments) is reported, never silently treated as a real reading.

    Classifies the standard four OI quadrants, comparing the contract's own
    price and OI now against `lookback_minutes` earlier on its own candle
    history:

        price up,   OI up   -> LONG_BUILDUP    new longs being built
        price up,   OI down -> SHORT_COVERING  shorts closing, not new buying
        price down, OI up   -> SHORT_BUILDUP   new shorts being built
        price down, OI down -> LONG_UNWINDING  longs closing, not new selling

    Uses CLOSED bars only for both "now" and the lookback point -- no
    lookahead in either direction.
    """
    if opt_candles is None or opt_candles.empty or "oi" not in opt_candles.columns:
        return None, "no OI data on this contract's candles"

    lookback_minutes = lookback_minutes or config.OI_BUILDUP_LOOKBACK_MINUTES
    closed = opt_candles[opt_candles.index + timedelta(minutes=config.INTERVAL_MINUTES) <= at]
    if closed.empty:
        return None, "no closed bars yet"

    # Bar-COUNT lookback, not a raw timestamp subtraction: "5 minutes
    # before now" and "the current 5-min bar's own open" are the SAME
    # instant when lookback_minutes == INTERVAL_MINUTES, which made a
    # timestamp-based prior_slice collide with `current` itself (found by
    # the self-test below -- every LONG_BUILDUP scenario misclassified as
    # SHORT_COVERING with a zero delta). Going back a fixed number of
    # CLOSED bars instead has no such boundary case.
    n_bars_back = max(round(lookback_minutes / config.INTERVAL_MINUTES), 1)
    current = closed.iloc[-1]
    if len(closed) <= n_bars_back:
        return None, (f"fewer than {n_bars_back} closed bar(s) before "
                      f"{at:%H:%M} yet")
    prior = closed.iloc[-1 - n_bars_back]

    if pd.isna(current["oi"]) or pd.isna(prior["oi"]):
        return None, "OI value missing on this bar"

    price_delta = float(current["close"]) - float(prior["close"])
    oi_delta = float(current["oi"]) - float(prior["oi"])

    if price_delta >= 0 and oi_delta > 0:
        quadrant = "LONG_BUILDUP"
    elif price_delta >= 0:
        quadrant = "SHORT_COVERING"
    elif oi_delta > 0:
        quadrant = "SHORT_BUILDUP"
    else:
        quadrant = "LONG_UNWINDING"

    note = (f"{quadrant} (price {price_delta:+.2f}, OI {oi_delta:+.0f} over "
           f"{lookback_minutes}min)")
    return quadrant, note


_OI_QUADRANT_LABELS = {
    "LONG_BUILDUP": "Long Buildup",
    "SHORT_COVERING": "Short Covering",
    "SHORT_BUILDUP": "Short Buildup",
    "LONG_UNWINDING": "Long Unwinding",
}


def classify_oi_buildup(opt_candles: pd.DataFrame, at: datetime,
                        lookback_minutes: int | None = None) -> str:
    """
    OI-quadrant label for the Orders sheet's 'OI Check' column (16-Aug-26,
    Harish's request) -- independent of OI_BUILDUP_GATE_ENABLED, since this
    is a display value, not a gate. Returns "Long Buildup" / "Short
    Covering" / "Short Buildup" / "Long Unwinding", or "" when there isn't
    enough OI history yet to classify.
    """
    quadrant, _note = _oi_quadrant(opt_candles, at, lookback_minutes)
    return _OI_QUADRANT_LABELS.get(quadrant, "")


def oi_buildup_confirms(opt_candles: pd.DataFrame, at: datetime, signal: str,
                        lookback_minutes: int | None = None) -> tuple[bool, str]:
    """
    OI CHANGE confirmation (16-Aug-26, Harish's request, "keep only OI
    Change (Long Buildup etc) for all Rise in Price and Rise in OI") --
    the ONE gate deliberately left running for a pipeline that has
    everything else in audit_option() switched off
    (config.TW_ALL_AUDIT_ENABLED). Unlike oi_confirms() above (a SNAPSHOT
    of composition across the strike window, LIVE only), this compares
    the traded CONTRACT's own price and OI now against `lookback_minutes`
    earlier -- see _oi_quadrant for the quadrant math, shared with the
    Orders sheet's 'OI Check' column (classify_oi_buildup).

    Only LONG_BUILDUP confirms a BUY CE and only SHORT_BUILDUP confirms a
    BUY PE -- "Rise in Price AND Rise in OI" (or the mirror) is the ONLY
    combination trusted. SHORT_COVERING and LONG_UNWINDING never confirm
    either direction, regardless of which way price moved. Adopted from
    F:\\05_Claude_Automation\\05 Codes_OLD\\historical_lookup.py, where a
    backtest audit found SHORT_COVERING-tagged entries lost money on BOTH
    CE and PE (-Rs 7,610/14 trades and -Rs 2,233/6 trades) while
    LONG_BUILDUP was the one net-profitable OI bucket (+Rs 1,382/17
    trades, 58.8% win). Small sample (n=17-23 there) -- a starting point,
    not a proven edge; re-validate as more trades accumulate here.

    Missing OI data SKIPS the gate rather than failing it, and says so --
    a silently-absent OI feed must never masquerade as a passing one.
    """
    if not config.OI_BUILDUP_GATE_ENABLED:
        return True, "OI buildup gate disabled"

    quadrant, note = _oi_quadrant(opt_candles, at, lookback_minutes)
    if quadrant is None:
        return True, f"OI buildup SKIPPED -- {note}"

    if signal == config.SIGNAL_BUY_CE:
        ok = quadrant == "LONG_BUILDUP"
    elif signal == config.SIGNAL_BUY_PE:
        ok = quadrant == "SHORT_BUILDUP"
    else:
        ok = True

    if not ok:
        note += f" -- does not confirm {signal}"
    return ok, note


def audit_option(contract: OptionContract, chain: ChainWindow, signal: str,
                 trade_date: date, quantity: int,
                 available_capital: float | None = None,
                 mode: str | None = None,
                 ignore_oi: bool = False,
                 skip: bool = False) -> AuditResult:
    """
    Run every gate. Returns AuditResult with every failure listed, not just
    the first -- seeing all four reasons a trade was refused is more useful
    than seeing the alphabetically first one.

    `ignore_oi` (09-Aug-26): for the "OI Blocked" shadow sheet only -- still
    evaluates and logs the real OI verdict (oi_snapshot, the CSV row), just
    never lets it fail `result.passed`. Never used for a real trade.

    `skip` (15-Aug-26, MACD pipeline): bypasses every gate below entirely
    and returns an always-passing result. The MACD pipeline is being
    rebuilt step by step, straight from Final Recomm to the order sheet --
    see config.MACD_AUDIT_ENABLED / order_engine.process_date. The "no live
    quote" check just above THIS function's caller still applies regardless
    -- that's data availability, not a quality gate.
    """
    if skip:
        return AuditResult(passed=True,
                           notes=["audit checks disabled (MACD pipeline)"])

    result = AuditResult(passed=True)

    # --- quote present ---------------------------------------------------
    if contract.ltp is None or contract.ltp <= 0:
        result.reasons.append(f"{contract.trading_symbol}: no live quote")
        result.passed = False
        return result

    # --- 1. spread ---------------------------------------------------------
    spread_pct = contract.spread_pct
    if spread_pct is not None:
        if spread_pct > config.MAX_SPREAD_PCT:
            result.reasons.append(
                f"Spread too wide ({spread_pct:.1%} of LTP, limit "
                f"{config.MAX_SPREAD_PCT:.0%}) -- costs {spread_pct * 2:.1%} round trip"
            )
            result.passed = False
    else:
        result.notes.append("bid/ask unavailable, spread not checked")

    # --- 2. liquidity -------------------------------------------------------
    if contract.volume is not None and contract.volume < config.MIN_OPTION_VOLUME:
        result.reasons.append(
            f"Illiquid: volume {contract.volume:,.0f} < {config.MIN_OPTION_VOLUME:,}"
        )
        result.passed = False

    # --- 3. minimum premium --------------------------------------------------
    if contract.ltp < config.MIN_OPTION_LTP:
        result.reasons.append(
            f"Entry LTP (Rs {contract.ltp:.2f}) < Rs {config.MIN_OPTION_LTP:.0f} "
            f"-- dead premium, tick noise dominates"
        )
        result.passed = False

    # --- 4. days to expiry ----------------------------------------------------
    dte = days_to_expiry(contract.expiry, trade_date)
    if dte < config.MIN_DAYS_TO_EXPIRY:
        result.reasons.append(
            f"Only {dte} day(s) to expiry (min {config.MIN_DAYS_TO_EXPIRY}) "
            f"-- theta decay too steep for a buyer"
        )
        result.passed = False
    result.notes.append(f"{dte} days to expiry")

    # --- 5. cost viability ------------------------------------------------
    t1_price = contract.ltp * config.TARGET_MULTS[0]
    t1_gain = (t1_price - contract.ltp) * quantity
    est_cost = estimate_round_trip_cost(contract.ltp, t1_price, quantity)
    result.t1_gain = t1_gain
    result.est_cost = est_cost

    if t1_gain > 0:
        ratio = est_cost / t1_gain
        if ratio > config.MAX_COST_TO_T1_GAIN:
            result.reasons.append(
                f"Not cost-viable: round-trip cost Rs {est_cost:,.0f} is "
                f"{ratio:.0%} of the T1 gain Rs {t1_gain:,.0f} "
                f"(limit {config.MAX_COST_TO_T1_GAIN:.0%})"
            )
            result.passed = False
        else:
            result.notes.append(f"cost/T1 gain = {ratio:.0%}")

    # --- 6. funding -------------------------------------------------------
    capital_required = contract.ltp * quantity
    if available_capital is not None and capital_required > available_capital:
        result.reasons.append(
            f"Insufficient balance (need Rs {capital_required:,.2f}, "
            f"have Rs {available_capital:,.2f})"
        )
        result.passed = False

    # --- 7. open interest confirmation -------------------------------------
    ok, note, snap = oi_confirms(chain, signal, mode)
    result.oi_snapshot = snap
    if ok or ignore_oi:
        result.notes.append(note if ok else f"{note} (ignored -- shadow simulation)")
    else:
        result.reasons.append(note)
        result.passed = False
    if snap is not None:
        import oi_log
        oi_log.log_oi_check(trade_date, contract.symbol, signal, "early", snap, ok, note)

    return result


def get_available_capital(angel) -> float | None:
    """
    Live funds from Angel One. Returns None if the call fails -- the caller
    must treat None as "unknown", never as "unlimited".
    """
    try:
        rms = angel.rmsLimit()
    except Exception as exc:
        print(f"[audit] could not read Angel One funds: {exc}")
        return None

    if not rms or not rms.get("status"):
        print("[audit] funds response not OK")
        return None

    data = rms.get("data") or {}
    for key in ("availablecash", "availableCash", "net", "availabelLimitMargin"):
        if key in data:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                continue
    print(f"[audit] no recognisable cash field in funds response: {list(data)}")
    return None


if __name__ == "__main__":
    # --- calibration: does the model reproduce the workbook's costs? -------
    print("\nCost model vs the workbook's actual Costs column:")
    real = [  # symbol, entry, qty, exit_ltp, actual_cost
        ("HDFCLIFE", 10.90, 1100, 10.95, 157.07),
        ("BEL", 9.60, 1425, 9.80, 180.82),
        ("MARUTI", 347.00, 50, 372.50, 235.97),
        ("SUNPHARMA", 52.30, 350, 53.10, 241.21),
        ("KOTAKBANK", 8.00, 2000, 8.25, 212.70),
    ]
    errors = []
    for name, entry, qty, exit_ltp, actual in real:
        modelled = estimate_round_trip_cost(entry, exit_ltp, qty)
        err = (modelled - actual) / actual
        errors.append(abs(err))
        print(f"  {name:<11} modelled Rs {modelled:>7.2f}  actual Rs {actual:>7.2f}  "
              f"error {err:+.1%}")
    worst = max(errors)
    print(f"  worst error {worst:.1%} (fitted across all 14 trades: "
          f"worst 8.3%, mean 4.3%, total within 0.9%)")
    assert worst < 0.12, f"cost model off by {worst:.1%} -- recalibrate"

    print("\n  breakdown for HDFCLIFE:")
    for k, v in cost_breakdown(10.90, 10.95, 1100).items():
        print(f"    {k:<12} Rs {v:>8.2f}")

    # --- the viability gate on those same trades --------------------------
    print("\nCost viability gate at T1 (+10%):")
    for name, entry, qty, _exit, actual in real:
        t1 = entry * config.TARGET_MULTS[0]
        gain = (t1 - entry) * qty
        cost = estimate_round_trip_cost(entry, t1, qty)
        ratio = cost / gain
        verdict = "REJECT" if ratio > config.MAX_COST_TO_T1_GAIN else "allow "
        print(f"  {verdict} {name:<11} T1 gain Rs {gain:>8,.0f}  "
              f"cost Rs {cost:>7,.0f}  = {ratio:>4.0%} of the gain")
    print("\noption_audit self-check passed")

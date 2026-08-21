"""
STEP 11 — Signal qualification, order placement, and position management.

QUALIFICATION (Harish, 01-Aug-26)
---------------------------------
A symbol qualifies when the Final Recomm row shows the SAME signal in three
consecutive 5-minute columns. Mixed values or WAIT do not qualify.

    09:20  09:25  09:30  09:35  09:40
    BUY CE BUY CE BUY CE BUY CE WAIT
    |----- run of 4 ------|
    ^ ONE order, at 09:30 (the third bar completes the trio)

ONE ORDER PER RUN. While the signal keeps repeating the position is held, not
re-entered. A new entry needs the run to break -- a WAIT or an opposite
signal -- and a fresh trio to form. Without this rule a signal persisting for
twelve bars would fire ten entries and pay ten sets of costs.

THE ENTRY BAR IS ALREADY CLOSED
-------------------------------
The trio completing at the 09:30 column means the 09:30 candle has CLOSED,
which happens at 09:35. So the entry is decided at 09:35 and filled at the
09:35 quote. Nothing here ever acts on a forming candle. The Orders sheet
records both the trigger times and the actual fill.

EXIT LADDER
-----------
    SL          entry x 0.90                      full exit
    T1          entry x 1.10   sell 50%   -> SL moves to breakeven
    T2          entry x 1.20   sell 25%
    T3          entry x 1.35   sell 25%   -> position closed
    TSL         10% below peak LTP, active after T1
    no-follow   flat (<0.5R) for 20 min           full exit
    max hold    75 min                            full exit
    EOD         square-off time                   full exit

Banking half at T1 is the direct answer to the 31-Jul-26 pattern: nine of
fourteen trades exited as "No Follow-Through" having gone nowhere, each
paying ~Rs 167 round trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

import config
import ist_clock
import option_audit
from option_chain import ChainWindow, OptionContract

# Absorbs float representation error only. A fraction of a paise, far below
# any real tick, so an exact touch counts as a hit in both directions.
PRICE_EPS = 1e-6


def compute_stop_price(entry_ltp: float, underlying_atr: float = 0.0) -> float:
    """
    Stop price for a long-premium option position.

    Adapted from F:\\06_Claude_v2's position_manager.compute_stop_and_target.
    The underlying's ATR is converted into premium terms through ATM delta
    (an ATM option moves roughly half of what the stock does), then clamped
    to a sane percentage band.

    Falls back to the flat STOP_LOSS_MULT when no ATR is available -- and
    that fallback matters, because the old model's `ATR (Underlying)` column
    read 0 on every single row of its own Orders sheet. Its ATR stop was
    never actually running; every trade silently used the MIN_STOP_PCT
    floor. Worth knowing before assuming ATR sizing is battle-tested.
    """
    if entry_ltp <= 0:
        return 0.0
    if not config.USE_ATR_STOP or underlying_atr <= 0:
        return entry_ltp * config.STOP_LOSS_MULT

    raw = config.ATR_STOP_MULTIPLIER * underlying_atr * config.ATM_DELTA_APPROX
    lo = config.MIN_STOP_PCT * entry_ltp
    hi = config.MAX_STOP_PCT * entry_ltp
    return entry_ltp - min(max(raw, lo), hi)


# --------------------------------------------------------------------------
# signal qualification
# --------------------------------------------------------------------------
@dataclass
class SignalRun:
    """An unbroken run of identical BUY CE / BUY PE values."""
    symbol: str
    signal: str
    slots: list[str]                 # the time columns in the run

    @property
    def trigger_slots(self) -> list[str]:
        """The first N slots -- the trio that qualified it."""
        return self.slots[:config.CONSECUTIVE_SIGNALS_REQUIRED]

    @property
    def qualifies(self) -> bool:
        return len(self.slots) >= config.CONSECUTIVE_SIGNALS_REQUIRED

    @property
    def entry_slot(self) -> str:
        """Slot at which the trio completes. The order is decided here."""
        return self.slots[config.CONSECUTIVE_SIGNALS_REQUIRED - 1]


def find_signal_runs(final_row: pd.Series, symbol: str,
                     slots: list[str]) -> list[SignalRun]:
    """
    Split one symbol's Final Recomm row into maximal runs of identical
    actionable signals.

    WAIT and blank both break a run. So does an opposite signal. Only runs
    of at least CONSECUTIVE_SIGNALS_REQUIRED length qualify, and each run
    yields at most ONE order.
    """
    runs: list[SignalRun] = []
    current_signal: str | None = None
    current_slots: list[str] = []

    actionable = (config.SIGNAL_BUY_CE, config.SIGNAL_BUY_PE)

    for slot in slots:
        value = final_row.get(slot, "")
        value = "" if pd.isna(value) else str(value).strip()

        if value in actionable and value == current_signal:
            current_slots.append(slot)
            continue

        if current_signal is not None and len(current_slots) >= config.CONSECUTIVE_SIGNALS_REQUIRED:
            runs.append(SignalRun(symbol, current_signal, list(current_slots)))

        if value in actionable:
            current_signal, current_slots = value, [slot]
        else:
            current_signal, current_slots = None, []

    if current_signal is not None and len(current_slots) >= config.CONSECUTIVE_SIGNALS_REQUIRED:
        runs.append(SignalRun(symbol, current_signal, list(current_slots)))

    return runs


def qualified_signals(final_df: pd.DataFrame,
                      slots: list[str]) -> list[SignalRun]:
    """Every qualifying run across every symbol in the Final sheet."""
    rows = final_df[final_df["Metrics"] == "Final Recomm"]
    out: list[SignalRun] = []
    for _, row in rows.iterrows():
        out.extend(find_signal_runs(row, str(row["Symbol"]), slots))
    out.sort(key=lambda r: (r.entry_slot, r.symbol))
    return out


# --------------------------------------------------------------------------
# position
# --------------------------------------------------------------------------
@dataclass
class Position:
    symbol: str
    signal: str
    contract: OptionContract
    entry_time: datetime
    entry_ltp: float
    quantity: int                    # units, not lots
    lots: int
    lot_size: int

    stop_loss: float = 0.0
    targets: tuple = ()
    target_hit: list[bool] = field(default_factory=lambda: [False, False, False])
    remaining_qty: int = 0
    peak_ltp: float = 0.0
    trough_ltp: float = 0.0
    breakeven_active: bool = False
    tsl_breach_streak: int = 0

    realised_pnl: float = 0.0
    exits: list[dict] = field(default_factory=list)
    closed: bool = False
    exit_reason: str = ""
    exit_time: datetime | None = None

    trigger_slots: list[str] = field(default_factory=list)
    audit: Any = None
    order_id: str = ""
    # "Confluence" for a normal 3-bar entry, "RSI Checkpoint (3rd Candle)"
    # for the RSI-checkpoint re-entry (16-Aug-26) -- see order_engine.
    # _third_candle_rising.
    entry_type: str = "Confluence"
    # OI-quadrant label for this fill -- "Long Buildup" / "Short Covering" /
    # "Short Buildup" / "Long Unwinding", or "" when there wasn't enough OI
    # history to classify. Display only; option_audit.classify_oi_buildup.
    oi_check: str = ""
    # "LIVE" once config.LIVE_TRADING is True and live_orders.py actually
    # placed a real order for this position; every position before 20-Aug-26
    # was "PAPER" (simulated fill, no order placed) because no order-
    # placement module existed. Dashboard splits on this so a real fill and
    # a simulated one are never blended into one headline number.
    trade_mode: str = "PAPER"

    # Real broker order tracking (20-Aug-26, live_orders.py). Blank for
    # every PAPER position -- only populated when trade_mode == "LIVE".
    broker_entry_order_id: str = ""
    broker_stop_order_id: str = ""      # the CURRENT resting SL-M order id
    broker_stop_trigger: float = 0.0    # trigger price that order is resting at
    broker_exit_order_id: str = ""
    actual_fill_price: float = 0.0      # what the broker really filled at,
                                        # vs entry_ltp which is the intended price
    actual_exit_price: float = 0.0
    # Timestamp of the last option candle already acted on for this LIVE
    # position -- unlike the PAPER path, a real position is never replayed
    # from scratch each cycle (broker orders aren't replayable), so this is
    # what keeps a repeat cycle from re-evaluating, and re-ordering against,
    # a bar it already handled. Always None for PAPER positions.
    last_processed_bar: datetime | None = None

    # The moment the third bar of the trio CLOSED -- i.e. the earliest instant
    # this trade could honestly have been known about. Recorded so the gap
    # between "signal confirmed" and "filled" is visible in the sheet. If a
    # fill ever timestamps BEFORE this, the backtest is buying on hindsight.
    signal_confirmed_at: datetime | None = None
    spot_price: float = 0.0        # underlying's price at entry decision
    underlying_atr: float = 0.0    # ATR of the UNDERLYING, sizes the stop

    @property
    def entry_lag_mins(self) -> float | None:
        if self.signal_confirmed_at is None:
            return None
        return (self.entry_time - self.signal_confirmed_at).total_seconds() / 60.0

    def __post_init__(self):
        # Round to paise. NSE quotes options to two decimals, so a level of
        # 110.00000000000001 is a float artefact, not a price. Left unrounded,
        # an LTP of exactly 110.00 fails `ltp >= target` and the target is
        # silently missed -- the position runs on as though it never got there.
        self.stop_loss = round(
            compute_stop_price(self.entry_ltp, self.underlying_atr), 2)
        # Targets stay proportional to the ACTUAL stop distance, so the
        # payoff ratio is constant whatever the stop width turns out to be.
        # Pinning targets to entry while the stop floats would let a wide
        # ATR quietly turn a 1:1 trade into 2:1 against.
        risk = self.entry_ltp - self.stop_loss
        base = [(m - 1.0) for m in config.TARGET_MULTS]      # 0.07, 0.12, 0.20
        ref_risk = 1.0 - config.STOP_LOSS_MULT               # 0.07
        self.targets = tuple(
            round(self.entry_ltp + risk * (b / ref_risk), 2) for b in base)
        self.remaining_qty = self.quantity
        self.peak_ltp = self.entry_ltp
        self.trough_ltp = self.entry_ltp

    @property
    def risk_per_unit(self) -> float:
        """
        Actual risk per unit, from the REAL stop (self.stop_loss, ATR-based
        when USE_ATR_STOP is on) -- not the flat STOP_LOSS_MULT assumption
        this used to return regardless of what stop was actually set.

        BUG FOUND 19-Aug-26 (Harish, reviewing a NESTLEIND loss against his
        stop): this fed the Orders sheet's 'Risk/Unit (Rs)' / 'Risk Amount
        (Rs)' columns and silently understated them whenever the ATR stop
        was wider than flat 7% -- NESTLEIND showed Risk Amount Rs 976.50
        when the real stop (8.18 vs entry 9.30) put it at ~Rs 1,680. Sizing
        itself (order_sheet.size_position) was never affected -- it always
        computed off compute_stop_price() correctly. This was a reporting
        bug only, but a materially misleading one.
        """
        return max(self.entry_ltp - self.stop_loss, 0.0)

    @property
    def r_multiple(self) -> float:
        """Current unrealised move in units of initial risk."""
        if self.risk_per_unit <= 0:
            return 0.0
        return (self.peak_ltp - self.entry_ltp) / self.risk_per_unit

    @property
    def trailing_stop(self) -> float:
        return round(self.peak_ltp * config.TRAILING_STOP_MULT, 2)

    @property
    def effective_stop(self) -> float:
        """
        The stop actually in force.

        Before T1: the fixed -10% stop.
        After T1: the higher of breakeven and the trailing stop. It only ever
        ratchets up, never down -- a stop that can loosen is not a stop.
        """
        stop = self.stop_loss
        if self.breakeven_active:
            stop = max(stop, self.entry_ltp)
            stop = max(stop, self.trailing_stop)
        return stop


# --------------------------------------------------------------------------
# exit engine
# --------------------------------------------------------------------------
def note_bar_range(pos: Position, bar_high: float, bar_low: float) -> None:
    """
    Register a whole bar's true high/low against the running peak/trough
    BEFORE any per-tick exit check runs for that bar.

    THE BUG THIS FIXES (found 02-Aug-26 on TATASTEEL, 29-Jul-26): the caller
    walks each closed bar worst-first -- low, then high, then close -- so
    that an ambiguous bar spanning both stop and target resolves as the
    stop. That's the right call for STOP vs TARGET. But No-Follow-Through
    and Max-Hold read `pos.r_multiple`, which is derived from `pos.peak_ltp`
    -- and peak_ltp previously only grew as each individual tick was passed
    in. On the FIRST tick of a new bar (the low), peak_ltp still reflects
    only the PRIOR bar's high. A trade sitting on a stale sub-threshold peak
    can get killed by the time rule using that low print, one call before
    its own bar's high would have cleared the R-multiple floor entirely --
    or hit a target outright.

    Measured: TATASTEEL entered 5.95 (CE), stop 5.65, Target 1 6.25. The
    09:45-09:50 bar ranged 6.06-6.67. Peak carried in from the prior bar was
    only 6.10 (0.36R, below the 0.5R follow-through floor) -- so at the
    20-minute mark the low tick (6.06) triggered "No Follow-Through" and
    closed the trade for Rs 82 net, one tick before the SAME bar's high
    (6.67) would have cleared Target 1 (6.25) outright.

    Calling this once per bar, before the lo/hi/cl walk, means peak_ltp
    already reflects the bar's true high when the time-based checks run --
    so a bar that actually got the trade moving no longer gets read as if
    it hadn't. Stop-vs-target ordering inside update_position() is
    untouched; only the time-based checks see a truer picture.
    """
    if pos.closed:
        return
    pos.peak_ltp = max(pos.peak_ltp, bar_high)
    pos.trough_ltp = min(pos.trough_ltp, bar_low)


def update_position(pos: Position, ltp: float, now: datetime,
                    square_off_time: datetime,
                    bar_open: float | None = None) -> list[dict]:
    """
    Advance one position by one tick of the clock. Returns any exits fired.

    Order of checks matters. Stop before target: if a bar could plausibly
    have hit both, assume the worse outcome. Being optimistic about which
    came first is how a backtest flatters itself.

    bar_open lets the stop fill correctly. Pass the candle's open when
    walking bars: a bar that opened below the stop gapped through it and
    fills at the open; otherwise the stop fills at the stop.
    """
    if pos.closed or ltp is None or ltp <= 0:
        return []

    pos.peak_ltp = max(pos.peak_ltp, ltp)
    pos.trough_ltp = min(pos.trough_ltp, ltp)
    fired: list[dict] = []

    # --- hard exits, full position ---------------------------------------
    if now >= square_off_time:
        return [_close(pos, ltp, now, "EOD Square-off")]

    # No-Follow-Through and Max-Hold are DISABLED by default (02-Aug-26).
    # CIPLA, 29-Jul-26 is the concrete case: No-Follow-Through correctly
    # measured 0.38R at the 20-minute mark and cut the trade at -Rs 236.93 --
    # the rule worked exactly as designed. The same contract went on to close
    # 47.50 by 11:00, past Target 3. The timeout wasn't broken, it was just
    # the wrong call often enough that only SL, TSL and Targets decide exits
    # now, plus the hard EOD square-off below (that one stays -- an intraday
    # paper position cannot ride overnight).
    held = (now - pos.entry_time).total_seconds() / 60.0
    if config.ENABLE_MAX_HOLD_EXIT and held >= config.MAX_HOLD_MINS:
        return [_close(pos, ltp, now, f"Max Hold Time ({config.MAX_HOLD_MINS}min)")]

    if (config.ENABLE_NO_FOLLOW_THROUGH_EXIT
            and held >= config.NO_FOLLOW_THROUGH_MINS
            and not any(pos.target_hit)
            and pos.r_multiple < config.NO_FOLLOW_THROUGH_R):
        return [_close(pos, ltp, now,
                       f"No Follow-Through ({config.NO_FOLLOW_THROUGH_MINS}min, "
                       f"<{config.NO_FOLLOW_THROUGH_R}R)")]

    # --- stop / trailing stop --------------------------------------------
    # PRICE_EPS absorbs float representation error only -- it is a fraction of
    # a paise, far below any real tick. Comparisons must treat an exact touch
    # as a hit, in both directions.
    stop = pos.effective_stop
    if ltp <= stop + PRICE_EPS:
        pos.tsl_breach_streak += 1
        if pos.tsl_breach_streak >= config.TSL_BREACH_CONFIRM_BARS:
            if pos.breakeven_active and stop > pos.stop_loss:
                reason = "Trailing Stop Hit"
            elif abs(stop - pos.entry_ltp) < 1e-9:
                reason = "Breakeven Stop Hit"
            else:
                reason = "Stop Loss Hit"

            # FILL AT THE STOP, NOT AT THE EXTREME.
            #
            # A resting stop order triggers at `stop` and fills at or near it.
            # Filling at the candle's low instead assumes the worst tick of the
            # bar, which is not what a stop does -- it is what no order does.
            #
            # Measured cost of getting this wrong: on 28-Jul-26 HINDUNILVR
            # stopped at 49.37 but the bar's low was 15.95, and the engine
            # booked the 15.95. One trade, Rs 94,207. Across the 4-session
            # backtest, 7 stop-outs lost Rs 111,806 -- 80% of the total loss --
            # against Rs 27,894 had they filled at the stop.
            #
            # `bar_open` is passed when the caller knows it. A bar that OPENED
            # below the stop is a genuine gap: there was no chance to fill at
            # the stop, so the open is the honest price. That is real gap risk
            # and is kept.
            fill = stop
            if bar_open is not None and bar_open < stop:
                fill = bar_open          # gapped through -- no fill at the stop
            fill = max(fill, ltp)        # never better than the bar actually traded
            return [_close(pos, fill, now, reason)]
    else:
        pos.tsl_breach_streak = 0

    # --- targets, partial exits ------------------------------------------
    for i, target in enumerate(pos.targets):
        if pos.target_hit[i] or ltp < target - PRICE_EPS:
            continue
        pos.target_hit[i] = True

        if i == len(pos.targets) - 1:
            fired.append(_close(pos, ltp, now, f"Target {i + 1} Hit"))
            return fired

        qty = int(pos.quantity * config.TARGET_EXIT_FRACTIONS[i])
        qty = min(qty, pos.remaining_qty)
        if qty > 0:
            fired.append(_partial(pos, ltp, now, qty, f"Target {i + 1} Hit"))

        if i == 0 and config.MOVE_SL_TO_BREAKEVEN_AT_T1:
            pos.breakeven_active = True

    return fired


def close_for_signal_invalidation(pos: Position, ltp: float, now: datetime,
                                  reason: str) -> dict | None:
    """
    Force-close a position outside the normal SL/Target/TSL ladder, for a
    reason the ladder itself has no concept of -- the entry signal broke
    (20-Aug-26, see order_engine._macd_invalidated). Public wrapper around
    _close() so order_engine doesn't reach into a private function.
    """
    if pos.closed or ltp is None or ltp <= 0:
        return None
    return _close(pos, ltp, now, reason)


def _partial(pos: Position, ltp: float, now: datetime, qty: int,
             reason: str) -> dict:
    gross = (ltp - pos.entry_ltp) * qty
    cost = option_audit.estimate_round_trip_cost(pos.entry_ltp, ltp, qty)
    net = gross - cost
    pos.remaining_qty -= qty
    pos.realised_pnl += net
    record = {"time": now, "qty": qty, "ltp": ltp, "reason": reason,
              "gross": gross, "cost": cost, "net": net, "partial": True}
    pos.exits.append(record)
    if pos.remaining_qty <= 0:
        pos.closed = True
        pos.exit_reason = reason
        pos.exit_time = now
    return record


def _close(pos: Position, ltp: float, now: datetime, reason: str) -> dict:
    qty = pos.remaining_qty
    gross = (ltp - pos.entry_ltp) * qty
    cost = option_audit.estimate_round_trip_cost(pos.entry_ltp, ltp, qty) if qty else 0.0
    net = gross - cost
    pos.remaining_qty = 0
    pos.realised_pnl += net
    pos.closed = True
    pos.exit_reason = reason
    pos.exit_time = now
    record = {"time": now, "qty": qty, "ltp": ltp, "reason": reason,
              "gross": gross, "cost": cost, "net": net, "partial": False}
    pos.exits.append(record)
    return record


# --------------------------------------------------------------------------
# sizing
# --------------------------------------------------------------------------
def size_position(entry_ltp: float, lot_size: int,
                  risk_budget: float | None = None,
                  available_capital: float | None = None,
                  underlying_atr: float = 0.0) -> tuple[int, str]:
    """
    Lots to trade, and a note explaining the number.

    Risk per lot = (entry - stop) * lot_size, PLUS the round-trip cost of
    getting stopped out. Floor-divided, never rounded up.

    COST-AWARE CAP (adopted from F:\\06_Claude_v2, 02-Aug-26). Their audit
    found the flaw in their own code and we had it identically: solving for
    the exit price where GROSS loss equals the budget, then subtracting
    costs afterwards, means every stopped-out trade breaches the stated cap.
    They measured 9-12% overshoot on all three occurrences in their sample
    (-Rs 2,195 / 2,234 / 2,175 against a Rs 2,000 ceiling).

    A "Rs 2,000 max loss" that reliably loses Rs 2,200 is not a risk cap,
    it is a rounding error with good intentions. Costs are now inside the
    budget, so the NET loss at the stop respects it.

    Returns (0, reason) when the trade cannot be taken -- a valid answer,
    not an error to work around.
    """
    risk_budget = config.RISK_PER_TRADE_RS if risk_budget is None else risk_budget
    if entry_ltp <= 0 or lot_size <= 0:
        return 0, "invalid price or lot size"

    stop_price = compute_stop_price(entry_ltp, underlying_atr)
    risk_per_unit = entry_ltp - stop_price
    if risk_per_unit <= 0:
        return 0, "non-positive stop distance"

    risk_per_lot = risk_per_unit * lot_size
    if config.COST_AWARE_RISK_CAP:
        import option_audit
        cost_at_stop = option_audit.estimate_round_trip_cost(
            entry_ltp, stop_price, lot_size)
        risk_per_lot += cost_at_stop
    if risk_per_lot <= 0:
        return 0, "zero risk per lot"

    lots = int(risk_budget // risk_per_lot)
    if lots < 1:
        return 0, (f"even 1 lot's all-in risk (Rs {risk_per_lot:,.0f}, "
                   f"stop + costs) exceeds this trade's risk budget "
                   f"(Rs {risk_budget:,.0f})")

    if available_capital is not None:
        cost_per_lot = entry_ltp * lot_size
        affordable = int(available_capital // cost_per_lot)
        if affordable < 1:
            return 0, (f"Insufficient balance (need Rs {cost_per_lot:,.2f}, "
                       f"have Rs {available_capital:,.2f})")
        if affordable < lots:
            lots = affordable
            return lots, f"capped to {lots} lot(s) by available capital"

    return lots, f"{lots} lot(s) within Rs {risk_budget:,.0f} risk budget"


# --------------------------------------------------------------------------
# sheet builders
# --------------------------------------------------------------------------
ORDER_COLUMNS = [
    "Symbol", "Signal", "Entry Type", "OI Check",
    "Pre-Entry Trigger Time", "Pre-Entry Trigger Status",
    "Entry Trigger Time", "Entry Trigger Status",
    "Support Entry Time", "Support Trigger Status",
    "Exit Trigger Time", "Exit Trigger Status",
    "Spot Price", "ATM Strike", "Option Symbol", "Option Token", "Lot Size",
    "Days To Expiry", "Signal Confirmed At", "Entry Time", "Entry Lag (min)",
    "Lookahead Check", "Entry LTP",
    "Stop Loss LTP", "Target 1 LTP", "Target 2 LTP", "Target 3 LTP",
    "Risk/Unit (Rs)", "Quantity (Lots)", "Quantity (Units)",
    "Risk Amount (Rs)", "Capital Required (Rs)",
    "Current LTP", "Max LTP", "Min LTP",
    "T1 Hit", "T2 Hit", "T3 Hit", "Breakeven Active", "TSL Breach Streak",
    "Effective Stop", "Gross P/L (Rs)", "Costs (Rs)", "Net P/L (Rs)",
    "Order ID", "Exit Time", "Exit Reason", "Trade Mode",
    "Broker Order ID", "Actual Fill Price", "Actual Exit Price", "Exit Order ID",
]

REJECTED_COLUMNS = ["Symbol", "Trigger Time", "Signal", "Reason", "Timestamp"]


def position_to_row(pos: Position, current_ltp: float | None = None) -> dict:
    """Flatten a Position into one Orders sheet row."""
    audit = pos.audit
    slots = pos.trigger_slots + ["", "", ""]
    gross = sum(e["gross"] for e in pos.exits)
    costs = sum(e["cost"] for e in pos.exits)

    return {
        "Symbol": pos.symbol,
        "Signal": pos.signal,
        "Entry Type": pos.entry_type,
        "OI Check": pos.oi_check,
        "Pre-Entry Trigger Time": slots[0], "Pre-Entry Trigger Status": pos.signal,
        "Entry Trigger Time": slots[1], "Entry Trigger Status": pos.signal,
        "Support Entry Time": slots[2], "Support Trigger Status": pos.signal,
        "Exit Trigger Time": pos.exit_time.strftime("%H:%M:%S") if pos.exit_time else "",
        "Exit Trigger Status": pos.exit_reason,
        # Was writing the strike here -- wrong column, found in review 01-Aug.
        "Spot Price": round(pos.spot_price, 2) if pos.spot_price else "",
        "ATM Strike": pos.contract.strike,
        "Option Symbol": pos.contract.trading_symbol,
        "Option Token": pos.contract.token,
        "Lot Size": pos.lot_size,
        "Days To Expiry": (pos.contract.expiry - pos.entry_time.date()).days,
        "Signal Confirmed At": (pos.signal_confirmed_at.strftime("%H:%M:%S")
                                if pos.signal_confirmed_at else ""),
        "Entry Time": pos.entry_time.strftime("%H:%M:%S"),
        "Entry Lag (min)": (round(pos.entry_lag_mins, 1)
                            if pos.entry_lag_mins is not None else ""),
        # A negative lag means the fill happened before the signal was known.
        # That is not a warning, it is a broken backtest.
        "Lookahead Check": ("" if pos.entry_lag_mins is None
                            else "OK" if pos.entry_lag_mins >= 0
                            else "FAIL: filled before signal confirmed"),
        "Entry LTP": round(pos.entry_ltp, 2),
        "Stop Loss LTP": round(pos.stop_loss, 2),
        "Target 1 LTP": round(pos.targets[0], 2),
        "Target 2 LTP": round(pos.targets[1], 2),
        "Target 3 LTP": round(pos.targets[2], 2),
        "Risk/Unit (Rs)": round(pos.risk_per_unit, 2),
        "Quantity (Lots)": pos.lots,
        "Quantity (Units)": pos.quantity,
        "Risk Amount (Rs)": round(pos.risk_per_unit * pos.quantity, 2),
        "Capital Required (Rs)": round(pos.entry_ltp * pos.quantity, 2),
        "Current LTP": round(current_ltp, 2) if current_ltp else "",
        "Max LTP": round(pos.peak_ltp, 2),
        "Min LTP": round(pos.trough_ltp, 2),
        "T1 Hit": "YES" if pos.target_hit[0] else "",
        "T2 Hit": "YES" if pos.target_hit[1] else "",
        "T3 Hit": "YES" if pos.target_hit[2] else "",
        "Breakeven Active": "YES" if pos.breakeven_active else "",
        "TSL Breach Streak": pos.tsl_breach_streak,
        "Effective Stop": round(pos.effective_stop, 2),
        "Gross P/L (Rs)": round(gross, 2),
        "Costs (Rs)": round(costs, 2),
        "Net P/L (Rs)": round(pos.realised_pnl, 2),
        "Order ID": pos.order_id,
        "Exit Time": pos.exit_time.strftime("%H:%M:%S") if pos.exit_time else "",
        "Exit Reason": pos.exit_reason,
        "Trade Mode": pos.trade_mode,
        "Broker Order ID": pos.broker_entry_order_id,
        "Actual Fill Price": (round(pos.actual_fill_price, 2)
                              if pos.actual_fill_price else ""),
        "Actual Exit Price": (round(pos.actual_exit_price, 2)
                              if pos.actual_exit_price else ""),
        "Exit Order ID": pos.broker_exit_order_id,
    }


def build_orders_sheet(positions: list[Position],
                       current_ltps: list[float | None] | None = None) -> pd.DataFrame:
    """
    `current_ltps`, parallel to `positions` (03-Aug-26): for a position still
    OPEN when the sheet is written -- the live incremental loop's normal
    case, not BACKTEST's -- this is the one piece position_to_row can't
    derive from the Position object itself (peak/trough track the whole
    life of the trade, not "right now"). Defaults to None per position,
    which is exactly what every existing caller (BACKTEST, EOD) already
    gets, since every position they pass in is already closed.
    """
    current_ltps = current_ltps if current_ltps is not None else [None] * len(positions)
    rows = [position_to_row(p, c) for p, c in zip(positions, current_ltps)]
    return pd.DataFrame(rows, columns=ORDER_COLUMNS) if rows \
        else pd.DataFrame(columns=ORDER_COLUMNS)


def build_rejected_sheet(rejections: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rejections, columns=REJECTED_COLUMNS) if rejections \
        else pd.DataFrame(columns=REJECTED_COLUMNS)


def make_rejection(symbol: str, slot: str, signal: str, reason: str,
                   now: datetime | None = None) -> dict:
    now = now or ist_clock.now_ist()
    return {"Symbol": symbol, "Trigger Time": slot, "Signal": signal,
            "Reason": reason, "Timestamp": now.strftime("%H:%M:%S")}

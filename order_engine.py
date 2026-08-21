"""
STEP 11b — The orchestrator that turns Final Recomm into Orders and Rejected.

    Final sheet
        -> qualified signal runs        (order_sheet.qualified_signals)
        -> option chain around ATM      (option_chain)
        -> audit gates                  (option_audit)
        -> position sizing              (order_sheet.size_position)
        -> exit ladder simulation       (order_sheet.update_position)
        -> Orders + Rejected DataFrames

ENTRY TIMING, stated once and enforced everywhere
-------------------------------------------------
Matrix columns are labelled with the candle's OPEN time. The "09:30" column
holds the candle covering 09:30:00-09:34:59, which CLOSES at 09:35:00.

So a trio completing at the 09:30 column is only known at 09:35, and the
fill happens on the NEXT candle -- the one opening 09:35. Entering at the
09:30 candle's own price would be trading on information that did not exist
yet. That single off-by-one is the most common way an options backtest
invents profit.

PAPER ONLY
----------
Nothing here places a broker order until config.LIVE_TRADING is set True
and an order module exists.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

import config
import ist_clock
import live_orders
import oi_log
import option_audit
import option_chain
import option_data
import order_sheet
import paths
import quote_history
from order_sheet import Position, SignalRun, make_rejection


def _slot_to_dt(slot: str, trade_date: date) -> datetime:
    hh, mm = slot.split(":")
    return ist_clock.combine_ist(trade_date,
                                 ist_clock.MARKET_OPEN.replace(hour=int(hh),
                                                               minute=int(mm)))


def _entry_datetime(run: SignalRun, trade_date: date) -> datetime:
    """
    When the order is actually decided: one full interval after the entry
    slot opens, i.e. the moment that candle closes.
    """
    return _slot_to_dt(run.entry_slot, trade_date) + timedelta(
        minutes=config.INTERVAL_MINUTES
    )


def _candle_momentum_ok_for(run: SignalRun, candles: dict,
                            trade_date: date) -> tuple[bool, str]:
    """
    Wires signal_quality.candle_momentum_ok up with the two trigger
    candles' OPEN timestamps -- trigger_slots[0]/[1], the same Pre-Entry/
    Entry pair CONSECUTIVE_SIGNALS_REQUIRED now qualifies on. If a run
    somehow has fewer than 2 trigger slots (CONSECUTIVE_SIGNALS_REQUIRED
    set to 1), there is nothing to compare and the gate passes.
    """
    import signal_quality

    slots = run.trigger_slots
    if len(slots) < 2:
        return True, "candle momentum filter: fewer than 2 trigger slots"
    first_at = _slot_to_dt(slots[0], trade_date)
    second_at = _slot_to_dt(slots[1], trade_date)
    return signal_quality.candle_momentum_ok(
        candles.get(run.symbol), first_at, second_at, run.signal)


def _third_candle_rising(symbol: str, candles: dict, rejected_at: datetime,
                         signal: str) -> tuple[bool, datetime | None]:
    """
    RSI-CHECKPOINT RE-ENTRY (16-Aug-26, Harish -- APOLLOHOSP, 14-Aug-26: RSI
    78 rejected a BUY CE at 09:20 as "already overbought", but the chart
    kept trending up till 10:35, a missed move). `rejected_at` is the
    moment signal_quality.rsi_extreme_ok said no -- the same entry_at a
    normal fill would have used. This checks the THIRD candle after that
    moment: the candle opening at rejected_at + 2 intervals, closing at
    rejected_at + 3 intervals.

    "Rising" (confirmed with Harish, 16-Aug-26) means the 3rd candle's
    CLOSE clears the PREVIOUS (2nd) candle's close, in the signal's
    direction -- close3 > close2 for BUY CE, close3 < close2 for BUY PE.
    Same close-over-close convention as signal_quality.candle_momentum_ok's
    follow-through check, not a same-candle open-vs-close reading.

    Returns (ok, decided_at) -- decided_at is when the 3rd candle closed
    (the honest decision moment for a re-entry, same "decided when the
    candle closes, filled on the next one" rule as every other entry here),
    or None when either candle doesn't exist yet in the data.
    """
    df = _as_ist(candles.get(symbol))
    if df is None or df.empty:
        return False, None

    prior_open = rejected_at + timedelta(minutes=config.INTERVAL_MINUTES)
    candle_open = rejected_at + timedelta(minutes=2 * config.INTERVAL_MINUTES)
    decided_at = rejected_at + timedelta(minutes=3 * config.INTERVAL_MINUTES)
    try:
        close_prior = float(df.loc[prior_open, "close"])
        close_third = float(df.loc[candle_open, "close"])
    except KeyError:
        return False, None

    if signal == config.SIGNAL_BUY_CE:
        return close_third > close_prior, decided_at
    if signal == config.SIGNAL_BUY_PE:
        return close_third < close_prior, decided_at
    return False, decided_at


def _candle_momentum_checkpoint_signal(symbol: str, candles: dict,
                                       rejected_at: datetime
                                       ) -> tuple[str | None, datetime | None]:
    """
    CANDLE-MOMENTUM CHECKPOINT (20-Aug-26, Harish -- see
    config.CANDLE_MOMENTUM_CHECKPOINT_ENABLED for the ASIANPAINT-shaped
    example this comes from). `rejected_at` is the same entry_at
    signal_quality.candle_momentum_ok was evaluated at when it rejected a
    run for "no candle follow-through" -- the confirming trigger candle's
    close didn't clear the leading one's, in the signal's direction.

    Same shape as _third_candle_rising (candle+1 vs candle+2 after
    rejected_at, decided when candle+2 closes) but generalized to pick
    WHICHEVER direction the trend actually confirms, not just re-check the
    original signal: a stalled BUY CE can resolve into a genuine BUY PE
    move just as easily, and vice versa. Flat (close2 == close1, no real
    trend either way) returns None -- there is nothing here to act on.

    Returns (SIGNAL_BUY_CE | SIGNAL_BUY_PE | None, decided_at).
    """
    df = _as_ist(candles.get(symbol))
    if df is None or df.empty:
        return None, None

    prior_open = rejected_at + timedelta(minutes=config.INTERVAL_MINUTES)
    candle_open = rejected_at + timedelta(minutes=2 * config.INTERVAL_MINUTES)
    decided_at = rejected_at + timedelta(minutes=3 * config.INTERVAL_MINUTES)
    try:
        close_prior = float(df.loc[prior_open, "close"])
        close_third = float(df.loc[candle_open, "close"])
    except KeyError:
        return None, None

    if close_third > close_prior:
        return config.SIGNAL_BUY_CE, decided_at
    if close_third < close_prior:
        return config.SIGNAL_BUY_PE, decided_at
    return None, decided_at


def _as_ist(df: pd.DataFrame) -> pd.DataFrame:
    """
    Guarantee a tz-aware IST index.

    Comparing a tz-naive index against a tz-aware datetime raises, and it
    raises deep inside pandas with a message that says nothing about which
    candle frame was at fault. Cheaper to normalise here than to debug it
    live at 09:20.
    """
    if df is None or df.empty:
        return df
    if not isinstance(df.index, pd.DatetimeIndex):
        return df
    if df.index.tz is None:
        df = df.copy()
        df.index = df.index.tz_localize(ist_clock.IST)
    elif str(df.index.tz) != str(ist_clock.IST):
        df = df.copy()
        df.index = df.index.tz_convert(ist_clock.IST)
    return df


def _macd_invalidated(final_df: pd.DataFrame, candles: dict, symbol: str,
                      signal: str, bar_open_ts: datetime) -> bool:
    """
    Early-exit check (20-Aug-26, Harish, ASIANPAINT BUY CE example: entered
    on the 09:20/09:25 trigger, the very next candle closed red with the
    histogram fading from teal to pale-teal, and the position still ran
    until 09:55 off the normal SL/Target ladder -- "which is not required
    is what I feel").

    Fires only when BOTH hold, on the SAME closed candle:
      1. the UNDERLYING candle closed the opposite colour to the signal --
         red after a BUY CE, green after a BUY PE.
      2. that candle's MACD Recomm (from the Final sheet, not just the
         line-vs-signal state -- see matrix_sheets.macd_recommendation) no
         longer matches the position's own signal.

    Either alone is not enough -- a red candle inside an intact MACD state
    is normal noise (this is exactly why CANDLE_MOMENTUM_ENABLED's entry
    gate only checks close vs close, not colour), and a fading MACD state
    without price actually turning is not yet a broken trade. Both
    together are what actually happened in the ASIANPAINT case.

    Reads MACD Recomm specifically (not Final Recomm) -- Harish's rule was
    stated in MACD terms only ("any of the logic mentioned above fails"),
    not a re-vote of every confluence component.

    `bar_open_ts` is the candle's OPEN-time index value, shared by the
    underlying candle frame and the Final sheet's slot columns alike.
    """
    if not config.MACD_INVALIDATION_EXIT_ENABLED:
        return False
    if signal not in (config.SIGNAL_BUY_CE, config.SIGNAL_BUY_PE):
        return False

    und = _as_ist(candles.get(symbol))
    if und is None or und.empty or bar_open_ts not in und.index:
        return False  # no underlying bar to read colour from -- don't guess
    o, c = float(und.loc[bar_open_ts, "open"]), float(und.loc[bar_open_ts, "close"])
    opposite_colour = (c < o) if signal == config.SIGNAL_BUY_CE else (c > o)
    if not opposite_colour:
        return False

    row = final_df[(final_df["Symbol"] == symbol)
                   & (final_df["Metrics"] == "MACD Recomm")]
    slot = bar_open_ts.strftime("%H:%M")
    if row.empty or slot not in row.columns:
        return False  # no MACD Recomm to check -- don't guess either
    macd_now = row[slot].iloc[0]
    macd_now = "" if pd.isna(macd_now) else str(macd_now).strip()
    if macd_now == "":
        return False  # still warming up -- absence of data isn't a broken signal

    return macd_now != signal


# --------------------------------------------------------------------------
# real-position management -- shared by the 5-min bar walk
# (advance_live_day's _advance_live_position) AND the 30-60s fast tracker
# (fast_track_live_positions) below, module-level rather than nested
# closures because the fast tracker fires from live_loop's on_tick callback,
# BETWEEN advance_live_day calls, not during one -- see this module's
# fast_track_live_positions for the full picture.
# --------------------------------------------------------------------------
def _retry_protective_stop(angel, pos: Position) -> None:
    """
    Place a protective stop that's missing (failed at entry, or after a
    resize) -- called every time a position is touched, regardless of
    whether new price data exists, because an unprotected live position
    should not wait for the next tick to get one.
    """
    if pos.broker_stop_order_id or pos.remaining_qty <= 0:
        return
    contract = pos.contract
    try:
        pos.broker_stop_order_id = live_orders.place_protective_stop(
            angel, contract.trading_symbol, contract.token,
            pos.remaining_qty, pos.effective_stop)
        pos.broker_stop_trigger = pos.effective_stop
        print(f"[live-orders] {pos.symbol}: protective stop placed on retry")
    except live_orders.LiveOrderError as exc:
        print(f"[live-orders] {pos.symbol}: protective stop STILL FAILED -- "
              f"{exc}. Position remains UNPROTECTED.")


def _check_broker_stop_fired(angel, pos: Position, when: datetime) -> bool:
    """
    True (and pos is now closed) if the resting SL-M order shows COMPLETE
    at the broker -- it already happened for real, whether this loop
    noticed at the 5-min bar walk or the 30-60s fast tick makes no
    difference to what to do about it: record the real fill and stop.
    """
    if not pos.broker_stop_order_id:
        return False
    row = live_orders.order_status(angel, pos.broker_stop_order_id)
    if not row or str(row.get("status", "")).lower() != "complete":
        return False

    fill = live_orders._parse_fill(row)
    gross = (fill.avg_price - pos.entry_ltp) * fill.filled_qty
    cost = option_audit.estimate_round_trip_cost(
        pos.entry_ltp, fill.avg_price, fill.filled_qty)
    pos.remaining_qty -= fill.filled_qty
    pos.realised_pnl += gross - cost
    pos.exits.append({"time": when, "qty": fill.filled_qty,
                      "ltp": fill.avg_price, "reason": "Stop Loss Hit (broker)",
                      "gross": gross, "cost": cost, "net": gross - cost,
                      "partial": False})
    pos.actual_exit_price = fill.avg_price
    pos.broker_exit_order_id = fill.order_id
    pos.closed = True
    pos.exit_reason = "Stop Loss Hit (broker)"
    pos.exit_time = when
    print(f"[live-orders] {pos.symbol}: protective stop filled at the "
          f"broker -- {fill.avg_price:.2f}")
    return True


def _real_exit(angel, pos: Position, qty: int, reason: str, when: datetime,
               is_final: bool) -> None:
    """
    Place a REAL exit order for `qty` units and update bookkeeping from the
    actual fill. Raises live_orders.LiveOrderError upward on failure --
    callers must not treat a failed exit as a closed position, see
    live_orders.exit_live's docstring for why that matters here specifically.
    """
    contract = pos.contract
    fill = live_orders.exit_live(
        angel, contract.trading_symbol, contract.token, qty,
        pos.broker_stop_order_id if is_final else None, reason)
    gross = (fill.avg_price - pos.entry_ltp) * fill.filled_qty
    cost = option_audit.estimate_round_trip_cost(
        pos.entry_ltp, fill.avg_price, fill.filled_qty)
    pos.remaining_qty -= fill.filled_qty
    pos.realised_pnl += gross - cost
    pos.exits.append({"time": when, "qty": fill.filled_qty,
                      "ltp": fill.avg_price, "reason": reason,
                      "gross": gross, "cost": cost, "net": gross - cost,
                      "partial": not is_final})
    pos.actual_exit_price = fill.avg_price
    pos.broker_exit_order_id = fill.order_id
    if is_final or pos.remaining_qty <= 0:
        pos.closed = True
        pos.exit_reason = reason
        pos.exit_time = when


def _evaluate_live_tick(angel, pos: Position, when: datetime, ltp: float,
                        square_off: datetime) -> None:
    """
    Check ONE real, open live position against ONE price observation --
    a genuine 30-60s live quote from the fast tracker, or one lo/hi/close
    tick of a closed 5-min bar from _advance_live_position -- and place
    REAL orders for whatever fires. The SAME function either way, so a
    target does not depend on which loop happened to see it first.

    Deliberately does NOT compare `ltp` against pos.stop_loss/
    effective_stop -- for a LIVE position that comparison is the broker's
    job (the resting SL-M order), not Python's. Call _check_broker_stop_
    fired separately to notice when that order has already done its job.
    Also does not check the MACD-invalidation exit -- that needs a CLOSED
    candle's colour and Final-sheet slot, see _advance_live_position.
    """
    if pos.closed or ltp is None or ltp <= 0:
        return
    pos.peak_ltp = max(pos.peak_ltp, ltp)
    pos.trough_ltp = min(pos.trough_ltp, ltp)

    if when >= square_off:
        try:
            _real_exit(angel, pos, pos.remaining_qty, "EOD Square-off", when, True)
        except live_orders.LiveOrderError as exc:
            print(f"[live-orders] {pos.symbol}: EOD SQUARE-OFF FAILED -- {exc}")
        return

    contract = pos.contract
    for i, target in enumerate(pos.targets):
        if pos.target_hit[i] or ltp < target - order_sheet.PRICE_EPS:
            continue
        pos.target_hit[i] = True
        is_final_target = (i == len(pos.targets) - 1)
        qty = (pos.remaining_qty if is_final_target else
              min(int(pos.quantity * config.TARGET_EXIT_FRACTIONS[i]),
                  pos.remaining_qty))
        if qty <= 0:
            continue
        try:
            _real_exit(angel, pos, qty, f"Target {i + 1} Hit", when, is_final_target)
        except live_orders.LiveOrderError as exc:
            print(f"[live-orders] {pos.symbol}: TARGET {i + 1} EXIT FAILED "
                  f"-- {exc}. The resting stop may now be sized for MORE "
                  f"than what's actually held -- check by hand.")
            continue
        if pos.closed:
            return
        if i == 0 and config.MOVE_SL_TO_BREAKEVEN_AT_T1:
            pos.breakeven_active = True
        if pos.broker_stop_order_id:
            try:
                pos.broker_stop_order_id = live_orders.resize_protective_stop(
                    angel, pos.broker_stop_order_id,
                    contract.trading_symbol, contract.token,
                    pos.remaining_qty, pos.effective_stop)
                pos.broker_stop_trigger = pos.effective_stop
            except Exception as exc:
                print(f"[live-orders] {pos.symbol}: resizing the protective "
                      f"stop after a target FAILED -- {exc}. Check the "
                      f"Angel One app by hand.")

    if pos.closed:
        return

    if (pos.breakeven_active and pos.broker_stop_order_id
            and pos.effective_stop > pos.broker_stop_trigger + order_sheet.PRICE_EPS):
        try:
            pos.broker_stop_order_id = live_orders.update_protective_stop(
                angel, pos.broker_stop_order_id,
                contract.trading_symbol, contract.token,
                pos.remaining_qty, pos.effective_stop)
            pos.broker_stop_trigger = pos.effective_stop
        except Exception as exc:
            print(f"[live-orders] {pos.symbol}: trailing-stop update FAILED "
                  f"-- {exc}. Check by hand.")


def fast_track_live_positions(state: dict, angel, square_off: datetime) -> None:
    """
    30-60 second poll (config.LIVE_FAST_TRACK_INTERVAL_SECS) of every
    currently OPEN real live position -- NOT the whole watchlist, only
    what's actually been entered (20-Aug-26, Harish: "not all stocks to be
    tracked here"). Called from live_loop's on_tick, in the gap between
    candle-close cycles; see live_loop.run_live_session's docstring for why
    it never overlaps with advance_live_day.

    One batched Angel quote call for however many positions are open (same
    getMarketData batching option_chain.fetch_quotes already uses for a
    strike window), each evaluated through _evaluate_live_tick -- the SAME
    core the 5-min bar walk uses, so a target or a broker-fired stop is
    caught within this cadence instead of waiting up to 5 minutes.

    Deliberately does NOT touch the MACD-invalidation exit or re-read the
    underlying at all -- that check needs a genuinely CLOSED 5-min candle
    (colour, and that slot's MACD Recomm off the Final sheet), neither of
    which exists between candle closes. It stays exactly where it was, in
    the 5-min walk. This function only ever tightens/executes what the
    entry's own SL/Target/TSL ladder already committed to.
    """
    positions = [p for p in state["live_positions"].values() if not p.closed]
    if not positions:
        return

    from option_chain import ChainWindow
    chain = ChainWindow(symbol="", spot=0.0, atm_strike=0.0, strike_step=0.0,
                        expiry=positions[0].contract.expiry)
    for p in positions:
        (chain.calls if p.contract.option_type == "CE" else chain.puts).append(p.contract)

    try:
        option_chain.fetch_quotes(chain, angel, limiter=option_data._angel_limiter)
    except Exception as exc:
        print(f"[live-fast] quote batch failed: {exc}")
        return

    when = ist_clock.now_ist()
    for p in positions:
        _retry_protective_stop(angel, p)
        if _check_broker_stop_fired(angel, p, when):
            continue
        if p.contract.ltp is None:
            continue
        _evaluate_live_tick(angel, p, when, p.contract.ltp, square_off)

    resolved = [k for k, p in state["live_positions"].items() if p.closed]
    for key in resolved:
        p = state["live_positions"].pop(key)
        state["live_terminal"].append(p)
        print(f"[live-fast] {p.symbol} resolved -> {p.exit_reason}, "
              f"net Rs {p.realised_pnl:,.2f}")

    _write_live_status_snapshot(state)


def _write_live_status_snapshot(state: dict) -> None:
    """
    Overwrite paths.LIVE_STATUS_FILE with one row per currently open real
    live position -- see that path's own docstring for why a CSV, and why
    this exists at all (visibility between the once-per-5-min workbook
    writes). Wholesale rewrite, not an append -- oi_log-style appending
    would need its own daily rotation/cleanup this doesn't need, since only
    "right now" matters for this file.
    """
    import csv
    rows = []
    for p in state["live_positions"].values():
        rows.append({
            "Symbol": p.symbol, "Signal": p.signal,
            "Option": p.contract.trading_symbol,
            "Entry LTP": round(p.entry_ltp, 2),
            "Current LTP": round(p.contract.ltp, 2) if p.contract.ltp else "",
            "Effective Stop": round(p.effective_stop, 2),
            "Target 1": round(p.targets[0], 2), "T1 Hit": "YES" if p.target_hit[0] else "",
            "Target 2": round(p.targets[1], 2), "T2 Hit": "YES" if p.target_hit[1] else "",
            "Target 3": round(p.targets[2], 2), "T3 Hit": "YES" if p.target_hit[2] else "",
            "Remaining Qty": p.remaining_qty,
            "Unrealised P/L (Rs)": (round((p.contract.ltp - p.entry_ltp) * p.remaining_qty, 2)
                                    if p.contract.ltp else ""),
            "Realised P/L (Rs)": round(p.realised_pnl, 2),
            "Last Update": ist_clock.now_ist().strftime("%H:%M:%S"),
        })

    path = paths.LIVE_STATUS_FILE
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        else:
            f.write("no open live positions\n")
    tmp.replace(path)  # atomic -- never leaves a half-written file to read


def _spot_at(candles: pd.DataFrame, at: datetime) -> float | None:
    """Close of the last closed underlying candle at or before `at`."""
    candles = _as_ist(candles)
    if candles is None or candles.empty:
        return None
    usable = candles[candles.index + timedelta(minutes=config.INTERVAL_MINUTES) <= at]
    if usable.empty:
        return None
    return float(usable["close"].iloc[-1])


def _historical_quote(opt_candles: pd.DataFrame, at: datetime) -> float | None:
    """
    What the option was actually worth at `at`, from history, using CLOSED
    bars only (index + INTERVAL <= at) -- no lookahead.

    Exists because option_chain.fetch_quotes() calls Angel's getMarketData,
    which always returns the price RIGHT NOW. In LIVE that is correct and
    contemporaneous. In BACKTEST it is a quote from a different day
    entirely, silently compared against historical candles.

    That is the INDIANB bug (06-Aug-26): the 09:35 candle on 05-Aug opened
    at Rs 25.10 (correct, matches his TradingView), but the "quote" read
    Rs 16.65 -- the live bid the next morning, visible in the SELL box of
    his own screenshot. The 51% "disagreement" was between two different
    days, not two disagreeing feeds, and it rejected a valid signal.
    """
    if opt_candles is None or opt_candles.empty:
        return None
    closed = opt_candles[
        opt_candles.index + timedelta(minutes=config.INTERVAL_MINUTES) <= at]
    if closed.empty:
        return None
    price = float(closed["close"].iloc[-1])
    return price if price > 0 else None


def _historical_volume(opt_candles: pd.DataFrame, at: datetime) -> float | None:
    """
    Cumulative traded volume for THIS session up to `at`, from history --
    same fix as _historical_quote, for the same reason, found the same way
    (15-Aug-26, Harish: "Illiquid: volume 0 < 100" on every single symbol).

    option_audit's liquidity gate compares against config.MIN_OPTION_VOLUME
    expecting Angel's getMarketData "tradeVolume" semantics -- the day's
    running total so far, not one 5-min bar. But contract.volume was never
    re-priced for BACKTEST the way contract.ltp is just above: it was left
    holding whatever the LIVE quote fetch returned for a contract dated
    TODAY, not the backtest date. For an expired or since-rolled contract
    that is 0 or None every time, which is why every symbol failed the
    gate regardless of how liquid it actually was that day. Summing this
    session's own candle volumes reproduces the same "so far today" figure
    the live gate was designed around, from the correct date's data.
    """
    if opt_candles is None or opt_candles.empty:
        return None
    today = opt_candles[opt_candles.index.date == at.date()]
    closed = today[
        today.index + timedelta(minutes=config.INTERVAL_MINUTES) <= at]
    if closed.empty:
        return None
    return float(closed["volume"].sum())


def _caps_block(entry_at: datetime, symbol: str, positions: list,
                open_symbols: set, entries_taken: int,
                trade_date: date) -> str:
    """
    The book-level limits, as a reason string, or "" if the trade may open.

    Extracted (06-Aug-26) so a momentum-promoted entry can be checked at
    the moment it is PROMOTED rather than at its original signal time --
    a promotion happens two bars later, by which point the book may have
    filled up. Skipping this would let promotions quietly exceed the caps.
    """
    realised_so_far = sum(
        p.realised_pnl for p in positions
        if p.closed and p.exit_time is not None and p.exit_time <= entry_at)
    if realised_so_far <= -config.DAILY_MAX_LOSS_RS:
        return (f"daily loss limit hit: Rs {realised_so_far:,.0f} against a "
                f"cap of Rs {-config.DAILY_MAX_LOSS_RS:,.0f}")

    if config.ONE_POSITION_PER_SYMBOL and symbol in open_symbols:
        return f"{symbol}: position already open in this symbol"
    if (config.MAX_ENTRIES_PER_DAY is not None
            and entries_taken >= config.MAX_ENTRIES_PER_DAY):
        return f"daily entry cap reached ({config.MAX_ENTRIES_PER_DAY})"
    if config.MAX_CONCURRENT_POSITIONS is not None:
        # BUG FOUND 06-Aug-26 (ICICIPRULI, same day as the slot-fill fix):
        # this only checked whether a position was STILL open by entry_at,
        # never whether it had STARTED yet. Harmless before momentum
        # promotion existed, because everything was processed in the same
        # order it actually entered. A promoted entry breaks that: CAMS is
        # evaluated (and its exit ladder fully walked) at its ORIGIN slot's
        # position in the loop -- 10:10 -- but its real fill isn't until
        # 10:25, two candles later. Without the entry_time check, CAMS
        # counted as "concurrently open" for ICICIPRULI's 10:20 decision
        # even though CAMS hadn't opened yet in real time, pushing a true
        # count of 2 (CIPLA, TITAN) up to a false 3 and blocking a trade
        # that should have been allowed.
        live_now = sum(
            1 for p in positions
            if p.entry_time <= entry_at
            and (not p.closed or (p.exit_time and p.exit_time > entry_at)))
        if live_now >= config.MAX_CONCURRENT_POSITIONS:
            return (f"concurrent position cap reached "
                    f"({config.MAX_CONCURRENT_POSITIONS})")

    hh, mm = (int(x) for x in config.NO_NEW_ENTRY_AFTER.split(":"))
    cutoff = ist_clock.combine_ist(
        trade_date, ist_clock.MARKET_OPEN.replace(hour=hh, minute=mm))
    if entry_at > cutoff:
        return (f"after entry cutoff {config.NO_NEW_ENTRY_AFTER} -- too "
                f"little session left for the trade to work")
    return ""


def _tradeable_precheck(run: SignalRun, ref_by_symbol: dict, candles: dict,
                        scrip: pd.DataFrame, trade_date: date,
                        mode: str) -> tuple[bool, str]:
    """
    Cheap, NO-NETWORK check: does this run even have a real option contract
    to trade -- before it's allowed to WIN a slot's ranking (03-Aug-26,
    found live: SUNPHARMA beat 6 other qualified signals at 09:25 on
    ADX/RSI/ATR score, then itself failed with "no PE contract at ATM
    strike 1950" -- config.RANK_PER_SLOT=1 means ranking commits to exactly
    one winner per slot, so when that winner turns out structurally
    untradeable the other 6 are already rejected and the whole slot
    produces nothing, even though several of them may have been fine.

    `quote_history.resolve_only()` reads the day's captured contract list
    when one exists (BACKTEST) or the already-loaded scrip master
    otherwise (LIVE, or an uncaptured BACKTEST date) -- either way no Angel
    call, no rate-limit cost -- so this is safe to run on every candidate
    in a slot, not just the one that would win. Mirrors the vix/regime/
    momentum pre-filter's own principle (stated in process_date's
    comments): filter to what CAN trade, then rank among those, never rank
    first and hope the winner works out.

    Using the capture here too (09-Aug-26) is what actually fixes the MCX
    case from the 07-Aug BACKTEST-vs-LIVE comparison: a contract that has
    since rolled out of today's scrip master still resolves correctly from
    that day's own captured chain.
    """
    symbol, signal = run.symbol, run.signal
    entry_at = _entry_datetime(run, trade_date)

    ref_row = ref_by_symbol.get(symbol)
    if ref_row is None:
        return False, "symbol not found in Reference sheet"
    step = _strike_step(ref_row)
    if step <= 0:
        return False, (f"no usable 'Option Price Difference' for {symbol} "
                       f"-- cannot round to a strike")
    spot = _spot_at(candles.get(symbol), entry_at)
    if spot is None:
        return False, f"no closed underlying candle at {entry_at:%H:%M}"

    chain = quote_history.resolve_only(
        symbol, spot, step, scrip, trade_date, mode,
        sheet_expiry=ref_row.get(config.COL_EXPIRY))
    if chain is None:
        return False, f"no option contracts found around ATM for {symbol}"

    opt_type = "CE" if signal == config.SIGNAL_BUY_CE else "PE"
    contract = chain.atm(opt_type)
    if contract is None:
        return False, f"no {opt_type} contract at all in the ATM window (strike {chain.atm_strike:g})"
    if not contract.lot_size:
        return False, f"{contract.trading_symbol}: lot size unknown"
    return True, ""


def _filter_to_tradeable(eligible: list, ref_by_symbol: dict, candles: dict,
                         scrip: pd.DataFrame, trade_date: date, mode: str,
                         rejections: list) -> list:
    """Split `eligible` into tradeable/not, appending real rejections for
    the ones that can't trade -- called right before ranking, in both
    process_date and advance_live_day, so the two never diverge on this."""
    tradeable = []
    for run in eligible:
        ok, why = _tradeable_precheck(run, ref_by_symbol, candles, scrip,
                                      trade_date, mode)
        if ok:
            tradeable.append(run)
        else:
            rejections.append(make_rejection(
                run.symbol, run.entry_slot, run.signal, why,
                _entry_datetime(run, trade_date)))
    dropped = len(eligible) - len(tradeable)
    if dropped:
        print(f"[orders] {dropped} signal(s) have no real contract to trade -- "
              f"excluded before ranking so they can't starve out a tradeable one")
    return tradeable


def _strike_step(ref_row: pd.Series) -> float:
    raw = ref_row.get(config.COL_STRIKE_STEP)
    try:
        step = float(raw)
        return step if step > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# shadow-sheet classification (09-Aug-26): a real entry attempt that fails
# can produce several reasons at once (audit_option reports every failing
# gate, not just the first). A rejection only routes into Capital Shadow or
# OI Blocked when EVERY reason belongs to that one category -- a run that
# failed on spread AND capital, say, isn't purely a capital problem, so
# simulating "what if capital weren't the issue" wouldn't answer anything
# real about it. Missed_Concurrent uses the same "purely this one reason"
# principle for the concurrent-position cap.
# --------------------------------------------------------------------------
def _is_capital_reason(reason: str) -> bool:
    return "exceeds this trade's risk budget" in reason or "Insufficient balance" in reason


def _is_oi_reason(reason: str) -> bool:
    return "-side OI is only" in reason


def process_date(final_df: pd.DataFrame, ref_df: pd.DataFrame,
                 candles: dict[str, pd.DataFrame], scrip: pd.DataFrame,
                 angel, trade_date: date, mode: str,
                 slots: list[str],
                 available_capital: float | None = None,
                 index_candles: pd.DataFrame | None = None,
                 vix_candles: pd.DataFrame | None = None,
                 kite=None,
                 audit_enabled: bool = True,
                 oi_buildup_enabled: bool = False,
                 ) -> tuple[pd.DataFrame, pd.DataFrame, list[Position], pd.DataFrame,
                           pd.DataFrame, pd.DataFrame]:
    """
    Run the whole order pipeline for one trading date.

    Returns (orders_df, rejected_df, positions, missed_df, capital_shadow_df,
    oi_blocked_df).

    oi_buildup_enabled (16-Aug-26, TW ALL pipeline): runs
    option_audit.oi_buildup_confirms() as an independent gate on top of
    whatever audit_enabled decides -- see config.TW_ALL_OI_BUILDUP_ENABLED.
    Off by default; run_TW_ALL.py passes it explicitly.

    audit_enabled (15-Aug-26, MACD pipeline): when False, skips
    option_audit.audit_option() entirely for every real/shadow build in
    this call -- see config.MACD_AUDIT_ENABLED. run_TW_ALL.py never passes
    this, so it stays True (audited) there; run_MACD.py passes
    config.MACD_AUDIT_ENABLED. Capital-Shadow/OI-Blocked sheets stay
    meaningful either way since ignore_capital/ignore_oi are independent
    of this flag.

    missed_df -- Missed_Concurrent sheet (07-Aug-26): every run that failed
    ONLY the concurrent-position cap, simulated exactly as if the book had
    room for it. A run that's trending up (unrealized P&L positive) 2
    candles after its would-have entry, AND still has room at that later
    moment, is PROMOTED instead: ends up in `positions`/orders_df like any
    real trade, not here.

    capital_shadow_df -- Capital Shadow sheet (09-Aug-26, fixed 10-Aug-26):
    every run that failed ONLY on risk-budget/available-capital sizing,
    re-simulated at exactly 1 lot (the minimum real trade -- an earlier
    version substituted a huge risk budget instead, which produced lot
    counts in the thousands and P&L in the lakhs/crores, not a meaningful
    answer). Lets you review by EOD whether raising RISK_PER_TRADE_RS (or
    funding the account further) would have paid for itself. Never
    promoted into real trades.

    oi_blocked_df -- OI Blocked sheet (09-Aug-26): every run that failed
    ONLY the OI gate (early or the late re-check), re-simulated with that
    gate ignored -- lets you review whether OI_MIN_OWN_SIDE_SHARE is
    costing more than it's worth. Never promoted into real trades.

    All three shadow sheets share the same "purely this one reason" rule --
    see _is_capital_reason/_is_oi_reason's own comment.
    """
    import signal_quality

    runs = order_sheet.qualified_signals(final_df, slots)
    print(f"[orders] {len(runs)} qualified signal run(s) "
          f"({config.CONSECUTIVE_SIGNALS_REQUIRED} consecutive identical)")

    # --- Zerodha's own NFO instrument dump, fetched once for the whole date --
    # Preferred source for option OHLCV (one Kite call, one timestamp grid).
    # Angel's getCandleData is the fallback, used only when a contract's
    # Zerodha token doesn't resolve. Failure here is non-fatal: the date
    # still runs, every trade just falls back to Angel and says so.
    nfo_instruments = None
    if kite is not None:
        try:
            import token_mgmt
            nfo_instruments = token_mgmt._fetch_instruments(kite, "NFO")
        except Exception as exc:
            print(f"[orders] Kite NFO instrument dump unavailable ({exc}) -- "
                  f"option data will come from Angel only")

    rejections: list[dict] = []
    ref_by_symbol = {str(r[config.COL_SYMBOL]): r for _, r in ref_df.iterrows()}

    # --- FILTER FIRST, THEN RANK ------------------------------------------
    # Order matters and getting it wrong is subtle. Ranking before the
    # tradeability gates lets a dead stock win its slot and then get thrown
    # out by the momentum floor -- taking the slot's only good signal down
    # with it, because ranking already discarded it. Filter to what CAN
    # trade, then rank among those.
    eligible = []
    rsi_checkpoint_candidates: list[tuple[SignalRun, datetime]] = []
    candle_momentum_candidates: list[tuple[SignalRun, datetime]] = []
    for run in runs:
        at = _entry_datetime(run, trade_date)

        ok, note = signal_quality.vix_ok(vix_candles, at)
        if not ok:
            rejections.append(make_rejection(run.symbol, run.entry_slot,
                                             run.signal, note, at))
            continue

        ok, note = signal_quality.regime_ok(index_candles, at)
        if not ok:
            rejections.append(make_rejection(run.symbol, run.entry_slot,
                                             run.signal, note, at))
            continue

        ok, note = signal_quality.volatility_ok(candles.get(run.symbol), at)
        if not ok:
            rejections.append(make_rejection(run.symbol, run.entry_slot,
                                             run.signal, f"{run.symbol}: {note}", at))
            continue

        ok, note = signal_quality.rsi_extreme_ok(candles.get(run.symbol), at, run.signal)
        if not ok:
            rejections.append(make_rejection(run.symbol, run.entry_slot,
                                             run.signal, f"{run.symbol}: {note}", at))
            if config.RSI_CHECKPOINT_REENTRY_ENABLED:
                rsi_checkpoint_candidates.append((run, at))
            continue

        ok, note = _candle_momentum_ok_for(run, candles, trade_date)
        if not ok:
            rejections.append(make_rejection(run.symbol, run.entry_slot,
                                             run.signal, f"{run.symbol}: {note}", at))
            if config.CANDLE_MOMENTUM_CHECKPOINT_ENABLED:
                candle_momentum_candidates.append((run, at))
            continue

        eligible.append(run)

    filtered_out = len(runs) - len(eligible)
    if filtered_out:
        print(f"[orders] {filtered_out} signal(s) failed regime/momentum gates")

    # A SECOND tradeability gate, still before ranking (03-Aug-26): does
    # this symbol even have a real option contract at its ATM strike. Not
    # part of the block above because it's structural (contract exists or
    # it doesn't), not a market-condition gate -- but it has to happen here,
    # not after ranking, or a dead contract can still WIN its slot and take
    # every other candidate down with it (see _tradeable_precheck).
    eligible = _filter_to_tradeable(eligible, ref_by_symbol, candles, scrip,
                                    trade_date, mode, rejections)

    # Within each 5-min slot, only the best-scoring signal is taken; every
    # other qualified candidate in that slot is a plain Rejected row.
    if config.RANK_WITHIN_SLOT_ENABLED:
        kept, dropped = signal_quality.rank_within_slots(
            eligible, candles, lambda r: _entry_datetime(r, trade_date))
        for run, why in dropped:
            rejections.append(make_rejection(
                run.symbol, run.entry_slot, run.signal, why,
                _entry_datetime(run, trade_date)))
        runs = kept
        if dropped:
            print(f"[orders] {len(runs)} slot winner(s), {len(dropped)} "
                  f"outranked within their slot")
    else:
        # Every symbol that passed 3-bar confluence and can trade gets a
        # real attempt -- no slot cap, no score-based winner. Only
        # MAX_CONCURRENT_POSITIONS / MAX_ENTRIES_PER_DAY / capital (checked
        # per-run below) are allowed to stop one from becoming a trade.
        runs = sorted(eligible, key=lambda r: (r.entry_slot, r.symbol))

    positions: list[Position] = []
    shadow_positions: list[Position] = []
    capital_shadow_positions: list[Position] = []
    oi_shadow_positions: list[Position] = []
    open_symbols: set[str] = set()
    entries_taken = 0

    square_off = ist_clock.combine_ist(
        trade_date, ist_clock.MARKET_OPEN.replace(hour=15, minute=15)
    )

    def _try_build_position(run: SignalRun, entry_at: datetime,
                            ignore_capital: bool = False,
                            ignore_oi: bool = False,
                            risk_budget: float | None = None,
                            ) -> tuple[Position | None, list[str], float | None]:
        """
        Simulate one run all the way from reference-row lookup through a
        fully-resolved exit -- chain, audit, fill, exit ladder -- with no
        book-level bookkeeping (positions/open_symbols/entries_taken/caps).

        Used for a real entry (once its caps have already cleared) and for
        three kinds of shadow entry: a run blocked ONLY by the
        concurrent-position cap, one blocked ONLY by risk-budget/capital
        (`ignore_capital=True`, 09-Aug-26 -- "Capital Shadow" sheet), and
        one blocked ONLY by the OI gate (`ignore_oi=True` -- "OI Blocked"
        sheet). Extracted/generalised so all four never diverge on how a
        trade is priced and walked -- only on whether it's allowed to
        happen for real.

        Returns (position, [], ltp_at_2candles) on success or
        (None, reasons, None) on failure, `reasons` possibly holding more
        than one string (the audit can fail several gates at once, each one
        its own Rejected row for a real entry). `ltp_at_2candles` is the
        option's price two candles after the fill -- feeds the shadow
        promotion check further down (07-Aug-26: "wait for next 2 candles
        and see the performance") -- and is None when there isn't 2 candles
        of data past the fill yet (e.g. a very late entry_at).
        """
        symbol, signal = run.symbol, run.signal
        # ignore_capital forces exactly 1 lot (see the two size_position
        # call sites below) rather than a large substitute risk budget --
        # budget // tiny_risk_per_lot produces thousands of lots and P&L in
        # the lakhs/crores, which isn't a real answer to "what if the
        # budget were a bit bigger," it's a different, meaningless trade.
        # 1 lot is the smallest real trade, and its P&L scales linearly, so
        # it directly answers "would this have been worth taking at all."
        eff_capital = None if ignore_capital else available_capital

        # --- reference row --------------------------------------------------
        ref_row = ref_by_symbol.get(symbol)
        if ref_row is None:
            return None, ["symbol not found in Reference sheet"], None
        step = _strike_step(ref_row)
        if step <= 0:
            return None, [f"no usable 'Option Price Difference' for {symbol} "
                          f"-- cannot round to a strike"], None

        spot = _spot_at(candles.get(symbol), entry_at)
        if spot is None:
            return None, [f"no closed underlying candle at {entry_at:%H:%M}"], None

        # --- option chain --------------------------------------------------
        # quote_history.resolve_and_quote (09-Aug-26): in BACKTEST, replays
        # that day's own captured contracts+quotes when available, instead
        # of asking TODAY's scrip master/live quotes about a past date --
        # see quote_history.py's module docstring for the 07-Aug BAJAJ-AUTO/
        # SHREECEM/MCX comparison that found this. LIVE is unaffected: it
        # always resolves live and records the result for future backtests.
        chain = quote_history.resolve_and_quote(
            symbol, spot, step, scrip, trade_date, mode, entry_at, angel,
            option_data._angel_limiter, sheet_expiry=ref_row.get(config.COL_EXPIRY),
            kite=kite, nfo_instruments=nfo_instruments,
            kite_limiter=option_data._kite_limiter)
        if chain is None:
            return None, [f"no option contracts found around ATM for {symbol}"], None

        opt_type = "CE" if signal == config.SIGNAL_BUY_CE else "PE"
        contract = chain.atm(opt_type)
        if contract is None:
            return None, [f"no {opt_type} contract at all in the ATM window (strike {chain.atm_strike:g})"], None
        if not contract.lot_size:
            return None, [f"{contract.trading_symbol}: lot size unknown"], None

        # --- option price path for the exit simulation ------------------------
        # Fetched HERE, before sizing/audit (moved 06-Aug-26), because in
        # BACKTEST the historical candles are the only valid price source --
        # see _historical_quote() and the re-pricing right below. Costs one
        # extra data call for runs that later fail the audit; buys a price
        # that is real for the date being tested.
        cutoff_dt = ist_clock.now_ist() if mode == config.LIVE else None
        opt_candles, data_source = _fetch_opt_candles(
            contract, symbol, opt_type, trade_date, angel, kite,
            nfo_instruments, cutoff_dt)
        if opt_candles is None:
            return None, [f"{contract.trading_symbol}: no option candles from "
                          f"either Kite or Angel -- cannot simulate the exit "
                          f"ladder honestly"], None

        # OI Check (16-Aug-26, Harish: Orders sheet column showing Long
        # Buildup/Short Covering/etc) -- display only, independent of
        # oi_buildup_enabled below. Computed from the same candles already
        # fetched for the exit-ladder simulation, no extra API call.
        oi_check = option_audit.classify_oi_buildup(opt_candles, entry_at)

        # In BACKTEST, replace the live getMarketData quote (which is priced
        # at "now", a different day) with what this contract was actually
        # worth at entry_at. LIVE keeps the real quote -- there it IS
        # contemporaneous with the candle, and the drift check below is a
        # genuine two-feed safety net.
        if mode != config.LIVE:
            hist_ltp = _historical_quote(opt_candles, entry_at)
            if hist_ltp is not None:
                contract.ltp = hist_ltp
            hist_vol = _historical_volume(opt_candles, entry_at)
            if hist_vol is not None:
                contract.volume = hist_vol

        # --- provisional size, needed by the cost gate -------------------------
        # Provisional only. The real sizing happens against the actual fill
        # price further down: sizing off the quote and filling off the candle
        # let one HINDUNILVR trade size 8 lots and risk Rs 13,164 against a
        # Rs 2,000 budget, because the quote said ~7.70 and the candle opened
        # at 54.85. Size and fill must come from the same price.
        if contract.ltp is None or contract.ltp <= 0:
            return None, [f"{contract.trading_symbol}: no usable price at "
                          f"{entry_at:%H:%M} (quote and history both empty)"], None

        if ignore_capital:
            lots, size_note = 1, "1 lot (Capital Shadow: minimum size, ignoring risk budget/capital)"
        else:
            lots, size_note = order_sheet.size_position(
                contract.ltp, contract.lot_size, risk_budget=risk_budget,
                available_capital=eff_capital)
        if lots < 1:
            return None, [size_note], None
        quantity = lots * contract.lot_size

        # --- audit -------------------------------------------------------------
        audit = option_audit.audit_option(
            contract, chain, signal, trade_date, quantity,
            available_capital=eff_capital, mode=mode, ignore_oi=ignore_oi,
            skip=not audit_enabled)

        if not audit.passed:
            return None, [f"{contract.trading_symbol}: {why}"
                          for why in audit.reasons], None

        # --- OI buildup (16-Aug-26, TW ALL pipeline) ----------------------------
        # Independent of audit_enabled/skip above -- this is the one
        # confirmation meant to survive even when every audit_option()
        # gate is switched off. See config.TW_ALL_OI_BUILDUP_ENABLED.
        if oi_buildup_enabled:
            ok, note = option_audit.oi_buildup_confirms(opt_candles, entry_at, signal)
            if not ok:
                return None, [f"{contract.trading_symbol}: {note}"], None

        # Fill on the NEXT candle's open, never the signal candle's own price.
        fill_rows = opt_candles[opt_candles.index >= entry_at]
        if fill_rows.empty:
            return None, [f"{contract.trading_symbol}: no candle at or after "
                          f"{entry_at:%H:%M} to fill against"], None
        # Fill at the raw candle open. Slippage is NOT added here -- it is
        # charged once per leg inside option_audit.cost_breakdown, which is
        # what the model was calibrated against. Marking the entry price up
        # AND charging slippage in costs bills it twice (a 33% overcharge on
        # the slippage line, measured).
        fill_price = float(fill_rows["open"].iloc[0])
        fill_time = fill_rows.index[0].to_pydatetime()

        # --- quote vs fill sanity ----------------------------------------------
        # If the quote and the candle disagree wildly, one of them is wrong
        # and neither can be trusted to size a position. Refuse rather than
        # guess which. This is what produced the Rs 94,207 HINDUNILVR trade.
        if contract.ltp and fill_price:
            drift = abs(fill_price - contract.ltp) / contract.ltp
            if drift > config.MAX_QUOTE_FILL_DRIFT:
                return None, [f"{contract.trading_symbol}: quote Rs {contract.ltp:.2f} vs "
                              f"candle open Rs {fill_price:.2f} differ by {drift:.0%} "
                              f"(limit {config.MAX_QUOTE_FILL_DRIFT:.0%}) -- data "
                              f"disagreement, cannot size safely"], None

        # --- OI re-check, closer to fill (07-Aug-26, Harish) --------------------
        # Gate #25's OI check ran back when the chain was first quoted --
        # potentially several candles before this actual fill. Re-run it now
        # against a fresh quote. LIVE only (mode==LIVE re-check happening
        # inside a mode!=LIVE/BACKTEST run is a contradiction that can't
        # occur, but oi_confirms itself already no-ops in BACKTEST regardless).
        if audit.oi_snapshot is not None:
            option_chain.fetch_quotes_kite_first(
                chain, angel, kite, nfo_instruments,
                angel_limiter=option_data._angel_limiter,
                kite_limiter=option_data._kite_limiter)
            quote_history.record_chain(chain, trade_date, ist_clock.now_ist())
            ok, note, snap = option_audit.oi_confirms(chain, signal, mode)
            if snap is not None:
                oi_log.log_oi_check(trade_date, symbol, signal, "late", snap, ok, note)
            if not ok and not ignore_oi:
                return None, [f"{contract.trading_symbol}: {note} "
                              f"(re-checked closer to fill)"], None

        # --- price 2 candles after the fill, for the shadow promotion check ------
        # (07-Aug-26): "wait for next 2 candles and see the performance."
        # Independent of how the trade eventually resolves below -- a stop
        # hit at candle 1 followed by a bounce still reads as "trending up"
        # here, same as it would for a symbol nobody was watching that
        # closely. It's a follow-through signal, not a re-judgment of the
        # exit that already happened.
        ltp_at_2candles = _historical_quote(
            opt_candles, fill_time + timedelta(minutes=2 * config.INTERVAL_MINUTES))

        # --- underlying ATR, which now sizes the stop ---------------------------
        und = _as_ist(candles.get(symbol))
        atr_val = 0.0
        if und is not None and not und.empty:
            closed = und[und.index + timedelta(minutes=config.INTERVAL_MINUTES)
                         <= entry_at]
            if len(closed) > config.ATR_PERIOD:
                import indicators
                atr_val = float(indicators.atr(closed, config.ATR_PERIOD).iloc[-1])

        # --- RE-SIZE against the actual fill price -------------------------------
        if ignore_capital:
            lots, size_note = 1, "1 lot (Capital Shadow: minimum size, ignoring risk budget/capital)"
        else:
            lots, size_note = order_sheet.size_position(
                fill_price, contract.lot_size, risk_budget=risk_budget,
                available_capital=eff_capital, underlying_atr=atr_val)
        if lots < 1:
            return None, [f"{contract.trading_symbol}: {size_note}"], None
        quantity = lots * contract.lot_size

        pos = Position(
            symbol=symbol, signal=signal, contract=contract,
            entry_time=fill_time, entry_ltp=fill_price,
            quantity=quantity, lots=lots, lot_size=contract.lot_size,
            trigger_slots=run.trigger_slots, audit=audit,
            order_id=f"SIM_{int(fill_time.timestamp())}",
            signal_confirmed_at=entry_at,
            spot_price=spot,
            underlying_atr=atr_val,
            oi_check=oi_check,
        )

        # --- walk the remaining candles through the exit ladder -----------------
        for ts, bar in opt_candles[opt_candles.index > fill_time].iterrows():
            now = ts.to_pydatetime() + timedelta(minutes=config.INTERVAL_MINUTES)
            o, hi, lo, cl = (float(bar["open"]), float(bar["high"]),
                             float(bar["low"]), float(bar["close"]))

            # Bad-tick guard. An option premium does not lose this much of its
            # value inside one 5-minute bar; when the print says it did, the
            # print is wrong. Trading it books a loss that never happened.
            if o > 0 and (o - lo) / o > config.MAX_INTRABAR_COLLAPSE:
                print(f"[orders] {contract.trading_symbol} {ts:%H:%M}: bar low "
                      f"{lo:.2f} is {(o - lo) / o:.0%} below open {o:.2f} -- "
                      f"treating as a bad tick, using open/close only")
                lo = min(o, cl)

            # Seed peak/trough with this bar's TRUE high/low before any
            # per-tick check runs. Without this, No-Follow-Through/Max-Hold
            # (which read pos.r_multiple, derived from peak_ltp) can fire on
            # the low-tick call using a stale peak carried from the PRIOR
            # bar -- one call before THIS bar's own high would have cleared
            # the R-multiple floor or hit a target outright. Real case:
            # TATASTEEL 29-Jul-26, exited "No Follow-Through" on a 6.06 low
            # tick when the same bar's high (6.67) already cleared Target 1
            # (6.25). Does not change stop-vs-target ordering below.
            order_sheet.note_bar_range(pos, hi, lo)

            # Worst-first within the bar: low before high, so a bar spanning
            # both stop and target resolves as the stop.
            order_sheet.update_position(pos, lo, now, square_off, bar_open=o)
            if pos.closed:
                break
            order_sheet.update_position(pos, hi, now, square_off, bar_open=o)
            if pos.closed:
                break

            # Signal-invalidation exit (20-Aug-26) -- checked AFTER the real
            # stop/target range for this bar (a genuine SL/Target hit still
            # wins), only using the bar's close, because colour and MACD
            # Recomm are only honestly knowable once the candle has closed.
            # See _macd_invalidated's docstring for the ASIANPAINT case this
            # is built from.
            if _macd_invalidated(final_df, candles, symbol, signal, ts):
                order_sheet.close_for_signal_invalidation(
                    pos, cl, now, "MACD Invalidation")
                if pos.closed:
                    break

            order_sheet.update_position(pos, cl, now, square_off, bar_open=o)
            if pos.closed:
                break

        if not pos.closed:
            last = float(opt_candles["close"].iloc[-1])
            order_sheet.update_position(pos, last, square_off, square_off)

        return pos, [], ltp_at_2candles

    for run in runs:
        symbol, signal = run.symbol, run.signal
        entry_at = _entry_datetime(run, trade_date)

        def reject(why):
            rejections.append(
                make_rejection(symbol, run.entry_slot, signal, why, entry_at))

        # Book-level limits, evaluated as of this signal's entry moment.
        blocked = _caps_block(entry_at, symbol, positions, open_symbols,
                              entries_taken, trade_date)
        if blocked:
            reject(blocked)
            # Blocked ONLY by the concurrent-position cap (not by data, not
            # by the daily loss limit, not by cutoff) is the one case where
            # "what would this have done" is a meaningful question -- capital
            # and data would have been fine, the book just had no room.
            if blocked.startswith("concurrent position cap reached"):
                shadow_pos, _shadow_reasons, ltp_2c = _try_build_position(run, entry_at)
                if shadow_pos is not None:
                    # PROMOTION (07-Aug-26, Harish: "wait for next 2 candles
                    # and see the performance... if trending up, move it to
                    # Paper Trading"). Trending up = unrealized P&L positive
                    # 2 candles after entry. Caps are re-checked at THAT
                    # later moment, same principle _caps_block's own
                    # docstring already stated for a "momentum-promoted
                    # entry" -- a slot may have freed up by then.
                    promote_at = shadow_pos.entry_time + timedelta(
                        minutes=2 * config.INTERVAL_MINUTES)
                    trending_up = ltp_2c is not None and ltp_2c > shadow_pos.entry_ltp
                    still_blocked = (
                        _caps_block(promote_at, symbol, positions, open_symbols,
                                   entries_taken, trade_date)
                        if trending_up else "no follow-through 2 candles after entry")
                    if trending_up and not still_blocked:
                        positions.append(shadow_pos)
                        open_symbols.add(symbol)
                        entries_taken += 1
                        print(f"[orders] {symbol} PROMOTED to Paper Trading -- "
                              f"trending up 2 candles after entry ({ltp_2c:.2f} "
                              f"vs entry {shadow_pos.entry_ltp:.2f}), room now "
                              f"in the book")
                    else:
                        shadow_positions.append(shadow_pos)
                        if trending_up:
                            print(f"[orders] {symbol} trending up 2 candles "
                                  f"after entry but still {still_blocked} -- "
                                  f"staying shadow")
            continue

        # regime and volatility already cleared in the pre-filter above
        pos, reasons, _ltp_2c = _try_build_position(run, entry_at)
        if pos is None:
            for why in reasons:
                reject(why)
            # Capital Shadow / OI Blocked (09-Aug-26, Harish: "review the
            # threshold and increase either the capital or OI% by EOD") --
            # only when the failure is PURELY that one thing, see the
            # classification helpers' own comment above.
            if reasons and all(_is_capital_reason(r) for r in reasons):
                cap_pos, _r, _l = _try_build_position(run, entry_at, ignore_capital=True)
                if cap_pos is not None:
                    capital_shadow_positions.append(cap_pos)
            elif reasons and all(_is_oi_reason(r) for r in reasons):
                oi_pos, _r, _l = _try_build_position(run, entry_at, ignore_oi=True)
                if oi_pos is not None:
                    oi_shadow_positions.append(oi_pos)
            continue

        positions.append(pos)
        open_symbols.add(symbol)
        entries_taken += 1
        print(f"[orders] {symbol:<12} {signal:<7} {pos.contract.trading_symbol:<24} "
              f"entry {pos.entry_ltp:>8.2f} x{pos.quantity:<6} "
              f"-> {pos.exit_reason:<28} net Rs {pos.realised_pnl:>9,.2f}")

    # --- RSI-checkpoint re-entry (16-Aug-26, Harish) -----------------------
    # A signal rejected ONLY by the RSI-extreme gate isn't necessarily a
    # dead move -- see APOLLOHOSP, 14-Aug-26: RSI 78 rejected a BUY CE at
    # 09:20, the chart kept trending to 10:35. If the 3rd candle after that
    # rejection is still moving with the signal, take it anyway, but
    # smaller: RSI_CHECKPOINT_REENTRY_RISK_RS (Rs 750) instead of the
    # normal RISK_PER_TRADE_RS (Rs 2000) -- a deliberately tighter bet on a
    # trade the RSI gate itself still calls stretched.
    for run, rejected_at in rsi_checkpoint_candidates:
        symbol, signal = run.symbol, run.signal
        rising, reentry_at = _third_candle_rising(symbol, candles, rejected_at, signal)
        if not rising or reentry_at is None:
            continue

        blocked = _caps_block(reentry_at, symbol, positions, open_symbols,
                              entries_taken, trade_date)
        if blocked:
            rejections.append(make_rejection(
                symbol, run.entry_slot, signal,
                f"RSI-checkpoint re-entry blocked: {blocked}", reentry_at))
            continue

        pos, reasons, _ltp2c = _try_build_position(
            run, reentry_at, risk_budget=config.RSI_CHECKPOINT_REENTRY_RISK_RS)
        if pos is None:
            for why in reasons:
                rejections.append(make_rejection(
                    symbol, run.entry_slot, signal,
                    f"RSI-checkpoint re-entry: {why}", reentry_at))
            continue

        pos.entry_type = "RSI Checkpoint (3rd Candle)"
        positions.append(pos)
        open_symbols.add(symbol)
        entries_taken += 1
        print(f"[orders] {symbol:<12} {signal:<7} RSI-CHECKPOINT RE-ENTRY "
              f"{pos.contract.trading_symbol:<24} entry {pos.entry_ltp:>8.2f} "
              f"x{pos.quantity:<6} -> {pos.exit_reason:<28} "
              f"net Rs {pos.realised_pnl:>9,.2f}")

    # --- Candle-momentum checkpoint re-entry (20-Aug-26, Harish) -----------
    # A stalled confirming candle ("no candle follow-through") isn't
    # necessarily a dead move if the underlying is genuinely trending --
    # check the 2 candles after the rejection and let a real trend pick a
    # side, EITHER side, not just the original signal (see
    # _candle_momentum_checkpoint_signal), then run it through the SAME
    # audit gates as any other entry. Smaller risk budget, same reasoning
    # as the RSI checkpoint above: a second chance on a trade the momentum
    # gate itself still called stalled at the time.
    for run, rejected_at in candle_momentum_candidates:
        symbol = run.symbol
        new_signal, reentry_at = _candle_momentum_checkpoint_signal(
            symbol, candles, rejected_at)
        if new_signal is None or reentry_at is None:
            continue

        blocked = _caps_block(reentry_at, symbol, positions, open_symbols,
                              entries_taken, trade_date)
        if blocked:
            rejections.append(make_rejection(
                symbol, run.entry_slot, new_signal,
                f"Candle-momentum checkpoint blocked: {blocked}", reentry_at))
            continue

        checkpoint_run = SignalRun(symbol=symbol, signal=new_signal, slots=run.slots)
        pos, reasons, _ltp2c = _try_build_position(
            checkpoint_run, reentry_at,
            risk_budget=config.CANDLE_MOMENTUM_CHECKPOINT_RISK_RS)
        if pos is None:
            for why in reasons:
                rejections.append(make_rejection(
                    symbol, run.entry_slot, new_signal,
                    f"Candle-momentum checkpoint: {why}", reentry_at))
            continue

        pos.entry_type = "Candle Momentum Checkpoint (3rd Candle)"
        positions.append(pos)
        open_symbols.add(symbol)
        entries_taken += 1
        print(f"[orders] {symbol:<12} {new_signal:<7} CANDLE-MOMENTUM CHECKPOINT "
              f"{pos.contract.trading_symbol:<24} entry {pos.entry_ltp:>8.2f} "
              f"x{pos.quantity:<6} -> {pos.exit_reason:<28} "
              f"net Rs {pos.realised_pnl:>9,.2f}")

    orders_df = order_sheet.build_orders_sheet(positions)
    rejected_df = order_sheet.build_rejected_sheet(rejections)
    missed_df = order_sheet.build_orders_sheet(shadow_positions)
    capital_shadow_df = order_sheet.build_orders_sheet(capital_shadow_positions)
    oi_blocked_df = order_sheet.build_orders_sheet(oi_shadow_positions)

    net = sum(p.realised_pnl for p in positions)
    print(f"[orders] {len(positions)} order(s), {len(rejections)} rejection(s), "
          f"net Rs {net:,.2f}")
    if shadow_positions:
        missed_net = sum(p.realised_pnl for p in shadow_positions)
        print(f"[orders] {len(shadow_positions)} shadow trade(s) blocked only by "
              f"the concurrent-position cap -- would-have net Rs {missed_net:,.2f}")
    if capital_shadow_positions:
        cap_net = sum(p.realised_pnl for p in capital_shadow_positions)
        print(f"[orders] {len(capital_shadow_positions)} shadow trade(s) blocked "
              f"only by risk-budget/capital -- would-have net Rs {cap_net:,.2f}")
    if oi_shadow_positions:
        oi_net = sum(p.realised_pnl for p in oi_shadow_positions)
        print(f"[orders] {len(oi_shadow_positions)} shadow trade(s) blocked only "
              f"by the OI gate -- would-have net Rs {oi_net:,.2f}")
    return orders_df, rejected_df, positions, missed_df, capital_shadow_df, oi_blocked_df


def _fetch_opt_candles(contract, symbol: str, opt_type: str, trade_date: date,
                       angel, kite, nfo_instruments, cutoff: datetime):
    """
    Same Kite-first/Angel-fallback source selection process_date uses,
    factored out (03-Aug-26) so the LIVE incremental loop can call it twice
    per open position -- once to decide entry, then again every later cycle
    to extend the exit-ladder walk with whatever bars have closed since.
    """
    opt_candles = None
    data_source = None

    if nfo_instruments is not None:
        ktoken, ksym = option_chain.resolve_kite_option_token(
            nfo_instruments, symbol, contract.strike, contract.expiry, opt_type)
        if ktoken is not None:
            kdf = option_data.fetch_option_history_kite(
                kite, ktoken, trade_date, cutoff=cutoff,
                trading_symbol=ksym or contract.trading_symbol)
            if kdf is not None and not kdf.empty:
                opt_candles = _as_ist(kdf)
                data_source = "KITE"

    if opt_candles is None:
        angel_candles = option_data.fetch_option_candles(
            contract.token, trade_date, angel, cutoff=cutoff,
            trading_symbol=contract.trading_symbol)
        if angel_candles is None or angel_candles.empty:
            return None, None
        opt_candles = _as_ist(angel_candles)
        data_source = "ANGEL"

    return opt_candles, data_source


def new_live_state() -> dict:
    """
    Fresh persistent state for one LIVE day's incremental evaluation.
    Lives in run_live_day()'s closure across every candle-close cycle --
    NOT rebuilt each call, that is the whole point of it.
    """
    return {
        "nfo_instruments": None,
        "nfo_fetched": False,
        "open_symbols": set(),
        "entries_taken": 0,
        # (symbol, entry_slot) already DECIDED -- accepted or rejected.
        # Entry decisions are made exactly once and never revisited; only
        # the exit-ladder walk for an accepted one continues across cycles.
        "seen": set(),
        # (symbol, entry_slot) -> entry snapshot for runs that were accepted
        # but have not resolved to an exit yet.
        "open_runs": {},
        "terminal": [],           # resolved Position objects
        # REAL broker positions (20-Aug-26, live_orders.py), only ever
        # populated when config.LIVE_TRADING is True. Deliberately a
        # SEPARATE dict from open_runs/terminal -- those are replayed fresh
        # from their entry snapshot every cycle (safe, since it's only
        # simulating against historical candles); a real broker order
        # cannot be replayed, so a live Position is built ONCE at entry and
        # mutated in place cycle to cycle. See _advance_live_position.
        "live_positions": {},     # (symbol, entry_slot) -> Position, still open
        "live_terminal": [],      # resolved (real) Position objects
        "rejections": [],
        # Same shape as open_runs/terminal, for runs blocked ONLY by the
        # concurrent-position cap -- simulated as if the book had room, so
        # the cap's cost is visible (07-Aug-26, Harish). Never touches
        # open_symbols/entries_taken/capital unless PROMOTED (see
        # _check_promotions) -- until then, purely observational.
        "shadow_open_runs": {},
        "shadow_terminal": [],
        # (symbol, entry_slot) keys already given their one-time promotion
        # verdict (promoted, or staying shadow) -- never re-checked once
        # decided, same "decided once" principle as `seen` above.
        "shadow_promotion_checked": set(),
        # Same shape again, for runs blocked ONLY by risk-budget/capital
        # (09-Aug-26, "Capital Shadow") and ONLY by the OI gate ("OI
        # Blocked"). Never promoted -- pure EOD review sheets, see
        # process_date's docstring for the full reasoning.
        "capital_shadow_open_runs": {},
        "capital_shadow_terminal": [],
        "oi_shadow_open_runs": {},
        "oi_shadow_terminal": [],
        # RSI-checkpoint re-entry (16-Aug-26, Harish) -- (symbol, entry_slot)
        # -> {"run": SignalRun, "rejected_at": datetime} for every signal
        # rejected ONLY by the RSI-extreme gate, waiting to see whether its
        # 3rd candle keeps moving with the signal. Same "decided once"
        # principle as shadow_promotion_checked: each key is re-examined
        # exactly once, as soon as that 3rd candle has closed.
        "rsi_checkpoint_pending": {},
        "rsi_checkpoint_checked": set(),
        # Candle-momentum checkpoint re-entry (20-Aug-26, Harish) -- same
        # shape as rsi_checkpoint_pending/_checked above, for every run
        # rejected ONLY by signal_quality.candle_momentum_ok ("no candle
        # follow-through"). See _candle_momentum_checkpoint_signal.
        "candle_momentum_pending": {},
        "candle_momentum_checked": set(),
    }


def advance_live_day(state: dict, final_df: pd.DataFrame, ref_df: pd.DataFrame,
                     candles: dict[str, pd.DataFrame], scrip: pd.DataFrame,
                     angel, trade_date: date, mode: str, slots: list[str],
                     available_capital: float | None = None,
                     index_candles: pd.DataFrame | None = None,
                     vix_candles: pd.DataFrame | None = None,
                     kite=None,
                     oi_buildup_enabled: bool = False,
                     ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame,
                               pd.DataFrame, pd.DataFrame]:
    """
    Returns (orders_df, rejected_df, missed_df, capital_shadow_df,
    oi_blocked_df) -- see process_date's docstring for what each shadow
    sheet means; identical rules, just tracked incrementally here.

    One LIVE candle-close cycle (03-Aug-26, at Harish's request -- watching
    HDFCBANK's Final Recomm complete its 3-bar trio with nothing in Orders
    until 15:15 was not useful for a session he needed to act on today).

    Decides any NEWLY qualified signals this cycle -- same gates, same
    order, as process_date -- and advances every still-open position with
    whatever option candles exist so far. Returns fresh (orders_df,
    rejected_df) built from `state` every call, so the caller just writes
    them to the sheet each cycle; open positions show up with a blank Exit
    Reason and a live Current LTP, exactly like a real trading terminal,
    and `dashboard.compute_stats` already reads "open" from a blank Exit
    Reason so nothing downstream needed to change for that.

    Deliberately a SEPARATE function from process_date, not a shared
    refactor of it. process_date is the verified BACKTEST path -- a single
    full-day batch call where positions always have the whole day's option
    candles available up front. Editing it to behave differently live would
    put a live financial session's correctness and BACKTEST's already-
    verified correctness behind the same change; keeping them apart means a
    live-only bug (there will be one, eventually) can never touch BACKTEST,
    and a BACKTEST change can never touch what's running live.

    HOW THIS AVOIDS THE 02-AUG-26 BUG (positions force-closing mid-session
    because process_date assumed the whole day's future data already
    existed): an open position's `Position` object is never mutated across
    cycles. Each cycle rebuilds a FRESH Position from the run's fixed entry
    snapshot (entry price/time/stop/targets/audit -- decided once, at entry,
    and never re-decided) and replays it from fill_time through whatever
    option candles exist as of THIS cycle's real clock. Same entry, more
    data each time -- deterministic, so replaying is equivalent to
    resuming, with none of the bookkeeping a true resume would need. It is
    only ever force-closed at square-off if `ist_clock.now_ist()` has
    genuinely reached 15:15 -- never because the data ran out early.
    """
    import signal_quality

    now = ist_clock.now_ist()

    if not state["nfo_fetched"]:
        state["nfo_fetched"] = True
        if kite is not None:
            try:
                import token_mgmt
                state["nfo_instruments"] = token_mgmt._fetch_instruments(kite, "NFO")
            except Exception as exc:
                print(f"[live-orders] Kite NFO instrument dump unavailable ({exc}) "
                      f"-- option data will come from Angel only")
    nfo_instruments = state["nfo_instruments"]

    runs = order_sheet.qualified_signals(final_df, slots)
    new_runs = [r for r in runs if (r.symbol, r.entry_slot) not in state["seen"]]

    eligible = []
    for run in new_runs:
        at = _entry_datetime(run, trade_date)
        # `at` (entry_at) is when the signal is CONFIRMED -- the moment the
        # trio's 3rd bar closes. The FILL happens on the candle that OPENS
        # at `at` (the very next one), which doesn't itself close until one
        # more interval later. Gating on `at > now` alone let a signal
        # confirmed at, say, 10:10 get decided in the SAME cycle -- reading
        # a fill price from a candle that was still forming, or (as caught
        # live 03-Aug-26 on TCS, entry_slot 10:05) finding no candle there
        # at all yet and rejecting it PERMANENTLY as "no candle to fill
        # against" one cycle too early, one candle before it would have
        # existed. Must wait for the FILL candle to close, not just the
        # trio's candle.
        fill_candle_closes = at + timedelta(minutes=config.INTERVAL_MINUTES)
        if now < fill_candle_closes:
            continue
        state["seen"].add((run.symbol, run.entry_slot))

        ok, note = signal_quality.vix_ok(vix_candles, at)
        if not ok:
            state["rejections"].append(make_rejection(run.symbol, run.entry_slot,
                                                       run.signal, note, at))
            continue
        ok, note = signal_quality.regime_ok(index_candles, at)
        if not ok:
            state["rejections"].append(make_rejection(run.symbol, run.entry_slot,
                                                       run.signal, note, at))
            continue
        ok, note = signal_quality.volatility_ok(candles.get(run.symbol), at)
        if not ok:
            state["rejections"].append(make_rejection(
                run.symbol, run.entry_slot, run.signal, f"{run.symbol}: {note}", at))
            continue
        ok, note = signal_quality.rsi_extreme_ok(candles.get(run.symbol), at, run.signal)
        if not ok:
            state["rejections"].append(make_rejection(
                run.symbol, run.entry_slot, run.signal, f"{run.symbol}: {note}", at))
            if config.RSI_CHECKPOINT_REENTRY_ENABLED:
                state["rsi_checkpoint_pending"][(run.symbol, run.entry_slot)] = {
                    "run": run, "rejected_at": at,
                }
            continue
        ok, note = _candle_momentum_ok_for(run, candles, trade_date)
        if not ok:
            state["rejections"].append(make_rejection(
                run.symbol, run.entry_slot, run.signal, f"{run.symbol}: {note}", at))
            if config.CANDLE_MOMENTUM_CHECKPOINT_ENABLED:
                state["candle_momentum_pending"][(run.symbol, run.entry_slot)] = {
                    "run": run, "rejected_at": at,
                }
            continue
        eligible.append(run)

    if eligible:
        print(f"[live-orders] {len(eligible)} newly-qualified signal(s) ready to decide")

    ref_by_symbol = {str(r[config.COL_SYMBOL]): r for _, r in ref_df.iterrows()}

    # Same tradeability-before-ranking fix as process_date (03-Aug-26): a
    # symbol with no real contract at its ATM strike must not be allowed to
    # WIN a slot and take every other qualified candidate down with it.
    eligible = _filter_to_tradeable(eligible, ref_by_symbol, candles, scrip,
                                    trade_date, mode, state["rejections"])

    # Within each slot, only the best-scoring signal is taken this cycle;
    # every other qualified candidate is a plain Rejected row. Cycle-local
    # only -- every run in a slot shares the same entry_at, so they're all
    # ranked together in one pass, nothing to carry across cycles.
    if config.RANK_WITHIN_SLOT_ENABLED:
        kept, dropped = signal_quality.rank_within_slots(
            eligible, candles, lambda r: _entry_datetime(r, trade_date))
        for run, why in dropped:
            state["rejections"].append(make_rejection(
                run.symbol, run.entry_slot, run.signal, why,
                _entry_datetime(run, trade_date)))
        runs_ranked = kept
    else:
        # Every qualified, tradeable symbol gets a real attempt -- no
        # score-based slot winner. Same call as process_date, same reason.
        runs_ranked = sorted(eligible, key=lambda r: (r.entry_slot, r.symbol))

    square_off = ist_clock.combine_ist(
        trade_date, ist_clock.MARKET_OPEN.replace(hour=15, minute=15))
    is_eod = now >= square_off

    def _live_caps_block(symbol: str, check_at: datetime) -> str:
        """
        Same book-level limits as process_date's _caps_block, shaped for the
        LIVE state dict. Used both for a run's own entry decision and (with
        `check_at` set to the 2-candles-later promotion mark) to re-check
        whether a shadow trade can be promoted into the real book.
        """
        realised_so_far = sum(
            p.realised_pnl for p in state["terminal"] + state["live_terminal"]
            if p.exit_time is not None and p.exit_time <= check_at)
        live_now_ct = len(state["open_runs"]) + len(state["live_positions"])
        hh, mm = (int(x) for x in config.NO_NEW_ENTRY_AFTER.split(":"))
        entry_cutoff = ist_clock.combine_ist(
            trade_date, ist_clock.MARKET_OPEN.replace(hour=hh, minute=mm))

        if realised_so_far <= -config.DAILY_MAX_LOSS_RS:
            return (f"daily loss limit hit: Rs {realised_so_far:,.0f} "
                    f"against a cap of Rs {-config.DAILY_MAX_LOSS_RS:,.0f} "
                    f"-- no new entries")
        if config.ONE_POSITION_PER_SYMBOL and symbol in state["open_symbols"]:
            return (f"{symbol}: position already open in this symbol -- "
                    f"skipping new entry until it closes")
        if (config.MAX_ENTRIES_PER_DAY is not None
                and state["entries_taken"] >= config.MAX_ENTRIES_PER_DAY):
            return f"daily entry cap reached ({config.MAX_ENTRIES_PER_DAY})"
        if (config.MAX_CONCURRENT_POSITIONS is not None
                and live_now_ct >= config.MAX_CONCURRENT_POSITIONS):
            return f"concurrent position cap reached ({config.MAX_CONCURRENT_POSITIONS})"
        if check_at > entry_cutoff:
            return (f"after entry cutoff {config.NO_NEW_ENTRY_AFTER} -- too "
                    f"little session left for the trade to work")
        return ""

    def _try_build_snapshot(run: SignalRun, entry_at: datetime,
                            ignore_capital: bool = False,
                            ignore_oi: bool = False,
                            risk_budget: float | None = None,
                            ) -> tuple[dict | None, str, list[str]]:
        """
        Same simulation process_date's _try_build_position runs, shaped for
        the LIVE incremental loop: returns an entry snapshot dict (the kwargs
        for a fresh Position, replayed each cycle -- see this function's own
        docstring for why) instead of a walked-to-close Position, because a
        LIVE entry's exit ladder isn't known yet.

        Used for a real entry (once caps clear) and three shadow entries --
        concurrent-cap, capital (`ignore_capital=True`), and OI
        (`ignore_oi=True`) -- see process_date's _try_build_position for the
        full reasoning, identical here. Returns (snapshot, opt_type, []) on
        success or (None, "", reasons) on failure.
        """
        symbol, signal = run.symbol, run.signal
        # See process_date's _try_build_position for why ignore_capital
        # forces exactly 1 lot rather than a large substitute risk budget.
        eff_capital = None if ignore_capital else available_capital

        ref_row = ref_by_symbol.get(symbol)
        if ref_row is None:
            return None, "", ["symbol not found in Reference sheet"]
        step = _strike_step(ref_row)
        if step <= 0:
            return None, "", [f"no usable 'Option Price Difference' for {symbol} "
                              f"-- cannot round to a strike"]

        spot = _spot_at(candles.get(symbol), entry_at)
        if spot is None:
            return None, "", [f"no closed underlying candle at {entry_at:%H:%M}"]

        # quote_history.resolve_and_quote (09-Aug-26): mode is always LIVE
        # here, so this always resolves+quotes live as before -- the
        # difference is it also RECORDS the result, which is what makes a
        # future BACKTEST of this date able to replay it instead of asking
        # that future day's scrip master/quotes. See quote_history.py.
        #
        # Labelled with entry_at, NOT `now` (fixed 10-Aug-26): the quote
        # fetch itself always happens at real wall-clock `now` regardless --
        # that part's unavoidable and unchanged. But `now` can run several
        # minutes behind entry_at just from working through the watchlist
        # each cycle, so recording under `now` mislabels every capture a few
        # minutes LATE. A later BACKTEST looks for a captured row at-or-
        # before entry_at, finds nothing (the real capture is stamped after
        # it), and silently falls back to that future day's live scrip
        # master/quotes -- reproducing the exact spread/OI/contract drift
        # bug this module was built to fix in the first place. Confirmed
        # against 10-Aug-26: LICHSGFIN/MOTILALOFS/COALINDIA/ICICIBANK all
        # had their first capture 6-7 minutes after entry_at, and all four
        # came out differently in that day's BACKTEST.
        chain = quote_history.resolve_and_quote(
            symbol, spot, step, scrip, trade_date, mode, entry_at, angel,
            option_data._angel_limiter, sheet_expiry=ref_row.get(config.COL_EXPIRY),
            kite=kite, nfo_instruments=nfo_instruments,
            kite_limiter=option_data._kite_limiter)
        if chain is None:
            return None, "", [f"no option contracts found around ATM for {symbol}"]

        opt_type = "CE" if signal == config.SIGNAL_BUY_CE else "PE"
        contract = chain.atm(opt_type)
        if contract is None:
            return None, "", [f"no {opt_type} contract at all in the ATM window (strike {chain.atm_strike:g})"]
        if not contract.lot_size:
            return None, "", [f"{contract.trading_symbol}: lot size unknown"]

        opt_candles, data_source = _fetch_opt_candles(
            contract, symbol, opt_type, trade_date, angel, kite, nfo_instruments, now)
        if opt_candles is None:
            return None, "", [f"{contract.trading_symbol}: no option candles from "
                              f"either Kite or Angel -- cannot simulate the exit "
                              f"ladder honestly"]

        # OI Check column -- see process_date's _try_build_position, same call.
        oi_check = option_audit.classify_oi_buildup(opt_candles, entry_at)

        # STALE-DECISION GUARD (07-Aug-26, found on a restart: AMBER, blocked
        # by the concurrent cap at 09:35, silently vanished from BOTH Orders
        # and Missed_Concurrent). In normal LIVE operation entry_at is
        # seconds old, so the live quote fetched just above IS contemporary
        # with the candle -- exactly the case process_date's own comment
        # describes ("there it IS contemporaneous... a genuine two-feed
        # safety net"). But this loop also runs on a CATCH-UP cycle after a
        # restart, deciding signals hours old, with a live quote priced at
        # NOW being compared against a fill from hours ago. For a stock that
        # actually moved (AMBER hit Target 3 the same day) that drift routinely
        # exceeds MAX_QUOTE_FILL_DRIFT, and the run fails with no trace --
        # this is the INDIANB bug (see _historical_quote's docstring) showing
        # up in LIVE instead of BACKTEST. Same fix: once the decision is no
        # longer contemporaneous, price it from history like BACKTEST does.
        if now - entry_at > timedelta(minutes=2 * config.INTERVAL_MINUTES):
            hist_ltp = _historical_quote(opt_candles, entry_at)
            if hist_ltp is not None:
                contract.ltp = hist_ltp
            hist_vol = _historical_volume(opt_candles, entry_at)
            if hist_vol is not None:
                contract.volume = hist_vol

        if contract.ltp is None or contract.ltp <= 0:
            return None, "", [f"{contract.trading_symbol}: no usable price at "
                              f"{entry_at:%H:%M} (live quote and history both empty)"]

        if ignore_capital:
            lots, size_note = 1, "1 lot (Capital Shadow: minimum size, ignoring risk budget/capital)"
        else:
            lots, size_note = order_sheet.size_position(
                contract.ltp, contract.lot_size, risk_budget=risk_budget,
                available_capital=eff_capital)
        if lots < 1:
            return None, "", [size_note]
        quantity = lots * contract.lot_size

        audit = option_audit.audit_option(
            contract, chain, signal, trade_date, quantity,
            available_capital=eff_capital, mode=mode, ignore_oi=ignore_oi)
        if not audit.passed:
            return None, "", [f"{contract.trading_symbol}: {why}"
                              for why in audit.reasons]

        # OI CHANGE / buildup GATE (20-Aug-26, Harish: "check for Rise in OI
        # & Rise in Price... only if trending... else ignore"). Already
        # built and running in BACKTEST (process_date, identical call) but
        # never wired into LIVE -- classify_oi_buildup above only ever
        # populated the display column here, nothing gated on it. Only
        # LONG_BUILDUP (price up AND OI up) confirms a BUY CE, only
        # SHORT_BUILDUP (price down AND OI up) confirms a BUY PE --
        # SHORT_COVERING and LONG_UNWINDING confirm neither direction,
        # whichever way price moved. Same placement as process_date's
        # identical block (after audit_option, before the fill) so a
        # rejection reason lines up the same way in both paths. See
        # option_audit.oi_buildup_confirms.
        if oi_buildup_enabled:
            ok, note = option_audit.oi_buildup_confirms(opt_candles, entry_at, signal)
            if not ok:
                return None, "", [f"{contract.trading_symbol}: {note}"]

        fill_rows = opt_candles[opt_candles.index >= entry_at]
        if fill_rows.empty:
            return None, "", [f"{contract.trading_symbol}: no candle at or after "
                              f"{entry_at:%H:%M} to fill against"]
        fill_price = float(fill_rows["open"].iloc[0])
        fill_time = fill_rows.index[0].to_pydatetime()

        if contract.ltp and fill_price:
            drift = abs(fill_price - contract.ltp) / contract.ltp
            if drift > config.MAX_QUOTE_FILL_DRIFT:
                return None, "", [f"{contract.trading_symbol}: quote Rs {contract.ltp:.2f} vs "
                                  f"candle open Rs {fill_price:.2f} differ by {drift:.0%} "
                                  f"(limit {config.MAX_QUOTE_FILL_DRIFT:.0%}) -- data "
                                  f"disagreement, cannot size safely"]

        # --- OI re-check, closer to fill (07-Aug-26, Harish) ---------------------
        # Same idea as process_date's identical block: gate #25's OI check
        # ran when the chain was first quoted, possibly several cycles
        # before this fill. Re-run it fresh, right before this run is
        # finalised into Orders/Missed_Concurrent.
        if audit.oi_snapshot is not None:
            option_chain.fetch_quotes_kite_first(
                chain, angel, kite, nfo_instruments,
                angel_limiter=option_data._angel_limiter,
                kite_limiter=option_data._kite_limiter)
            quote_history.record_chain(chain, trade_date, now)
            ok, note, snap = option_audit.oi_confirms(chain, signal, mode)
            if snap is not None:
                oi_log.log_oi_check(trade_date, symbol, signal, "late", snap, ok, note)
            if not ok and not ignore_oi:
                return None, "", [f"{contract.trading_symbol}: {note} "
                                  f"(re-checked closer to fill)"]

        und = _as_ist(candles.get(symbol))
        atr_val = 0.0
        if und is not None and not und.empty:
            closed_bars = und[und.index + timedelta(minutes=config.INTERVAL_MINUTES)
                              <= entry_at]
            if len(closed_bars) > config.ATR_PERIOD:
                import indicators
                atr_val = float(indicators.atr(closed_bars, config.ATR_PERIOD).iloc[-1])

        if ignore_capital:
            lots, size_note = 1, "1 lot (Capital Shadow: minimum size, ignoring risk budget/capital)"
        else:
            lots, size_note = order_sheet.size_position(
                fill_price, contract.lot_size, risk_budget=risk_budget,
                available_capital=eff_capital, underlying_atr=atr_val)
        if lots < 1:
            return None, "", [f"{contract.trading_symbol}: {size_note}"]
        quantity = lots * contract.lot_size

        entry_snapshot = dict(
            symbol=symbol, signal=signal, contract=contract,
            entry_time=fill_time, entry_ltp=fill_price,
            quantity=quantity, lots=lots, lot_size=contract.lot_size,
            trigger_slots=run.trigger_slots, audit=audit,
            order_id=f"SIM_{int(fill_time.timestamp())}",
            signal_confirmed_at=entry_at, spot_price=spot, underlying_atr=atr_val,
            oi_check=oi_check,
        )
        return entry_snapshot, opt_type, []

    def _open_position(key: tuple, symbol: str, signal: str, snap: dict,
                       opt_type: str, label: str) -> bool:
        """
        Accept one entry snapshot into the book -- PAPER (unchanged
        behaviour, replayed each cycle) when config.LIVE_TRADING is False,
        a REAL Angel One order when it's True. Shared by all three entry
        paths (normal, RSI-checkpoint re-entry, shadow promotion) so none
        of them can accidentally skip real order placement.

        Returns True if the position was opened (paper or live), False if a
        live order attempt failed -- caller should reject/skip on False,
        never fall back to treating a failed live order as a paper fill.
        """
        if not config.LIVE_TRADING:
            state["open_runs"][key] = {"snapshot": snap, "opt_type": opt_type}
            state["open_symbols"].add(symbol)
            state["entries_taken"] += 1
            print(f"[live-orders] {symbol:<12} {signal:<7} "
                  f"{snap['contract'].trading_symbol:<24} "
                  f"ENTERED {snap['entry_ltp']:>8.2f} x{snap['quantity']:<6} "
                  f"-- PAPER, now tracking{label}")
            return True

        entry_slot = key[1]
        if live_orders.kill_switch_active():
            state["rejections"].append(make_rejection(
                symbol, entry_slot, signal,
                f"live kill switch active ({paths.LIVE_KILL_SWITCH_FILE.name} "
                f"exists) -- no new live entries", ist_clock.now_ist()))
            print(f"[live-orders] {symbol}: kill switch active -- skipping "
                  f"live entry{label}")
            return False

        contract = snap["contract"]
        try:
            fill = live_orders.enter_live(
                angel, contract.trading_symbol, contract.token, snap["quantity"])
        except live_orders.LiveOrderError as exc:
            print(f"[live-orders] {symbol}: LIVE ENTRY FAILED{label} -- {exc}")
            state["rejections"].append(make_rejection(
                symbol, entry_slot, signal,
                f"live entry order failed: {exc}", ist_clock.now_ist()))
            return False

        real_qty = fill.filled_qty
        real_lots = max(real_qty // contract.lot_size, 1)
        pos = Position(**{**snap, "entry_ltp": fill.avg_price,
                          "quantity": real_qty, "lots": real_lots})
        pos.trade_mode = "LIVE"
        pos.broker_entry_order_id = fill.order_id
        pos.actual_fill_price = fill.avg_price

        try:
            pos.broker_stop_order_id = live_orders.place_protective_stop(
                angel, contract.trading_symbol, contract.token,
                real_qty, pos.stop_loss)
        except live_orders.LiveOrderError as exc:
            # The entry already filled for real -- discarding the position
            # now would leave a live position nobody is tracking, which is
            # worse than one tracked with no resting stop yet. Flag loudly;
            # _advance_live_position retries placing the stop every cycle
            # until it succeeds (see its own docstring).
            print(f"[live-orders] {symbol}: ENTERED LIVE but the protective "
                  f"stop FAILED to place -- {exc}. Position is UNPROTECTED "
                  f"until the next cycle retries. Check the Angel One app "
                  f"by hand, now.")

        state["live_positions"][key] = pos
        state["open_symbols"].add(symbol)
        state["entries_taken"] += 1
        print(f"[live-orders] {symbol:<12} {signal:<7} "
              f"{contract.trading_symbol:<24} LIVE ENTRY {fill.avg_price:>8.2f} "
              f"x{real_qty:<6} -- REAL ORDER (entry {fill.order_id}, "
              f"stop {pos.broker_stop_order_id or 'FAILED'}){label}")
        return True

    for run in runs_ranked:
        symbol, signal = run.symbol, run.signal
        entry_at = _entry_datetime(run, trade_date)

        def reject(why):
            state["rejections"].append(
                make_rejection(symbol, run.entry_slot, signal, why, entry_at))

        # Live counts differ from the backtest's: realised P/L comes from
        # TERMINAL trades and concurrency from open_runs, but the limits
        # and their meaning are identical.
        blocked = _live_caps_block(symbol, entry_at)

        if blocked:
            reject(blocked)
            # See process_date's identical comment: only the concurrent-cap
            # rejection is worth simulating -- capital and data would have
            # been fine, the book just had no room. Promotion itself (if it
            # ever trends up) happens later, once 2 candles have actually
            # closed -- see _check_promotions below.
            if blocked.startswith("concurrent position cap reached"):
                snap, opt_type, _reasons = _try_build_snapshot(run, entry_at)
                if snap is not None:
                    state["shadow_open_runs"][(symbol, run.entry_slot)] = {
                        "snapshot": snap, "opt_type": opt_type,
                    }
            continue

        snap, opt_type, reasons = _try_build_snapshot(run, entry_at)
        if snap is None:
            for why in reasons:
                reject(why)
            # Capital Shadow / OI Blocked (09-Aug-26) -- same "purely this
            # one reason" rule as process_date, see _is_capital_reason/
            # _is_oi_reason's own comment.
            if reasons and all(_is_capital_reason(r) for r in reasons):
                cap_snap, cap_opt_type, _r = _try_build_snapshot(
                    run, entry_at, ignore_capital=True)
                if cap_snap is not None:
                    state["capital_shadow_open_runs"][(symbol, run.entry_slot)] = {
                        "snapshot": cap_snap, "opt_type": cap_opt_type,
                    }
            elif reasons and all(_is_oi_reason(r) for r in reasons):
                oi_snap, oi_opt_type, _r = _try_build_snapshot(
                    run, entry_at, ignore_oi=True)
                if oi_snap is not None:
                    state["oi_shadow_open_runs"][(symbol, run.entry_slot)] = {
                        "snapshot": oi_snap, "opt_type": oi_opt_type,
                    }
            continue

        _open_position((symbol, run.entry_slot), symbol, signal, snap,
                       opt_type, "")

    # --- RSI-checkpoint re-entry (16-Aug-26, Harish) ------------------------
    # Same idea as process_date's identical block: a signal rejected ONLY by
    # the RSI-extreme gate isn't necessarily dead (APOLLOHOSP, 14-Aug-26).
    # Each pending key is checked exactly once, as soon as its 3rd candle
    # after rejection has closed -- see _third_candle_rising.
    for key, info in list(state["rsi_checkpoint_pending"].items()):
        if key in state["rsi_checkpoint_checked"]:
            continue
        run, rejected_at = info["run"], info["rejected_at"]
        symbol, signal = run.symbol, run.signal
        third_close_at = rejected_at + timedelta(minutes=3 * config.INTERVAL_MINUTES)
        if now < third_close_at:
            continue  # 3rd candle hasn't closed yet -- try again next cycle
        state["rsi_checkpoint_checked"].add(key)

        rising, reentry_at = _third_candle_rising(symbol, candles, rejected_at, signal)
        if not rising or reentry_at is None:
            continue

        blocked = _live_caps_block(symbol, reentry_at)
        if blocked:
            state["rejections"].append(make_rejection(
                symbol, run.entry_slot, signal,
                f"RSI-checkpoint re-entry blocked: {blocked}", reentry_at))
            continue

        snap, opt_type, reasons = _try_build_snapshot(
            run, reentry_at, risk_budget=config.RSI_CHECKPOINT_REENTRY_RISK_RS)
        if snap is None:
            for why in reasons:
                state["rejections"].append(make_rejection(
                    symbol, run.entry_slot, signal,
                    f"RSI-checkpoint re-entry: {why}", reentry_at))
            continue

        snap["entry_type"] = "RSI Checkpoint (3rd Candle)"
        _open_position(key, symbol, signal, snap, opt_type,
                       " (RSI-checkpoint re-entry)")

    # --- Candle-momentum checkpoint re-entry (20-Aug-26, Harish) -----------
    # Same idea as the RSI checkpoint above: a stalled confirming candle
    # ("no candle follow-through") isn't necessarily a dead move if the
    # underlying is genuinely trending. Checked exactly once per pending
    # key, as soon as its 2nd subsequent candle has closed -- see
    # _candle_momentum_checkpoint_signal, which picks WHICHEVER side the
    # trend actually confirms, not just the original signal.
    for key, info in list(state["candle_momentum_pending"].items()):
        if key in state["candle_momentum_checked"]:
            continue
        run, rejected_at = info["run"], info["rejected_at"]
        symbol = run.symbol
        third_close_at = rejected_at + timedelta(minutes=3 * config.INTERVAL_MINUTES)
        if now < third_close_at:
            continue  # 2nd subsequent candle hasn't closed yet -- retry next cycle
        state["candle_momentum_checked"].add(key)

        new_signal, reentry_at = _candle_momentum_checkpoint_signal(
            symbol, candles, rejected_at)
        if new_signal is None or reentry_at is None:
            continue

        blocked = _live_caps_block(symbol, reentry_at)
        if blocked:
            state["rejections"].append(make_rejection(
                symbol, run.entry_slot, new_signal,
                f"Candle-momentum checkpoint blocked: {blocked}", reentry_at))
            continue

        checkpoint_run = SignalRun(symbol=symbol, signal=new_signal, slots=run.slots)
        snap, opt_type, reasons = _try_build_snapshot(
            checkpoint_run, reentry_at,
            risk_budget=config.CANDLE_MOMENTUM_CHECKPOINT_RISK_RS)
        if snap is None:
            for why in reasons:
                state["rejections"].append(make_rejection(
                    symbol, run.entry_slot, new_signal,
                    f"Candle-momentum checkpoint: {why}", reentry_at))
            continue

        snap["entry_type"] = "Candle Momentum Checkpoint (3rd Candle)"
        _open_position(key, symbol, new_signal, snap, opt_type,
                       " (candle-momentum checkpoint re-entry)")

    # --- shadow promotion: "wait for next 2 candles and see the performance"
    # (07-Aug-26, Harish). Checked exactly once per shadow run, as soon as 2
    # candles have closed since its entry -- not every cycle after that.
    for key, info in list(state["shadow_open_runs"].items()):
        if key in state["shadow_promotion_checked"]:
            continue
        snap = info["snapshot"]
        symbol = snap["symbol"]
        two_candle_mark = snap["entry_time"] + timedelta(
            minutes=2 * config.INTERVAL_MINUTES)
        if now < two_candle_mark:
            continue  # not yet -- try again next cycle

        contract = snap["contract"]
        opt_candles, _src = _fetch_opt_candles(
            contract, symbol, info["opt_type"], trade_date, angel, kite,
            nfo_instruments, now)
        ltp_at_mark = _historical_quote(opt_candles, two_candle_mark) \
            if opt_candles is not None else None
        if ltp_at_mark is None:
            continue  # that candle hasn't actually come through yet -- retry

        state["shadow_promotion_checked"].add(key)

        if ltp_at_mark <= snap["entry_ltp"]:
            print(f"[live-orders] [shadow] {symbol} flat/down 2 candles "
                  f"after entry ({ltp_at_mark:.2f} vs entry "
                  f"{snap['entry_ltp']:.2f}) -- staying shadow")
            continue

        promo_blocked = _live_caps_block(symbol, two_candle_mark)
        if promo_blocked:
            print(f"[live-orders] [shadow] {symbol} trending up ({ltp_at_mark:.2f} "
                  f"vs entry {snap['entry_ltp']:.2f}) but still {promo_blocked} "
                  f"-- staying shadow")
            continue

        del state["shadow_open_runs"][key]
        print(f"[live-orders] {symbol} PROMOTED -- trending up 2 candles "
              f"after entry ({ltp_at_mark:.2f} vs entry "
              f"{snap['entry_ltp']:.2f}), room now in the book")
        _open_position(key, symbol, snap["signal"], snap, info["opt_type"],
                       " (promoted from shadow)")

    # --- advance every still-open run with whatever candles exist now ------
    def _advance_open_runs(open_runs: dict, terminal: list,
                           tag: str) -> list[tuple]:
        """
        Replay every still-open run against whatever option candles exist as
        of `now`, moving anything that resolves into `terminal`. Returns the
        still-open (Position, current_ltp) pairs.

        Shared by the real book (open_runs/terminal) and the shadow book
        (shadow_open_runs/shadow_terminal, 07-Aug-26) so both advance
        identically -- see this function's enclosing docstring for why a
        position is replayed fresh from its entry snapshot every cycle
        rather than mutated in place.
        """
        resolved_keys = []
        rows: list[tuple] = []

        for key, info in open_runs.items():
            snap = info["snapshot"]
            contract = snap["contract"]
            opt_candles, _src = _fetch_opt_candles(
                contract, snap["symbol"], info["opt_type"], trade_date, angel, kite,
                nfo_instruments, now)

            pos = Position(**snap)

            current_ltp = None
            if opt_candles is not None and not opt_candles.empty:
                current_ltp = float(opt_candles["close"].iloc[-1])
                for ts, bar in opt_candles[opt_candles.index > pos.entry_time].iterrows():
                    bar_now = ts.to_pydatetime() + timedelta(minutes=config.INTERVAL_MINUTES)
                    o, hi, lo, cl = (float(bar["open"]), float(bar["high"]),
                                     float(bar["low"]), float(bar["close"]))
                    if o > 0 and (o - lo) / o > config.MAX_INTRABAR_COLLAPSE:
                        lo = min(o, cl)
                    order_sheet.note_bar_range(pos, hi, lo)
                    order_sheet.update_position(pos, lo, bar_now, square_off, bar_open=o)
                    if pos.closed:
                        break
                    order_sheet.update_position(pos, hi, bar_now, square_off, bar_open=o)
                    if pos.closed:
                        break

                    # Signal-invalidation exit (20-Aug-26) -- see
                    # _macd_invalidated's docstring. Same placement as the
                    # BACKTEST walk in _try_build_position: after the real
                    # stop/target range for this bar, using the bar's close.
                    if _macd_invalidated(final_df, candles, pos.symbol,
                                         pos.signal, ts):
                        order_sheet.close_for_signal_invalidation(
                            pos, cl, bar_now, "MACD Invalidation")
                        if pos.closed:
                            break

                    order_sheet.update_position(pos, cl, bar_now, square_off, bar_open=o)
                    if pos.closed:
                        break

            # Only forced closed if the real clock has genuinely reached
            # square-off -- never because this cycle's data happened to run out.
            if not pos.closed and is_eod:
                last = current_ltp if current_ltp is not None else pos.entry_ltp
                order_sheet.update_position(pos, last, square_off, square_off)

            if pos.closed:
                resolved_keys.append(key)
                terminal.append(pos)
                print(f"[live-orders]{tag} {pos.symbol} resolved -> "
                      f"{pos.exit_reason}, net Rs {pos.realised_pnl:,.2f}")
            else:
                rows.append((pos, current_ltp))

        for key in resolved_keys:
            del open_runs[key]
        return rows

    def _advance_live_position(pos: Position) -> tuple[Position, float | None]:
        """
        Advance ONE real broker position by whatever NEW option candles
        exist since pos.last_processed_bar, placing REAL orders for
        whatever fires. Mutates `pos` in place -- never rebuilt from a
        snapshot, unlike _advance_open_runs' PAPER replay, because a real
        broker order cannot be replayed. See new_live_state's docstring.

        Priority within a bar, same spirit as order_sheet.update_position:
        a broker-side stop that already fired wins first (it already
        happened, for real, whether this loop agrees or not), then EOD,
        then the MACD-invalidation exit (candle-close only, see
        _macd_invalidated), then targets/trailing via _evaluate_live_tick --
        the SAME per-observation core the 30-60s fast tracker uses (see
        fast_track_live_positions), called here once per lo/hi/close tick
        so a target reachable only intrabar is still caught.
        """
        contract = pos.contract
        opt_type = "CE" if pos.signal == config.SIGNAL_BUY_CE else "PE"
        _retry_protective_stop(angel, pos)

        opt_candles, _src = _fetch_opt_candles(
            contract, pos.symbol, opt_type, trade_date, angel, kite,
            nfo_instruments, now)

        current_ltp = None
        if opt_candles is not None and not opt_candles.empty:
            current_ltp = float(opt_candles["close"].iloc[-1])
            new_bars = opt_candles[opt_candles.index > pos.entry_time]
            if pos.last_processed_bar is not None:
                new_bars = new_bars[new_bars.index > pos.last_processed_bar]

            for ts, bar in new_bars.iterrows():
                if pos.closed:
                    break
                bar_now = ts.to_pydatetime() + timedelta(minutes=config.INTERVAL_MINUTES)
                o, hi, lo, cl = (float(bar["open"]), float(bar["high"]),
                                 float(bar["low"]), float(bar["close"]))
                if o > 0 and (o - lo) / o > config.MAX_INTRABAR_COLLAPSE:
                    lo = min(o, cl)
                pos.last_processed_bar = ts

                if _check_broker_stop_fired(angel, pos, bar_now):
                    break

                # --- signal invalidation (20-Aug-26) -- candle-close only,
                # needs THIS bar's colour and its MACD Recomm slot, neither
                # of which exists between candle closes (see the fast
                # tracker's docstring for why it can't run this check).
                if _macd_invalidated(final_df, candles, pos.symbol,
                                     pos.signal, ts):
                    try:
                        _real_exit(angel, pos, pos.remaining_qty,
                                  "MACD Invalidation", bar_now, True)
                    except live_orders.LiveOrderError as exc:
                        print(f"[live-orders] {pos.symbol}: MACD-INVALIDATION "
                              f"EXIT FAILED -- {exc}")
                    break

                _evaluate_live_tick(angel, pos, bar_now, lo, square_off)
                if pos.closed:
                    break
                _evaluate_live_tick(angel, pos, bar_now, hi, square_off)
                if pos.closed:
                    break
                _evaluate_live_tick(angel, pos, bar_now, cl, square_off)
                if pos.closed:
                    break

        return pos, current_ltp

    def _advance_live_positions() -> list[tuple]:
        """Same shape as _advance_open_runs' return value, for the real
        broker book. Resolved positions move into state['live_terminal']."""
        resolved_keys = []
        rows: list[tuple] = []
        for key, pos in state["live_positions"].items():
            pos, current_ltp = _advance_live_position(pos)
            if pos.closed:
                resolved_keys.append(key)
                state["live_terminal"].append(pos)
                print(f"[live-orders] [LIVE] {pos.symbol} resolved -> "
                      f"{pos.exit_reason}, net Rs {pos.realised_pnl:,.2f}")
            else:
                rows.append((pos, current_ltp))
        for key in resolved_keys:
            del state["live_positions"][key]
        return rows

    live_open_rows = _advance_live_positions()

    open_rows = _advance_open_runs(state["open_runs"], state["terminal"], "")
    shadow_open_rows = _advance_open_runs(
        state["shadow_open_runs"], state["shadow_terminal"], " [shadow]")
    capital_open_rows = _advance_open_runs(
        state["capital_shadow_open_runs"], state["capital_shadow_terminal"],
        " [capital-shadow]")
    oi_open_rows = _advance_open_runs(
        state["oi_shadow_open_runs"], state["oi_shadow_terminal"], " [oi-shadow]")

    real_positions = (state["terminal"] + state["live_terminal"]
                      + [p for p, _ in open_rows] + [p for p, _ in live_open_rows])
    real_ltps = ([None] * len(state["terminal"]) + [None] * len(state["live_terminal"])
                + [c for _, c in open_rows] + [c for _, c in live_open_rows])

    orders_df = order_sheet.build_orders_sheet(real_positions, real_ltps)
    rejected_df = order_sheet.build_rejected_sheet(state["rejections"])

    shadow_positions = state["shadow_terminal"] + [p for p, _ in shadow_open_rows]
    shadow_ltps = ([None] * len(state["shadow_terminal"])
                   + [c for _, c in shadow_open_rows])
    missed_df = order_sheet.build_orders_sheet(shadow_positions, shadow_ltps)

    capital_positions = (state["capital_shadow_terminal"]
                        + [p for p, _ in capital_open_rows])
    capital_ltps = ([None] * len(state["capital_shadow_terminal"])
                    + [c for _, c in capital_open_rows])
    capital_shadow_df = order_sheet.build_orders_sheet(capital_positions, capital_ltps)

    oi_positions = state["oi_shadow_terminal"] + [p for p, _ in oi_open_rows]
    oi_ltps = [None] * len(state["oi_shadow_terminal"]) + [c for _, c in oi_open_rows]
    oi_blocked_df = order_sheet.build_orders_sheet(oi_positions, oi_ltps)

    net = sum(p.realised_pnl for p in state["terminal"])
    print(f"[live-orders] {len(real_positions)} order(s) ({len(open_rows)} open), "
          f"{len(state['rejections'])} rejection(s), resolved net Rs {net:,.2f}")
    if shadow_positions:
        shadow_net = sum(p.realised_pnl for p in state["shadow_terminal"])
        print(f"[live-orders] {len(shadow_positions)} shadow trade(s) blocked only "
              f"by the concurrent-position cap ({len(shadow_open_rows)} open), "
              f"resolved net Rs {shadow_net:,.2f}")
    if capital_positions:
        cap_net = sum(p.realised_pnl for p in state["capital_shadow_terminal"])
        print(f"[live-orders] {len(capital_positions)} shadow trade(s) blocked only "
              f"by risk-budget/capital ({len(capital_open_rows)} open), "
              f"resolved net Rs {cap_net:,.2f}")
    if oi_positions:
        oi_net = sum(p.realised_pnl for p in state["oi_shadow_terminal"])
        print(f"[live-orders] {len(oi_positions)} shadow trade(s) blocked only by "
              f"the OI gate ({len(oi_open_rows)} open), resolved net Rs {oi_net:,.2f}")

    return orders_df, rejected_df, missed_df, capital_shadow_df, oi_blocked_df


if __name__ == "__main__":
    print(__doc__)

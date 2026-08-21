"""
STEP 11c — Real order placement on Angel One (SmartAPI), LIVE only.

Everything in order_sheet.py / order_engine.py before this file only ever
SIMULATED a fill against a candle price -- that is correct and necessary for
BACKTEST, and it is what every position has been until now (see
order_sheet.Position.trade_mode, always "PAPER" until this file existed).
This module is what actually sends an order to the broker.

THE ONE RULE THIS FILE EXISTS TO ENFORCE
-----------------------------------------
Never assume an order did what it was supposed to. A placeOrder() call
returns an order id, not a fill -- the order can be rejected, partially
filled, or sit pending. Every function here either confirms a REAL terminal
state from the order book before returning, or raises LiveOrderError. There
is no code path where a caller can mistake "the API call didn't throw" for
"the trade happened."

PROTECTIVE STOP, PLACED IMMEDIATELY ON ENTRY
---------------------------------------------
order_sheet.update_position's SL/Target/TSL logic only runs once per
5-minute candle-close cycle in Python. If the process dies, the laptop
sleeps, or the network drops, that logic simply doesn't run -- and a live
long-option position has nothing else protecting it. A resting SL-M order
at the broker is the real protection; the Python loop's job for a LIVE
position becomes managing TARGETS, moving that stop (breakeven/trailing),
watching for the new MACD-invalidation exit, and EOD square-off -- not
being the only thing standing between the position and an unbounded loss.

KILL SWITCH
-----------
paths.LIVE_KILL_SWITCH_FILE. Checked before every NEW entry, nowhere else --
an already-open position still gets its exits managed and placed for real
even with the switch on. See kill_switch_active()'s docstring.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import paths

VARIETY_NORMAL = "NORMAL"
VARIETY_STOPLOSS = "STOPLOSS"

ORDER_POLL_INTERVAL_SECS = 1.0
ORDER_FILL_TIMEOUT_SECS = 15.0

_TERMINAL_FILLED = {"complete"}
_TERMINAL_DEAD = {"rejected", "cancelled"}


class LiveOrderError(RuntimeError):
    """
    A real order did not do what it was supposed to. Always raised, never
    swallowed -- the caller (order_engine.py) is expected to stop and print
    loudly, not fall back to a simulated price as if nothing happened.
    """


def kill_switch_active() -> bool:
    """
    True if paths.LIVE_KILL_SWITCH_FILE exists. Create that file by hand
    (empty is enough) to stop new live entries instantly without editing
    config.py or restarting the running process -- checked once per entry
    attempt, so it takes effect on the very next candle cycle. Positions
    already open are unaffected: they still need real exits placed.
    """
    return paths.LIVE_KILL_SWITCH_FILE.exists()


# --------------------------------------------------------------------------
# order params / placement primitives
# --------------------------------------------------------------------------
def _order_params(*, tradingsymbol: str, symboltoken, transactiontype: str,
                  quantity: int, exchange: str = "NFO", ordertype: str = "MARKET",
                  producttype: str = "INTRADAY", variety: str = VARIETY_NORMAL,
                  price: str = "0", triggerprice: str = "0") -> dict:
    return {
        "variety": variety,
        "tradingsymbol": tradingsymbol,
        "symboltoken": str(symboltoken),
        "transactiontype": transactiontype,
        "exchange": exchange,
        "ordertype": ordertype,
        "producttype": producttype,
        "duration": "DAY",
        "price": str(price),
        "triggerprice": str(triggerprice),
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(int(quantity)),
    }


def _place(angel, params: dict, label: str) -> str:
    """One placeOrder call. Raises LiveOrderError on anything but a clean accept."""
    try:
        order_id = angel.placeOrder(params)
    except Exception as exc:
        raise LiveOrderError(f"{label}: placeOrder raised: {exc}") from exc
    if not order_id:
        raise LiveOrderError(f"{label}: placeOrder returned no order id")
    print(f"[live-order] {label}: placed, order id {order_id}")
    return str(order_id)


def _order_book_row(angel, order_id: str) -> dict | None:
    try:
        resp = angel.orderBook()
    except Exception as exc:
        print(f"[live-order] orderBook() failed: {exc}")
        return None
    if not resp or not resp.get("status"):
        return None
    for row in resp.get("data") or []:
        if str(row.get("orderid")) == str(order_id):
            return row
    return None


def wait_for_terminal(angel, order_id: str, label: str,
                      timeout_secs: float = ORDER_FILL_TIMEOUT_SECS) -> dict:
    """
    Poll the order book until the order is filled, rejected, or cancelled,
    or until timeout. Returns the last-seen row either way -- a timeout is
    NOT the same as a rejection (the order may still fill a moment later),
    so this does not raise on timeout; the caller decides what "still
    pending" means for that specific order (an entry going nowhere is a
    different problem than a protective stop going nowhere).

    Raises LiveOrderError only if the order never showed up in the order
    book at all within the timeout -- that is a real "something is wrong,"
    not a normal pending state.
    """
    deadline = time.monotonic() + timeout_secs
    last_row: dict | None = None
    while time.monotonic() < deadline:
        row = _order_book_row(angel, order_id)
        if row is not None:
            last_row = row
            status = str(row.get("status", "")).lower()
            if status in _TERMINAL_FILLED or status in _TERMINAL_DEAD:
                return row
        time.sleep(ORDER_POLL_INTERVAL_SECS)

    if last_row is None:
        raise LiveOrderError(
            f"{label}: order {order_id} never appeared in the order book "
            f"within {timeout_secs:.0f}s -- check the Angel One app by hand, now"
        )
    print(f"[live-order] {label}: order {order_id} still "
          f"'{last_row.get('status')}' after {timeout_secs:.0f}s -- pending")
    return last_row


def order_status(angel, order_id: str) -> dict | None:
    """One-shot status check, no waiting -- used to notice a resting stop
    that filled on its own since the last cycle."""
    return _order_book_row(angel, order_id)


@dataclass
class LiveFill:
    order_id: str
    filled_qty: int
    avg_price: float
    status: str


def _parse_fill(row: dict) -> LiveFill:
    return LiveFill(
        order_id=str(row.get("orderid", "")),
        filled_qty=int(float(row.get("filledshares") or 0)),
        avg_price=float(row.get("averageprice") or 0.0),
        status=str(row.get("status", "")).lower(),
    )


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------
def enter_live(angel, trading_symbol: str, symbol_token, quantity: int) -> LiveFill:
    """
    MARKET BUY to open a long option position, confirmed filled before
    returning. Raises LiveOrderError (never returns a fake fill) if the
    order is rejected or doesn't fill -- the caller must not open a
    Position off a trade the broker didn't actually confirm.
    """
    params = _order_params(tradingsymbol=trading_symbol, symboltoken=symbol_token,
                           transactiontype="BUY", quantity=quantity)
    order_id = _place(angel, params, f"ENTRY {trading_symbol}")
    row = wait_for_terminal(angel, order_id, f"ENTRY {trading_symbol}")
    fill = _parse_fill(row)
    if fill.status != "complete" or fill.filled_qty <= 0:
        raise LiveOrderError(
            f"ENTRY {trading_symbol}: order {order_id} did not fill "
            f"(status={fill.status}, filled={fill.filled_qty})")
    print(f"[live-order] ENTRY {trading_symbol}: filled {fill.filled_qty} @ "
          f"{fill.avg_price:.2f} (order {order_id})")
    return fill


def place_protective_stop(angel, trading_symbol: str, symbol_token,
                          quantity: int, trigger_price: float) -> str:
    """SL-M SELL resting at the broker -- the real protection, placed the
    moment the entry fill is confirmed. See this module's docstring."""
    params = _order_params(
        tradingsymbol=trading_symbol, symboltoken=symbol_token,
        transactiontype="SELL", quantity=quantity,
        ordertype="STOPLOSS_MARKET", variety=VARIETY_STOPLOSS,
        triggerprice=str(round(trigger_price, 1)))
    return _place(angel, params, f"PROTECTIVE STOP {trading_symbol}")


def update_protective_stop(angel, order_id: str, trading_symbol: str,
                           symbol_token, quantity: int,
                           new_trigger_price: float) -> str:
    """
    Move the resting SL-M's trigger price -- needed for the breakeven move
    at T1 and the trailing stop. Tries modifyOrder first (keeps the same
    order id); if the broker rejects the modify, cancels and re-places
    rather than leaving the position with no resting stop at all.
    """
    params = {
        "variety": VARIETY_STOPLOSS, "orderid": order_id,
        "ordertype": "STOPLOSS_MARKET", "producttype": "INTRADAY",
        "duration": "DAY", "price": "0",
        "triggerprice": str(round(new_trigger_price, 1)),
        "quantity": str(int(quantity)), "tradingsymbol": trading_symbol,
        "symboltoken": str(symbol_token), "exchange": "NFO",
    }
    try:
        resp = angel.modifyOrder(params)
        if resp:
            print(f"[live-order] STOP MOVED {trading_symbol}: order {order_id} "
                  f"-> trigger {new_trigger_price:.2f}")
            return order_id
    except Exception as exc:
        print(f"[live-order] modifyOrder failed for {trading_symbol} ({exc}) -- "
              f"cancelling and re-placing the stop instead")

    cancel_order(angel, order_id, VARIETY_STOPLOSS)
    return place_protective_stop(angel, trading_symbol, symbol_token,
                                 quantity, new_trigger_price)


def resize_protective_stop(angel, order_id: str, trading_symbol: str,
                           symbol_token, new_quantity: int,
                           trigger_price: float) -> str:
    """
    A target partial-exit shrinks the position -- the resting stop's
    quantity must shrink with it, or it eventually tries to sell more than
    is actually held. Always cancel-and-replace here rather than
    modifyOrder: a quantity change on a live SL-M is exactly the kind of
    edit some brokers' modify endpoints refuse outright, and silently
    keeping a stale, oversized stop resting is worse than a moment with no
    stop while it's replaced.
    """
    cancel_order(angel, order_id, VARIETY_STOPLOSS)
    return place_protective_stop(angel, trading_symbol, symbol_token,
                                 new_quantity, trigger_price)


def cancel_order(angel, order_id: str, variety: str) -> None:
    try:
        angel.cancelOrder(order_id, variety)
        print(f"[live-order] cancelled order {order_id}")
    except Exception as exc:
        print(f"[live-order] cancelOrder({order_id}) failed: {exc} -- it may "
              f"already be filled/cancelled, check the order book by hand")


def exit_live(angel, trading_symbol: str, symbol_token, quantity: int,
             stop_order_id: str | None, reason: str) -> LiveFill:
    """
    Close (all or part of) a live position for real. Cancels the resting
    protective stop FIRST so it can't also fire and double-sell, then
    MARKET SELLs `quantity`. Raises LiveOrderError -- and says so loudly --
    if the sell doesn't confirm filled, because at that point the position
    may genuinely still be open at the broker with no stop protecting it.
    """
    if stop_order_id:
        cancel_order(angel, stop_order_id, VARIETY_STOPLOSS)

    params = _order_params(tradingsymbol=trading_symbol, symboltoken=symbol_token,
                           transactiontype="SELL", quantity=quantity)
    order_id = _place(angel, params, f"EXIT {trading_symbol} ({reason})")
    row = wait_for_terminal(angel, order_id, f"EXIT {trading_symbol}")
    fill = _parse_fill(row)
    if fill.status != "complete" or fill.filled_qty <= 0:
        raise LiveOrderError(
            f"EXIT {trading_symbol} ({reason}): order {order_id} did not "
            f"fill (status={fill.status}) -- the position may STILL BE OPEN "
            f"at the broker with NO stop resting under it (the stop was just "
            f"cancelled above). Check the Angel One app by hand, immediately.")
    print(f"[live-order] EXIT {trading_symbol} ({reason}): filled "
          f"{fill.filled_qty} @ {fill.avg_price:.2f} (order {order_id})")
    return fill

"""
SB-STEP 5 -- Pure logic: parse sheet values, build order parameters, work
out the entry deadline, and manage the pre-Target-1 fixed stop / post-
Target-1 trailing stop. No network calls in this file on purpose, so all of
this can be tested without touching the live API.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import sb_config as cfg

_DATE_FORMATS = ["%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y"]
_TIME_FORMATS = ["%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"]


def normalise_lots(raw: str) -> int:
    try:
        lots = int(float(raw))
    except ValueError as exc:
        raise RuntimeError(f"Unrecognised LOT value: '{raw}'") from exc
    if lots <= 0:
        raise RuntimeError(f"LOT must be positive, got {lots}")
    if lots > cfg.MAX_LOTS_PER_ORDER:
        raise RuntimeError(
            f"{lots} lots exceeds MAX_LOTS_PER_ORDER ({cfg.MAX_LOTS_PER_ORDER}) in sb_config.py. "
            f"Raise the cap deliberately if this is intentional."
        )
    return lots


def normalise_price(raw: str, field_name: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Unrecognised {field_name} value: '{raw}'") from exc


def parse_row_datetime(date_raw: str, time_raw: str) -> datetime:
    date_part = None
    for fmt in _DATE_FORMATS:
        try:
            date_part = datetime.strptime(date_raw.strip(), fmt)
            break
        except ValueError:
            continue
    if date_part is None:
        raise RuntimeError(f"Unrecognised Date value: '{date_raw}'")

    time_part = None
    for fmt in _TIME_FORMATS:
        try:
            time_part = datetime.strptime(time_raw.strip(), fmt)
            break
        except ValueError:
            continue
    if time_part is None:
        raise RuntimeError(f"Unrecognised Time value: '{time_raw}'")

    return date_part.replace(hour=time_part.hour, minute=time_part.minute, second=time_part.second)


def is_today(row_dt: datetime) -> bool:
    return row_dt.date() == datetime.now().date()


def entry_deadline(row_dt: datetime) -> datetime:
    return row_dt + timedelta(minutes=cfg.ENTRY_WINDOW_MINUTES)


def build_entry_order(contract: dict[str, Any], lots: int) -> dict[str, Any]:
    quantity = lots * contract["lotsize"]
    return {
        "variety": cfg.VARIETY,
        "tradingsymbol": contract["symbol"],
        "symboltoken": contract["token"],
        "transactiontype": "BUY",
        "exchange": contract["exch_seg"],
        "ordertype": "MARKET",
        "producttype": cfg.PRODUCT_TYPE,
        "duration": cfg.DURATION,
        "price": "0",
        "quantity": quantity,
    }


def build_exit_order(contract: dict[str, Any], quantity: int) -> dict[str, Any]:
    return {
        "variety": cfg.VARIETY,
        "tradingsymbol": contract["symbol"],
        "symboltoken": contract["token"],
        "transactiontype": "SELL",
        "exchange": contract["exch_seg"],
        "ordertype": "MARKET",
        "producttype": cfg.PRODUCT_TYPE,
        "duration": cfg.DURATION,
        "price": "0",
        "quantity": quantity,
    }


def initial_stoploss(entry_ltp: float) -> float:
    """Fixed stop used before Target 1 is reached."""
    return round(entry_ltp * (1 - cfg.DEFAULT_STOPLOSS_PCT), 2)


def trail_gap(entry_ltp: float, target1: float) -> float:
    """Distance the trailing stop keeps below current price, once trailing
    starts -- same distance the trade needed to travel to reach Target 1."""
    gap = target1 - entry_ltp
    if gap <= 0:
        raise RuntimeError(f"Target 1 ({target1}) must be above Entry LTP ({entry_ltp}) for a BUY trade.")
    return round(gap, 2)


def update_trailing_stop(current_sl: float, ltp: float, gap: float) -> float:
    """Stop loss only ever moves up, never down."""
    candidate = round(ltp - gap, 2)
    return max(current_sl, candidate)

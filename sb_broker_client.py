"""
SB-STEP 4 -- Angel One (SmartAPI) login, order placement, and order-status
lookup.

TOTP-based headless login (no browser automation), same approach the main
pipeline's broker_auth.py uses. Credentials are read from
01_JSON_Files/harish_angel_one.json, never hardcoded, never printed.
"""
from __future__ import annotations

import json
from typing import Any

import sb_config as cfg


def login():
    """Return an authenticated SmartConnect session object."""
    try:
        import pyotp
        from SmartApi import SmartConnect
    except ImportError as exc:
        raise RuntimeError(
            "Angel One dependencies missing. Run: pip install smartapi-python pyotp"
        ) from exc

    with open(cfg.ANGEL_ONE_CREDENTIALS_FILE, "r", encoding="utf-8") as f:
        creds = json.load(f)

    smart = SmartConnect(api_key=creds["api_key"])
    totp = pyotp.TOTP(creds["totp_secret"]).now()  # live credential, never logged
    session = smart.generateSession(creds["client_id"], creds["password"], totp)

    if not session.get("status"):
        raise RuntimeError(f"Angel One login failed: {session}")

    return smart


def place_order(smart, order_params: dict[str, Any]) -> str:
    """Places the order and returns the Angel One order ID.
    Caller logs before/after (see sb_run_once.py)."""
    resp = smart.placeOrder(order_params)
    if isinstance(resp, dict):
        if not resp.get("status", True):
            raise RuntimeError(f"Order placement failed: {resp}")
        order_id = resp.get("data", {}).get("orderid") or resp.get("orderid")
    else:
        order_id = resp
    if not order_id:
        raise RuntimeError(f"Order placement returned no order id: {resp}")
    return str(order_id)


def get_ltp(smart, exchange: str, tradingsymbol: str, symboltoken: str) -> float:
    resp = smart.ltpData(exchange, tradingsymbol, symboltoken)
    if not resp.get("status"):
        raise RuntimeError(f"LTP fetch failed for {tradingsymbol}: {resp}")
    return float(resp["data"]["ltp"])


def get_order_status(smart, order_id: str) -> dict[str, Any] | None:
    """Looks up one order in today's order book.
    Returns a dict with at least 'status' (Angel One's order status string,
    e.g. 'complete', 'trigger pending', 'rejected', 'cancelled') and
    'averageprice' (0 if not yet filled), or None if not found."""
    resp = smart.orderBook()
    if not resp.get("status"):
        raise RuntimeError(f"Order book fetch failed: {resp}")
    for order in resp.get("data") or []:
        if str(order.get("orderid")) == str(order_id):
            return order
    return None

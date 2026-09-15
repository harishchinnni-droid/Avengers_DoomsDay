"""
SB-STEP 9 -- One tick of entry-watching for one sheet row.

Called repeatedly (every POLL_INTERVAL_SECONDS) by sb_live_monitor.py.
Pure state-in, state-out -- easy to test without a live broker connection
by passing a fake `smart` with a canned get_ltp.

State dict fields: row_number, contract, lots, trigger_price, deadline,
outcome (None while still watching, else "ENTERED" or "EXPIRED"),
order_id, entry_ltp.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import sb_broker_client as broker
import sb_trade_logic as logic


def check_entry(smart, state: dict[str, Any]) -> dict[str, Any]:
    if state["outcome"] is not None:
        return state  # already resolved, nothing to do

    if datetime.now() >= state["deadline"]:
        state["outcome"] = "EXPIRED"
        return state

    contract = state["contract"]
    ltp = broker.get_ltp(smart, contract["exch_seg"], contract["symbol"], contract["token"])
    state["last_ltp"] = ltp

    if ltp >= state["trigger_price"]:
        order_params = logic.build_entry_order(contract, state["lots"])
        order_id = broker.place_order(smart, order_params)
        state["outcome"] = "ENTERED"
        state["order_id"] = order_id
        state["entry_ltp"] = ltp  # best estimate at trigger time; order book has the true average fill

    return state

"""
SB-STEP 10 -- One tick of exit-tracking for one open position.

Called repeatedly by sb_live_monitor.py after a row has ENTERED. Manages
the fixed Stop Loss before Target 1, switches to a trailing Stop Loss once
Target 1 is hit, and auto-sells the moment price hits the current Stop
Loss.

State dict fields: row_number, contract, quantity, entry_ltp, target1,
target2, current_sl, trailing (bool), gap, target2_hit (bool, log-only),
outcome (None while open, else "EXITED"), exit_ltp, exit_order_id.
"""
from __future__ import annotations

from typing import Any

import sb_broker_client as broker
import sb_trade_logic as logic


def check_exit(smart, state: dict[str, Any]) -> dict[str, Any]:
    if state["outcome"] is not None:
        return state

    contract = state["contract"]
    ltp = broker.get_ltp(smart, contract["exch_seg"], contract["symbol"], contract["token"])
    state["last_ltp"] = ltp

    if not state["trailing"] and ltp >= state["target1"]:
        state["trailing"] = True

    if state["trailing"]:
        state["current_sl"] = logic.update_trailing_stop(state["current_sl"], ltp, state["gap"])

    if state.get("target2") and not state["target2_hit"] and ltp >= state["target2"]:
        state["target2_hit"] = True  # informational only -- trailing rule doesn't change

    if ltp <= state["current_sl"]:
        order_params = logic.build_exit_order(contract, state["quantity"])
        order_id = broker.place_order(smart, order_params)
        state["outcome"] = "EXITED"
        state["exit_ltp"] = ltp
        state["exit_order_id"] = order_id

    return state

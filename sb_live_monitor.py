"""
SB-STEP 11 -- Main script. Run this.

    py sb_live_monitor.py

Runs continuously (Ctrl+C to stop). Every POLL_INTERVAL_SECONDS (default 30):

  1. Scans the "Input" tab for new rows (Order ID empty, all fields filled
     in including Expiry) and starts watching any that are dated today.
  2. For each row being watched: if price has crossed Target Entry Price,
     places a MARKET buy and starts tracking it. If ENTRY_WINDOW_MINUTES
     (default 30) pass with no entry, marks it EXPIRED and stops watching.
  3. For each row that has entered: tracks price against Target 1 / Target 2.
     Stop Loss starts as a fixed percentage below entry (sb_config.py).
     Once Target 1 is hit, Stop Loss switches to trailing -- it follows
     price up, always staying (Target 1 - Entry) points behind. The moment
     price hits the current Stop Loss, places a MARKET sell to exit.

Keep this window open during market hours -- closing it stops all
tracking. Every state change is appended to logs/sb_bot.log AND written to
the sheet's Status column, so you can see what happened even if you weren't
watching.

Only tracks rows that were pending when this script was already running or
started watching them -- if you restart the script mid-trade, it will NOT
automatically resume tracking an already-ENTERED position (the Status
column will show TRACKING/TRAILING from before, but nothing will act on it
until you intervene). Keep this running continuously during the trading
window instead of restarting it.
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime

import sb_auto_shutdown as auto_shutdown
import sb_broker_client as broker
import sb_config as cfg
import sb_entry_tracker as entry_tracker
import sb_exit_tracker as exit_tracker
import sb_option_lookup as lookup
import sb_sheet_client as sheet
import sb_trade_logic as logic
import sb_write_back as write_back


def _setup_logging() -> logging.Logger:
    cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(cfg.LOG_FILE, mode="a", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("sb_bot")


def main() -> int:
    log = _setup_logging()
    log.info("=== sb_live_monitor start (LIVE_TRADING=%s, poll every %ss) ===",
              cfg.LIVE_TRADING, cfg.POLL_INTERVAL_SECONDS)

    if not cfg.LIVE_TRADING:
        log.warning("LIVE_TRADING is False in sb_config.py -- this script only places real orders "
                     "when it's True. Set it to True when you're ready to go live.")
        return 0

    try:
        ws = sheet.open_worksheet()
        known_cols = sheet.ensure_columns(ws)
        input_cols = sheet.find_input_columns(ws)
        smart = broker.login()
        log.info("Angel One login OK")
    except Exception:
        log.exception("Startup failed")
        return 1

    watching: dict[int, dict] = {}   # row_number -> entry-tracker state
    tracking: dict[int, dict] = {}   # row_number -> exit-tracker state
    known_rows: set[int] = set()     # rows we've already claimed, to avoid re-watching
    last_activity_at = datetime.now()

    while True:
        try:
            found_new_row = _scan_for_new_rows(ws, input_cols, known_cols, known_rows, watching, log)
            _tick_entries(smart, ws, known_cols, watching, tracking, log)
            _tick_exits(smart, ws, known_cols, tracking, log)

            if found_new_row or watching or tracking:
                last_activity_at = datetime.now()
            elif auto_shutdown.should_shutdown(watching, tracking, last_activity_at):
                auto_shutdown.shutdown_pc()
                return 0

        except KeyboardInterrupt:
            log.info("Stopped by user (Ctrl+C).")
            return 0
        except Exception:
            log.exception("Unexpected error in main loop -- continuing after a short pause")

        time.sleep(cfg.POLL_INTERVAL_SECONDS)


def _scan_for_new_rows(ws, input_cols, known_cols, known_rows, watching, log) -> bool:
    rows = sheet.find_all_pending_rows(ws, input_cols, known_cols)
    found_any = False
    for row in rows:
        if row["row_number"] in known_rows:
            continue
        known_rows.add(row["row_number"])
        found_any = True

        try:
            row_dt = logic.parse_row_datetime(row["date"], row["time"])
        except Exception as exc:
            log.error("Row %s: %s -- skipping", row["row_number"], exc)
            continue

        if not logic.is_today(row_dt):
            log.info("Row %s is not dated today -- skipping", row["row_number"])
            continue

        try:
            contract = lookup.resolve_contract(row["underlying"], row["strike"], row["option_type"], row["expiry"])
            lots = logic.normalise_lots(row["lots"])
            trigger_price = logic.normalise_price(row["trigger_price"], "Target Entry Price")
            target1 = logic.normalise_price(row["target1"], "Target 1")
            target2 = logic.normalise_price(row["target2"], "Target 2") if row.get("target2") else None
        except Exception as exc:
            log.error("Row %s: %s", row["row_number"], exc)
            write_back.write_entry_failed(ws, row["row_number"], known_cols, exc)
            continue

        deadline = logic.entry_deadline(row_dt)
        if datetime.now() >= deadline:
            log.info("Row %s: entry window already passed -- marking EXPIRED", row["row_number"])
            write_back.write_expired(ws, row["row_number"], known_cols)
            continue

        watching[row["row_number"]] = {
            "row_number": row["row_number"],
            "contract": contract,
            "lots": lots,
            "trigger_price": trigger_price,
            "target1": target1,
            "target2": target2,
            "deadline": deadline,
            "outcome": None,
            "order_id": None,
            "entry_ltp": None,
        }
        log.info("Row %s: now watching %s, trigger %s, deadline %s",
                  row["row_number"], contract["symbol"], trigger_price, deadline)
        write_back.write_watching(ws, row["row_number"], known_cols)

    return found_any


def _tick_entries(smart, ws, known_cols, watching, tracking, log) -> None:
    for row_number in list(watching.keys()):
        try:
            state = entry_tracker.check_entry(smart, watching[row_number])
        except Exception as exc:
            # One row's broker/API failure (e.g. order rejected) must not stop
            # other rows from being checked this tick -- log it, leave the row
            # in `watching` so it retries next tick, until it either succeeds
            # or its own deadline expires it.
            log.exception("Row %s: entry check failed, will retry next tick", row_number)
            write_back.write_entry_failed(ws, row_number, known_cols, exc)
            continue

        if state["outcome"] == "EXPIRED":
            log.info("Row %s: EXPIRED (target entry price not met within window)", row_number)
            write_back.write_expired(ws, row_number, known_cols)
            del watching[row_number]

        elif state["outcome"] == "ENTERED":
            log.info("Row %s: ENTERED at %s, order %s", row_number, state["entry_ltp"], state["order_id"])
            initial_sl = logic.initial_stoploss(state["entry_ltp"])
            gap = logic.trail_gap(state["entry_ltp"], state["target1"])
            write_back.write_entered(ws, row_number, known_cols,
                                      order_id=state["order_id"], entry_ltp=state["entry_ltp"], initial_sl=initial_sl)
            tracking[row_number] = {
                "row_number": row_number,
                "contract": state["contract"],
                "quantity": state["lots"] * state["contract"]["lotsize"],
                "entry_ltp": state["entry_ltp"],
                "target1": state["target1"],
                "target2": state["target2"],
                "current_sl": initial_sl,
                "trailing": False,
                "gap": gap,
                "target2_hit": False,
                "outcome": None,
                "exit_ltp": None,
                "exit_order_id": None,
            }
            del watching[row_number]


def _tick_exits(smart, ws, known_cols, tracking, log) -> None:
    for row_number in list(tracking.keys()):
        prev_sl = tracking[row_number]["current_sl"]
        try:
            state = exit_tracker.check_exit(smart, tracking[row_number])
        except Exception as exc:
            log.exception("Row %s: exit check failed", row_number)
            write_back.write_exit_failed(ws, row_number, known_cols, exc)
            continue

        if state["outcome"] == "EXITED":
            log.info("Row %s: EXITED at %s, order %s", row_number, state["exit_ltp"], state["exit_order_id"])
            write_back.write_exited(ws, row_number, known_cols,
                                     exit_ltp=state["exit_ltp"], exit_order_id=state["exit_order_id"])
            del tracking[row_number]
        elif state["current_sl"] != prev_sl:
            log.info("Row %s: Stop Loss moved to %s (trailing=%s)", row_number, state["current_sl"], state["trailing"])
            write_back.write_sl_update(ws, row_number, known_cols, state["current_sl"], state["trailing"])


if __name__ == "__main__":
    raise SystemExit(main())

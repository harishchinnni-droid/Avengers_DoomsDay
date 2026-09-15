"""
SB-STEP 6 -- Write results back into the sheet row. Never touches Target 1
/ Target 2 -- those are Harish's own inputs.
"""
from __future__ import annotations

import sb_sheet_client as sheet


def write_watching(ws, row_number: int, known_cols: dict[str, int]) -> None:
    sheet.write_result(ws, row_number, known_cols, {"Status": "WATCHING FOR ENTRY"})


def write_expired(ws, row_number: int, known_cols: dict[str, int]) -> None:
    sheet.write_result(ws, row_number, known_cols, {"Status": "EXPIRED -- target entry price not met in time"})


def write_entered(ws, row_number: int, known_cols: dict[str, int], *, order_id: str, entry_ltp: float, initial_sl: float) -> None:
    sheet.write_result(ws, row_number, known_cols, {
        "Order ID": order_id,
        "Entry LTP": entry_ltp,
        "Stop Loss": initial_sl,
        "Status": "TRACKING",
        "Order Time": sheet.timestamp(),
    })


def write_entry_failed(ws, row_number: int, known_cols: dict[str, int], error: Exception) -> None:
    sheet.write_result(ws, row_number, known_cols, {
        "Status": f"ENTRY FAILED: {error}"[:200],
        "Order Time": sheet.timestamp(),
    })


def write_sl_update(ws, row_number: int, known_cols: dict[str, int], current_sl: float, trailing: bool) -> None:
    sheet.write_result(ws, row_number, known_cols, {
        "Stop Loss": current_sl,
        "Status": "TRAILING" if trailing else "TRACKING",
    })


def write_exited(ws, row_number: int, known_cols: dict[str, int], *, exit_ltp: float, exit_order_id: str) -> None:
    sheet.write_result(ws, row_number, known_cols, {
        "Status": f"EXITED at {exit_ltp} (order {exit_order_id})",
        "Order Time": sheet.timestamp(),
    })


def write_exit_failed(ws, row_number: int, known_cols: dict[str, int], error: Exception) -> None:
    sheet.write_result(ws, row_number, known_cols, {"Status": f"EXIT FAILED: {error} -- SELL MANUALLY NOW"[:200]})

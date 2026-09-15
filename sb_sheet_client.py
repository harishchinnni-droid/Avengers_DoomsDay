"""
SB-STEP 2 -- Google Sheet access.

Connects with a service-account key, reads the "Input" tab, works out which
columns are which by header name, makes sure the "Expiry" input column and
the bot's output columns exist, and finds rows that still need watching
(Order ID empty, all required fields filled in).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import sb_config as cfg


def _client():
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(str(cfg.GOOGLE_CREDENTIALS_FILE), scopes=scopes)
    return gspread.authorize(creds)


def open_worksheet():
    gc = _client()
    sh = gc.open_by_key(cfg.SHEET_ID)
    return sh.worksheet(cfg.WORKSHEET_NAME)


def _header_map(headers: list[str]) -> dict[str, int]:
    return {h.strip().lower(): i + 1 for i, h in enumerate(headers) if h.strip()}


def _find_column(header_map: dict[str, int], aliases: list[str]) -> int | None:
    for alias in aliases:
        col = header_map.get(alias.strip().lower())
        if col:
            return col
    return None


def _append_column(ws, headers: list[str], name: str) -> int:
    col = len(headers) + 1
    ws.update_cell(1, col, name)
    headers.append(name)
    return col


def ensure_columns(ws) -> dict[str, int]:
    """Make sure every OUTPUT column and the 'Expiry' input column exist,
    appending any that are missing. Returns {name -> 1-based column index}."""
    headers = ws.row_values(1)
    header_map = _header_map(headers)
    result: dict[str, int] = {}

    expiry_col = _find_column(header_map, cfg.INPUT_COLUMN_ALIASES["expiry"])
    if not expiry_col:
        expiry_col = _append_column(ws, headers, "Expiry")
        header_map = _header_map(headers)
    result["Expiry"] = expiry_col

    for name in cfg.OUTPUT_COLUMNS:
        col = header_map.get(name.strip().lower())
        if not col:
            col = _append_column(ws, headers, name)
            header_map = _header_map(headers)
        result[name] = col

    return result


REQUIRED_INPUT_FIELDS = ("date", "time", "underlying", "strike", "option_type", "lots", "trigger_price", "target1")


def find_input_columns(ws) -> dict[str, int]:
    headers = ws.row_values(1)
    header_map = _header_map(headers)
    cols: dict[str, int] = {}
    for field in REQUIRED_INPUT_FIELDS:
        col = _find_column(header_map, cfg.INPUT_COLUMN_ALIASES[field])
        if not col:
            raise RuntimeError(
                f"Could not find a column for '{field}' in the '{cfg.WORKSHEET_NAME}' tab. "
                f"Expected one of: {cfg.INPUT_COLUMN_ALIASES[field]}. "
                f"Rename a column to match, or add an alias in sb_config.py."
            )
        cols[field] = col
    # target2 is optional -- only used for logging, never required to start a trade
    cols["target2"] = _find_column(header_map, cfg.INPUT_COLUMN_ALIASES["target2"])
    return cols


def _row_dict(row: list[str], input_cols: dict[str, int], expiry_col: int) -> dict[str, str] | None:
    def cell(col: int | None) -> str:
        if not col or len(row) < col:
            return ""
        return row[col - 1].strip()

    data = {
        "date": cell(input_cols["date"]),
        "time": cell(input_cols["time"]),
        "underlying": cell(input_cols["underlying"]),
        "strike": cell(input_cols["strike"]),
        "option_type": cell(input_cols["option_type"]),
        "lots": cell(input_cols["lots"]),
        "trigger_price": cell(input_cols["trigger_price"]),
        "target1": cell(input_cols["target1"]),
        "target2": cell(input_cols.get("target2")),
        "expiry": cell(expiry_col),
    }
    required_ok = all(data[f] for f in ("date", "time", "underlying", "strike", "option_type", "lots",
                                         "trigger_price", "target1", "expiry"))
    return data if required_ok else None


def find_all_pending_rows(ws, input_cols: dict[str, int], known_cols: dict[str, int]) -> list[dict[str, Any]]:
    """Every row with an empty Order ID and all required fields filled in
    (including Expiry). Used by sb_live_monitor.py, which tracks all of
    them concurrently, not just the newest."""
    all_values = ws.get_all_values()
    if len(all_values) <= 1:
        return []

    order_id_col = known_cols["Order ID"]
    expiry_col = known_cols["Expiry"]
    pending = []

    for row_number in range(2, len(all_values) + 1):
        row = all_values[row_number - 1]
        order_id = row[order_id_col - 1].strip() if len(row) >= order_id_col else ""
        if order_id:
            continue

        data = _row_dict(row, input_cols, expiry_col)
        if data is None:
            continue
        data["row_number"] = row_number
        pending.append(data)

    return pending


def write_result(ws, row_number: int, known_cols: dict[str, int], values: dict[str, Any]) -> None:
    updates = []
    for name, val in values.items():
        col = known_cols[name]
        updates.append({"range": _a1(row_number, col), "values": [[str(val)]]})
    if updates:
        ws.batch_update(updates)


def _a1(row: int, col: int) -> str:
    letters = ""
    n = col
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

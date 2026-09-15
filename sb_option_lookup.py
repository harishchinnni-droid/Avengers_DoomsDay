"""
SB-STEP 3 -- Resolve a sheet row (underlying + strike + CE/PE + expiry)
into the exact Angel One trading symbol, token, and lot size, using the
scrip master already downloaded to 01_JSON_Files/OpenAPIScripMaster.json.

Kept separate from sb_broker_client.py so this lookup logic (pure data,
no network/login) can be tested on its own.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from typing import Any

import sb_config as cfg

_EXPIRY_FORMATS = [
    "%d%b%Y", "%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y",
    "%d%b%y", "%d-%b-%y", "%d %b %y", "%d/%m/%y", "%d-%m-%y",  # 2-digit year, e.g. 29-Sep-26
]


@lru_cache(maxsize=1)
def _scrip_master() -> list[dict[str, Any]]:
    with open(cfg.SCRIP_MASTER_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalise_expiry(raw: str) -> str:
    """Sheet might have '23-Sep-2026', '23 Sep 2026', '23/09/2026', etc.
    Scrip master always uses '23SEP2026'. Try common formats."""
    raw = raw.strip()
    for fmt in _EXPIRY_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%d%b%Y").upper()
        except ValueError:
            continue
    # already in scrip-master format, e.g. "23SEP2026"
    if re.fullmatch(r"\d{2}[A-Z]{3}\d{4}", raw.upper()):
        return raw.upper()
    raise RuntimeError(
        f"Could not understand Expiry value '{raw}'. Use a format like '23-Sep-2026'."
    )


def _normalise_option_type(raw: str) -> str:
    s = raw.strip().upper()
    if s in ("CE", "CALL", "C"):
        return "CE"
    if s in ("PE", "PUT", "P"):
        return "PE"
    raise RuntimeError(f"Unrecognised CE/PE value: '{raw}'")


def resolve_contract(underlying: str, strike: str, option_type: str, expiry: str) -> dict[str, Any]:
    """Returns {'symbol': ..., 'token': ..., 'lotsize': int, 'exch_seg': ...}."""
    name = underlying.strip().upper()
    opt = _normalise_option_type(option_type)
    expiry_norm = _normalise_expiry(expiry)

    try:
        strike_paise = round(float(strike) * 100)
    except ValueError as exc:
        raise RuntimeError(f"Unrecognised strike value: '{strike}'") from exc

    for row in _scrip_master():
        if row.get("name", "").upper() != name:
            continue
        if row.get("instrumenttype") not in ("OPTSTK", "OPTIDX"):
            continue
        if row.get("expiry", "").upper() != expiry_norm:
            continue
        try:
            row_strike = round(float(row.get("strike", "0")))
        except ValueError:
            continue
        if row_strike != strike_paise:
            continue
        if not row.get("symbol", "").upper().endswith(opt):
            continue

        return {
            "symbol": row["symbol"],
            "token": row["token"],
            "lotsize": int(row["lotsize"]),
            "exch_seg": row.get("exch_seg", cfg.EXCHANGE),
        }

    raise RuntimeError(
        f"No matching option contract found for {name} {strike} {opt} expiring {expiry_norm}. "
        f"Check the underlying spelling matches Angel One exactly, the strike is correct, "
        f"and the expiry date actually exists for this symbol."
    )

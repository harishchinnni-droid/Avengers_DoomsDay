"""
SB-STEP 1 -- Settings for the simple Sheet -> Angel One -> Sheet options bot.

Separate, lightweight bot. Does not touch or import anything from the main
pipeline (config.py, order_engine.py, etc.) -- that system is untouched.

What this bot does:
  1. Reads the newest row in the "Input" tab with no Order ID yet.
  2. Looks up the exact option contract (underlying + strike + CE/PE + your
     Expiry) in the Angel One scrip master.
  3. Places a STOPLOSS_MARKET buy order that triggers once price crosses
     your "Target Entry Price" -- a breakout entry, not an instant buy.
  4. Writes the Order ID back immediately (sb_run_once.py).
  5. Separately, sb_check_orders.py polls pending orders and, once one
     actually fills, writes back Entry LTP and a computed Stop Loss.

Install once before running anything:
    pip install gspread google-auth smartapi-python pyotp

Before first run:
    Open harish_credentials.json, find "client_email", and Share the Google
    Sheet with that email as Editor.
"""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent          # .../04_Avengers_DoomsDay_04-Sep-26
JSON_DIR = BASE_DIR / "01_JSON_Files"

GOOGLE_CREDENTIALS_FILE = JSON_DIR / "harish_credentials.json"
ANGEL_ONE_CREDENTIALS_FILE = JSON_DIR / "harish_angel_one.json"
SCRIP_MASTER_FILE = JSON_DIR / "OpenAPIScripMaster.json"

LOG_DIR = BASE_DIR / "02_Codes" / "logs"
LOG_FILE = LOG_DIR / "sb_bot.log"          # appended, never overwritten

# ---------------------------------------------------------------------------
# Google Sheet
# ---------------------------------------------------------------------------
SHEET_ID = "1q2bZhseDRclGmRW-_voZQCNV2nJt9JP75Xo8D-o2nNE"
WORKSHEET_NAME = "Input"

# INPUT columns -- the bot only reads these, never overwrites them.
# First alias that matches an existing header (case-insensitive) wins.
INPUT_COLUMN_ALIASES = {
    "date":         ["Date"],
    "time":         ["Time"],
    "underlying":   ["Symbol / StrikePrice", "Symbol", "Underlying"],
    "strike":       ["StrikePrice", "Strike"],
    "option_type":  ["CE / PE", "CE/PE", "Option Type"],
    "lots":         ["LOT", "Lots", "Qty"],
    "trigger_price": ["Target Entry Price", "Entry Trigger"],
    "target1":      ["Target 1"],
    "target2":      ["Target 2"],
    "expiry":       ["Expiry"],   # created automatically if missing -- Harish fills it per row
}

# OUTPUT columns -- bot-owned, created automatically if missing. Target 1 /
# Target 2 are NOT here on purpose: Harish already fills those in himself,
# the bot must never touch them.
OUTPUT_COLUMNS = [
    "Order ID",
    "Entry LTP",
    "Stop Loss",
    "Status",
    "Order Time",
]

# ---------------------------------------------------------------------------
# Order settings
# ---------------------------------------------------------------------------
LIVE_TRADING = True            # True = places REAL orders with real money
EXCHANGE = "NFO"               # options trade on NFO
TRANSACTION_TYPE = "BUY"       # every order is a buy (CE/PE already encodes direction)
VARIETY = "NORMAL"             # self-managed entries/exits use plain market orders, not broker-side triggers
PRODUCT_TYPE = "INTRADAY"      # INTRADAY / CARRYFORWARD -- change here if you hold overnight
DURATION = "DAY"

MAX_LOTS_PER_ORDER = 10        # safety cap -- bot refuses a bigger single order than this

# Stop Loss BEFORE Target 1 is hit -- percentage below entry premium.
DEFAULT_STOPLOSS_PCT = 0.30    # 30% below entry premium -- adjust to your risk

# ---------------------------------------------------------------------------
# Live monitoring loop (sb_live_monitor.py)
# ---------------------------------------------------------------------------
ENTRY_WINDOW_MINUTES = 30      # give up watching a row if Target Entry Price isn't
                                # hit within this many minutes of the row's Date+Time
POLL_INTERVAL_SECONDS = 30     # how often the loop checks prices, for both entry and exit

# Trailing stop, once Target 1 is hit: SL = current LTP - gap, where
# gap = Target 1 - Entry LTP (the same distance the trade needed to reach
# Target 1 in the first place). SL only ever moves up, never down.

# ---------------------------------------------------------------------------
# Idle auto-shutdown -- saves power when nothing is happening
# ---------------------------------------------------------------------------
# Shuts the PC down if ALL of these hold for IDLE_SHUTDOWN_MINUTES straight:
#   - no new sheet row has appeared to watch
#   - nothing is currently being watched for entry
#   - no position is currently open and being tracked  <- hard rule, never overridden
#   - (if AUTO_SHUTDOWN_REQUIRE_OS_IDLE) no keyboard/mouse activity on the PC either
AUTO_SHUTDOWN_ENABLED = True
IDLE_SHUTDOWN_MINUTES = 90
SHUTDOWN_TIMER_START_HOUR = 10   # the idle clock never starts counting before this hour (24h, local time)
SHUTDOWN_TIMER_START_MINUTE = 0
AUTO_SHUTDOWN_REQUIRE_OS_IDLE = True   # also require no mouse/keyboard activity, not just no sheet activity
SHUTDOWN_WARNING_SECONDS = 120         # grace period before shutdown actually happens; run "shutdown /a" to cancel

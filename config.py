"""
STEP 0c — Every setting in one place. No magic numbers anywhere else.

Signal thresholds below are MY REVERSE-ENGINEERED DEFAULTS, read off the
sample workbook 31-Jul-26 FNO-L.xlsx. They are marked. Harish said to leave
them configurable rather than confirm them, so they live here where they can
be changed in one edit -- but until he confirms them, any output built on
them should be treated as unverified.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# MASTER SAFETY SWITCH
# --------------------------------------------------------------------------
# This pipeline (steps 1-8) does not place orders at all. The flag exists so
# the order module added later has something to check, and so it defaults to
# off from the very first commit rather than being bolted on afterwards.
LIVE_TRADING = False

# The per-run KPI sheet (dashboard.py) -- KPI panels, per-symbol/exit-reason/
# equity/hourly/rotation tables, no native Excel charts (charts were tried
# 15-Aug-26, removed again 16-Aug-26 at Harish's request: "not adding any
# value"). This is the PER-DATE sheet inside each dated workbook -- unrelated
# to AUTO_CONSOLIDATED_REPORT below, which covers the multi-date rollup.
DASHBOARD_ENABLED = True

# Off 15-Aug-26 at Harish's request: a multi-date BACKTEST run (len(dates)
# > 1) no longer auto-builds the consolidated "Backtest <first> to
# <last>.xlsx" + ".html" report at the end (backtest_report.write(), called
# from each run_*.py's main()). The per-date accumulator is still built and
# its console summary still prints -- only the FILE write is skipped. Build
# it yourself, on demand, with:
#     py rebuild_dashboard.py
# which reads whatever dated workbooks are already on disk and produces the
# identical consolidated report -- see that file's own docstring.
AUTO_CONSOLIDATED_REPORT = False

# --------------------------------------------------------------------------
# RUN MODES
# --------------------------------------------------------------------------
LIVE = "LIVE"
BACKTEST = "BACKTEST"

# --------------------------------------------------------------------------
# WATCHLIST
# --------------------------------------------------------------------------
# Sheet in 01_SourceFile.xlsx that drives a run. Harish chose 'Reference'
# (161 symbols). Alternatives present in the file: 'Nifty50', 'Reference (2)'.
WATCHLIST_SHEET = "Reference"

COL_SECTOR = "Sector"
COL_SYMBOL = "Symbol / StrikePrice"
COL_COMPANY = "Company Name"
COL_EXPIRY = "Expiry Date"
COL_STRIKE_STEP = "Option Price Difference"
COL_ZERODHA_TOKEN = "Zerodha_Token"
COL_ANGEL_TOKEN = "Angel_Token"
COL_INSTRUMENT_TYPE = "Instrument Type"

# Day OHLC on the Reference sheet (02-Aug-26, at his request) -- the
# underlying's own opening/closing price for THIS trade date, so the
# watchlist shows which stocks actually moved without cross-referencing the
# matrix sheets. Column order matches his screenshot: ... Token Note,
# Opening, Closing, Change%.
COL_DAY_OPEN = "Opening"
COL_DAY_CLOSE = "Closing"
COL_DAY_CHANGE_PCT = "Change%"

# --------------------------------------------------------------------------
# CANDLES
# --------------------------------------------------------------------------
INTERVAL = "5minute"
INTERVAL_MINUTES = 5

# How much history to pull per symbol. Indicators need a warm-up: RSI(14) and
# ADX(14) are Wilder-smoothed and are still settling for roughly 3x the period.
# Pulling only today's candles produces garbage values for the first hour.
LOOKBACK_DAYS = 30

# Bars discarded before any indicator value is trusted. The binding constraint
# is TW's N-Line, an ema(close, 100) -- a 100-period EMA computed from 30 bars
# is not a 100-period EMA. 150 gives every indicator room to settle.
# At 5-minute candles a session is ~75 bars, so 30 days of lookback (~2250
# bars) leaves this comfortably satisfied.
WARMUP_BARS = 150

MATRIX_LAST_SLOT = "15:15"  # matches the 73 columns in the sample workbook

# --------------------------------------------------------------------------
# RATE LIMITS
# --------------------------------------------------------------------------
# Kite historical endpoint documented limit is 3 requests/second. Staying just
# under it, because bursts get the whole app throttled, not just one call.
KITE_MAX_PER_SECOND = 3
KITE_MAX_WORKERS = 3
ANGEL_MAX_PER_SECOND = 3

REQUEST_TIMEOUT_SECS = 30
MAX_RETRIES = 3
RETRY_BACKOFF_SECS = 2.0

# Angel scrip master is large; refresh weekly rather than every run.
SCRIP_MASTER_MAX_AGE_DAYS = 7

# --------------------------------------------------------------------------
# INDICATOR SETTINGS  -- values taken from Harish's Pine scripts
# --------------------------------------------------------------------------

# --- RSI ---
# Two RSIs are in play and they are NOT the same number:
#   "multi"  = RSI Multi Length [LuxAlgo], the script he first uploaded.
#              Averages RSI over every length from 10 to 20.
#   "wilder" = the classic single-length RSI(14), matching TradingView's
#              built-in ta.rsi() -- and the exact indicator in the new
#              "Relative Strength Index" Pine v6 script (05-Aug-26) that
#              the RSI/EMA9 crossover rule below is built from.
# Switched to "wilder" 05-Aug-26 -- the crossover rule reads a specific
# built-in-RSI pane against its own EMA9, and that pane is Wilder RSI(14),
# not the LuxAlgo average. Running "multi" under an EMA9-cross rule would
# be comparing the EMA of the wrong RSI. Kept switchable, not deleted.
RSI_SOURCE = "wilder"         # "multi" | "wilder"
RSI_MIN_LENGTH = 10           # LuxAlgo input 'Minimum Length'
RSI_MAX_LENGTH = 20           # LuxAlgo input 'Maximum Length'
RSI_PERIOD = 14               # used when RSI_SOURCE == "wilder" (script: rsiLengthInput = 14)
RSI_EMA_LENGTH = 9            # EMA smoothing length on the RSI line itself, for the
                               # cross rule below. Harish's own number, not a script default
                               # (the uploaded script's own smoothing MA defaults to SMA(14)).

# --- EMA + VWAP Ribbon (05-Aug-26) ---
# Script default emaLen=9 ("EMA9 + VWAP Ribbon") -- Harish's own dialog
# screenshot shows 20 in the actual "EMA Length" input, and he said
# explicitly "Ensure EMA is 20 and not 9." 20 wins; script's own default
# is overridden, same as the RSI EMA above overrides that script's SMA(14)
# default. VWAP source hlc3 matches the script's own default (not close).
EMA_VWAP_LENGTH = 20          # overrides the uploaded script's own emaLen=9 default
VWAP_SOURCE = "hlc3"          # "hlc3" | "close" -- script: vwapSrc = input(hlc3)

# --- ADX & DI (BeikabuOyaji) ---
ADX_PERIOD = 14               # script: len = input(14)
ADX_THRESHOLD = 20.0          # script: th  = input(20)

# --- TW All in One ---
TW_LENGTH = 16                # script: length = 16
TW_MODE = "Ehma"              # script: modeSwitch = "Ehma"
TW_NLINE_LENGTH = 100         # script: ema100 = ema(src, 100)

# The uploaded EHMA reads:
#     ema(2 * ema(src, length) - ema(src, length), round(sqrt(length)))
# Both terms use `length`, so 2x - x = x and it collapses to a plain
# ema(ema(src, 16), 4). The standard Hull form uses length/2 in the first
# term. Verified: the two differ by ~10 points on a 1000-level series.
#
# False = reproduce his chart exactly (default, and the correct choice while
#         the Python output is being compared against TradingView)
# True  = use the standard Hull formula, which will NOT match his chart
TW_USE_STANDARD_EHMA = False

# TW ALL rule history, most recent first (see indicators.tw_recommendation
# for the live version):
#   16-Aug-26 (b) -- back to a plain ribbon+N-Line STATE READ, held for as
#     long as the condition holds (not a one-shot event). Real-chart check
#     (ADANIENT 09:55, ohlc4 clearly above both lines) caught BOTH of the
#     event-based versions below still reading WAIT there -- neither fires
#     on a bar that's simply, currently, above the lines without a fresh
#     crossover or confirming 2nd candle AT that bar. This is effectively
#     the same rule as 01-Aug-26, restored.
#   16-Aug-26 (a) -- blended with F:\05_Claude_Automation\05 Codes_OLD's
#     earlier (profitable, per Harish) version: pre-entry was the Hull
#     crossover event itself, entry was just the next candle's close
#     beyond it. Superseded same-day by (b) above.
#   15-Aug-26 -- replaced the triangle gate with ribbon+N-Line pre-entry
#     then a full-OHLC + RSI-trend confirmed 2nd candle. Superseded for
#     being too slow to trigger.
#   01-Aug-26 -- added the N-Line requirement on top of the ribbon alone.

# Skips option_audit.audit_option() entirely for the TW ALL pipeline
# (BACKTEST only -- run_TW_ALL.py's process_date() call), same mechanism
# as MACD_AUDIT_ENABLED below (16-Aug-26, Harish: "refresh the TW
# Indicator... disable all audit checks and keep only OI Change"). The OI
# buildup check (option_audit.oi_buildup_confirms, gated separately by
# OI_BUILDUP_GATE_ENABLED above) runs independently of this flag -- it's
# the one confirmation deliberately kept even with every audit gate off.
TW_ALL_AUDIT_ENABLED = False

# Runs option_audit.oi_buildup_confirms() as its own gate for the TW ALL
# pipeline, in addition to whatever audit_option() gates are (or aren't)
# active -- see TW_ALL_AUDIT_ENABLED above. Off for the other two
# pipelines by default; nothing stops wiring it in there too later.
TW_ALL_OI_BUILDUP_ENABLED = True

ATR_PERIOD = 14

# --------------------------------------------------------------------------
# ENTRY QUALIFICATION
# --------------------------------------------------------------------------
# Final Recomm must show the SAME signal in this many consecutive 5-min
# columns before a symbol qualifies. Reproduces the Pre-Entry / Entry /
# Support trigger trio in the sample workbook's Orders sheet.
#
# Lowered 3 -> 2 (15-Aug-26, Harish): only Pre-Entry + Entry now required,
# Support is no longer part of qualification. Faster to trigger -- catches
# a move one candle earlier -- but see CANDLE_MOMENTUM_ENABLED below, added
# the same day specifically because a faster trigger without a price-action
# check is more exposed to a stall on the very next candle (the BAJFINANCE
# 14-Aug-26 case).
CONSECUTIVE_SIGNALS_REQUIRED = 2

# Price-action confirmation between the two trigger candles above (15-Aug-26,
# Harish, from watching BAJFINANCE on 14-Aug-26 fail: 09:15 printed a strong
# BUY CE candle with long wicks, but 09:20 -- the confirming candle -- closed
# LOWER, and the trade still qualified and then stopped out almost
# immediately). Indicator confluence over 2 candles says the SIGNAL agreed
# twice; it says nothing about whether price itself kept moving. Requires
# the 2nd trigger candle's CLOSE to clear the 1st's, in the signal's
# direction. See signal_quality.candle_momentum_ok.
#
# Originally an ALL-OHLC check (open/high/low/close all clearing), same
# day. That first backtest (14-Aug-26 rerun) showed it was too strict: it
# was the single largest rejection reason (94/411) and it rejected that
# day's only two WINNERS from the prior run (NESTLEIND, ASIANPAINT) for the
# same "no follow-through" reason it correctly caught BAJFINANCE on -- a
# single wick dipping below the prior candle is normal inside a real trend.
# Loosened to close-only the same day.
CANDLE_MOMENTUM_ENABLED = True

# Straddle mode (buy both CE and PE on a qualified signal, flat rupee loss
# cap per leg, RSI-extreme override to single-leg) -- REMOVED 16-Aug-26 at
# Harish's request. Was briefly live 15-Aug-26, BACKTEST only, and never
# reached the LIVE incremental loop. If this needs to come back, the git-
# free history is this conversation; it was order_engine.process_date's
# `if config.STRADDLE_ENABLED:` branch plus Position.fixed_loss_cap_rs /
# confirm_bars_override.

# One order per unbroken run of identical signals. While the signal keeps
# repeating, the position is held -- it is NOT re-entered every candle. A new
# entry is only possible after the run breaks (a WAIT or an opposite signal)
# and a fresh run of CONSECUTIVE_SIGNALS_REQUIRED forms.
ONE_ORDER_PER_SIGNAL_RUN = True

# Capped 01-Aug-26 when Harish asked for the profitability rework. (He had
# earlier chosen no cap; the 83-trade backtest changed the calculus: 21
# trades/day paid ~Rs 5,600/day in friction and the marginal trades were the
# worst ones.) Signals are RANKED within each candle slot and only the best
# taken -- see signal_quality.py.
MAX_CONCURRENT_POSITIONS = 3

# Capped 01-Aug-26 when Harish asked for the profitability rework -- 21
# trades/day paid ~Rs 5,600/day in friction and the marginal trades were the
# worst ones. One order per candle slot, best-ranked signal only.
# Removed 07-Aug-26 per Harish -- no daily entry cap.
MAX_ENTRIES_PER_DAY = None

# --------------------------------------------------------------------------
# SIGNAL QUALITY FILTERS  (added 01-Aug-26 -- see signal_quality.py)
# --------------------------------------------------------------------------
# Volatility: the underlying must actually be moving. ATR(14) at entry must
# be at least this multiple of its own 20-bar average. An ATM option in a
# quiet stock is a theta donation with a brokerage surcharge.
VOL_FILTER_ENABLED = True
VOL_ATR_LOOKBACK = 20
# Bars skipped between "now" and the baseline window, so the baseline is the
# prior regime rather than being contaminated by the very expansion it is
# supposed to detect.
VOL_BASELINE_GAP = 10
VOL_MIN_RATIO = 1.10

# ABSOLUTE momentum floor (added 02-Aug-26). The ratio above only asks "is
# this stock livelier than its own recent past" -- HINDUNILVR at 1.2x its
# usual crawl is still a crawl. In the July backtest, all 15 no-follow-through
# exits were in low-beta names (HUL x3, HDFCBANK x2, ITC, INFY, POWERGRID...)
# whose peaks above entry averaged 1.2%. This floor asks the absolute
# question: is the 5-min ATR, as a fraction of price, big enough that a +7%
# premium move is even plausible inside 20 minutes?
#
# Rough arithmetic behind the default: ATM premium is ~2.5% of spot with
# delta ~0.5, so +7% on the premium needs ~0.35% of spot movement. Over four
# 5-min bars that is ~0.09% per bar. Floor set slightly under, at 0.08%.
VOL_MIN_ATR_PCT = 0.0008          # ATR(14) / price, per 5-min bar

# RSI-extreme rejection (added 06-Aug-26, from Harish's F&O checklist:
# "RSI not extreme against the trade"). Rejects BUY CE when RSI is already
# deep overbought, BUY PE when RSI is already deep oversold -- i.e. don't
# chase a move that's already stretched. See signal_quality.rsi_extreme_ok()
# for the important caveat: the 8-day correlation study behind this change
# found |RSI-50| only weakly correlated with realised P&L (0.10, n=234,
# not distinguishable from noise) -- this is a plausibility guardrail from
# accepted practice, not something the data has proven pays for itself yet.
# 70/30 is the standard textbook overbought/oversold split; loosen to
# 80/20 if it's rejecting too much on genuinely strong trend days.
# Turned OFF 16-Aug-26 at Harish's explicit request ("remove the RSI Check
# during audit, I will ask for it again, if required"). Kept as a flag, not
# deleted -- one line to bring back. See the caveat above: the correlation
# study never proved this gate paid for itself, so removing it isn't
# reversing something known to work, either.
RSI_EXTREME_FILTER_ENABLED = False
RSI_EXTREME_OVERBOUGHT = 75.0     # was 70.0, raised 16-Aug-26 at Harish's request
RSI_EXTREME_OVERSOLD = 20.0       # was 30.0, lowered 15-Aug-26 at Harish's request

# RSI-CHECKPOINT RE-ENTRY (16-Aug-26, Harish -- APOLLOHOSP, 14-Aug-26: RSI
# 78 rejected a BUY CE at 09:20 as "already overbought", but the chart kept
# trending up till 10:35 -- a real move the RSI-extreme gate alone missed).
#
# When rsi_extreme_ok rejects a signal, check the THIRD candle after that
# rejection: if its CLOSE clears the PREVIOUS candle's close, in the
# signal's direction (higher for BUY CE, lower for BUY PE), take the trade
# anyway -- but deliberately smaller, since the RSI gate is still calling
# the move stretched. Uses RSI_CHECKPOINT_REENTRY_RISK_RS as the risk
# budget instead of the normal RISK_PER_TRADE_RS. See
# order_engine._third_candle_rising.
#
# Dormant while RSI_EXTREME_FILTER_ENABLED is False above -- nothing gets
# rejected by that gate for this to react to. No action needed either way;
# it picks back up the moment the gate is re-enabled.
RSI_CHECKPOINT_REENTRY_ENABLED = True
RSI_CHECKPOINT_REENTRY_RISK_RS = 750.0   # vs RISK_PER_TRADE_RS (Rs 2000) normally

# --- INDIA VIX GATE (adopted from F:\06_Claude_v2, VIX_MAX = 18.0) --------
# Per-option IV asks "is THIS contract expensive?". VIX asks "is the whole
# market expensive?" -- they catch different things. A high-VIX day makes
# every long-premium entry a worse bet regardless of which strike you pick,
# because you are paying elevated premium across the board.
#
# Kite token 264969 = India VIX. Gate is skipped, loudly, if the feed is
# unavailable -- never silently passed.
VIX_FILTER_ENABLED = True
INDIA_VIX_TOKEN = 264969
VIX_MAX = 18.0

# Regime: no trades at all when the index itself is chopping. 29-Jul-26 ran
# 22 trades at a 9.1% win rate on a directionless day -- one index-level gate
# would have skipped the whole session. Uses NIFTY 50 spot (Kite token
# 256265), ADX(14) on 5-min candles.
#
# Turned OFF 06-Aug-26 at Harish's call: "I dont think it will help, we
# already have enough indicator to select a symbol." Kept as a flag, not
# deleted -- if a choppy-index day like 29-Jul-26 repeats (many symbols
# showing spurious per-symbol confluence at once, because the noise is
# coming from the index, not the stock), this is one line to bring back.
REGIME_FILTER_ENABLED = False
NIFTY_INDEX_TOKEN = 256265
REGIME_MIN_ADX = 20.0

# Ranking: when several symbols qualify in the SAME candle slot, take only
# the strongest. Ranking never compares across time slots -- a 14:00 signal
# cannot be judged against a 09:40 one at decision time, and doing so in a
# backtest is look-ahead.
#
# Turned OFF 03-Aug-26, at Harish's explicit call: "if a symbol passes 3
# candles [confluence] should be placed in order sheet, if order is not
# placed then give a rejection reason." Ranking was picking one winner per
# slot by ADX/RSI/ATR score and silently discarding every other qualified
# symbol in that slot as "outranked" -- not a real trading constraint, and
# it repeatedly threw away tradeable signals (BAJAJFINSV/KOTAKBANK/LT/
# ADANIENT/INFY/ONGC/EICHERMOT/DRREDDY/WIPRO, 03-Aug-26 live session) to
# make room for a "winner" that then failed anyway.
#
# Turned back ON 03-Aug-26, same day. With ranking off, a 09:15-09:25 slot
# let BAJAJFINSV+LT+ONGC all trade at once (all 3 real, confluence-verified
# signals -- not a bug, RSI was reading them correctly once the backfill
# fix kicked in) and all 3 hit stop loss: -5,516 vs the ranked run's
# -381 on the same slot. Harish's call: bring the cap back, he'll fix the
# INFY/LT strike-step values in the source file himself (found wrong same
# session: INFY 1145 isn't a real strike, should compute 1140; LT 4010
# isn't real, should compute 4000). Kept as a flag either way -- this is
# one line to flip if the trade-off looks different again later.
# Turned back OFF again 07-Aug-26 per Harish, despite the 03-Aug-26 incident
# above -- his call, made with that history in view.
RANK_WITHIN_SLOT_ENABLED = False
RANK_PER_SLOT = 1                 # best N signals per 5-min slot, only used
                                   # when RANK_WITHIN_SLOT_ENABLED is True

# Never two open positions in the same underlying.
ONE_POSITION_PER_SYMBOL = True

# --------------------------------------------------------------------------
# OPTION SELECTION
# --------------------------------------------------------------------------
# Strikes fetched either side of ATM. +/-2 means 5 strikes per side of the
# chain, which is what keeps the download small.
OPTION_STRIKE_WINDOW = 2

# Which expiry to trade. "nearest" uses the closest expiry on or after today;
# "sheet" honours the Expiry Date column in the Reference sheet.
OPTION_EXPIRY_MODE = "sheet"      # "sheet" | "nearest"

# Refuse to buy an option with fewer than this many days to expiry. Theta
# accelerates hard in the final sessions and an intraday buyer wears all of it.
MIN_DAYS_TO_EXPIRY = 2

# --------------------------------------------------------------------------
# RISK AND EXITS  (levels are multiples of entry LTP)
# --------------------------------------------------------------------------
RISK_PER_TRADE_RS = 2000.0        # flat rupee budget, as in the sample workbook

# The "Capital Shadow" sheet (09-Aug-26, Harish) re-simulates a run that
# failed purely on RISK_PER_TRADE_RS or available capital at exactly 1 lot
# -- the smallest real trade -- rather than substituting a bigger budget.
# (An earlier version used a large substitute budget; risk_budget //
# tiny_risk_per_lot produced lot counts in the thousands and P&L in the
# lakhs/crores, not a meaningful answer to "what if the budget were a bit
# bigger.") See order_engine._try_build_position's `ignore_capital` branch.

# --- PAYOFF STRUCTURE, recalibrated 01-Aug-26 from the 83-trade backtest ---
#
# The old ladder was symmetric: -10% stop, +10% first target. Symmetric
# payoff needs >51% wins; the measured win rate was 25%. The move data said
# why: only 14% of trades ever reached +10%, T3 (+35%) hit ZERO times in 83
# trades, winners peaked at +10.8% and losers at +1.8%.
#
# New ladder is asymmetric and sized to the moves that actually happened:
#   stop  -7%  (losers rarely ran further before the exit rules fired anyway)
#   T1    +7%  sell 50%   T2  +12%  sell 25%   T3  +20%  sell 25%
# Full winner = 0.5*7 + 0.25*12 + 0.25*20 = +11.5% vs -7% loss.
# Breakeven win rate drops from ~51% to ~38% before costs, ~40% after.
#
# THIS IS A HYPOTHESIS, NOT A RESULT. Nothing here is proven until the
# multi-session backtest is re-run. The old values are kept below, commented,
# so an A/B comparison is one edit away.
STOP_LOSS_MULT = 0.93             # -7%   (was 0.90); fallback when ATR is absent
TARGET_MULTS = (1.07, 1.12, 1.20) # (was 1.10, 1.20, 1.35 -- T3 never hit)
TARGET_EXIT_FRACTIONS = (0.50, 0.25, 0.25)

# --- ATR-ADAPTIVE STOP (adopted from F:\06_Claude_v2, 02-Aug-26) ----------
# A fixed percentage stop is the same distance in a sleepy stock and a wild
# one, which is wrong in both. The old model sized the stop off the
# UNDERLYING's ATR and converted it to premium terms via ATM delta:
#
#     stop_distance = clamp(ATR_MULT * underlying_ATR * ATM_DELTA,
#                           MIN_STOP_PCT * entry, MAX_STOP_PCT * entry)
#
# The clamp matters as much as the formula: without a floor you get a stop
# so tight that noise takes you out, without a ceiling a volatile name
# swallows the whole risk budget in one trade.
#
# Their floor was 10% and ceiling 35%. Ours are tighter because our targets
# are tighter (+7/+12/+20 vs their single +2R) -- a 35% stop against a 7%
# first target is a payoff ratio no win rate can rescue.
USE_ATR_STOP = True
ATR_STOP_MULTIPLIER = 1.5
ATM_DELTA_APPROX = 0.5            # ATM option moves ~half the underlying
MIN_STOP_PCT = 0.05               # never tighter than 5% of premium
MAX_STOP_PCT = 0.12               # never wider than 12% of premium

# After T1 fills, the stop moves to entry. The trade can no longer lose.
MOVE_SL_TO_BREAKEVEN_AT_T1 = True

# Then trail this far below the peak LTP seen since entry. Tightened to match
# the new stop distance -- a 10% trail on a +7% first target gave back the
# whole target before triggering.
TRAILING_STOP_MULT = 0.93         # 7% below peak (was 0.90)

# Bars closed beyond the trail before it is honoured. Raised 1 -> 2 from the
# old model: a single wick through the trail is noise, and exiting on it
# donates the position to whoever wicked it.
TSL_BREACH_CONFIRM_BARS = 2

# Costs counted inside the risk budget rather than subtracted afterwards.
# See the note in order_sheet.size_position -- without this the stated cap
# is breached on every stop-out by roughly the cost of the round trip.
COST_AWARE_RISK_CAP = True

# --- DAILY LOSS CIRCUIT BREAKER (adopted from F:\06_Claude_v2) ------------
# There was no daily kill switch anywhere in our pipeline. The old model's
# audit flagged the same gap in theirs and added one. Once realised losses
# for the session reach this, no new entries are taken -- open positions
# still manage themselves to their own exits.
DAILY_MAX_LOSS_RS = 6000.0        # 3x the per-trade budget

# No entries after this time (IST). Was silently missing -- the engine
# guarded it with hasattr() and skipped the check, so a 14:55 signal got
# entered with 20 minutes of session left.
# Widened 14:00 -> 15:00 on 06-Aug-26 at Harish's call ("Keep the cut-off
# till 3pm, instead of 2pm"). EOD square-off is 15:15, so a 15:00 entry now
# has 15 minutes -- three 5-min candles -- to work. That is thin: such a
# trade is far likelier to close on EOD Square-off than on T1/T2/T3, so
# expect more small forced exits in the tail of the day. One line to move
# back if the 14:00-15:00 window turns out to be where the losses cluster.
NO_NEW_ENTRY_AFTER = "15:00"

# Time-based exits, carried over from the sample workbook's exit reasons.
#
# DISABLED (02-Aug-26, at his request). CIPLA, 29-Jul-26: entered 33.20,
# No-Follow-Through correctly measured only 0.38R by the 20-minute mark and
# cut the trade at -Rs 236.93 -- exactly as designed. The underlying kept
# climbing anyway: the same contract closed 47.50 by 11:00, past Target 3
# (41.46). The rule wasn't broken here (that was a SEPARATE bug, fixed
# separately in note_bar_range) -- it did exactly what a 20-minute/0.5R
# timeout is supposed to do, and that was still the wrong call often enough
# that he wants it gone. Exits now come from SL, TSL, and Targets only, plus
# the hard EOD square-off (that one is not optional -- an intraday paper
# position cannot be held overnight).
#
# Flags kept, not deleted: a single-line revert if the data says otherwise.
ENABLE_NO_FOLLOW_THROUGH_EXIT = False
ENABLE_MAX_HOLD_EXIT = False

NO_FOLLOW_THROUGH_MINS = 20       # flat after this long
NO_FOLLOW_THROUGH_R = 0.5         # "flat" = less than this many R in profit
MAX_HOLD_MINS = 75

# --------------------------------------------------------------------------
# OPTION AUDIT GATES
# --------------------------------------------------------------------------
# Liquidity and tradeability.
MIN_OPTION_LTP = 5.0              # below this the tick is a large % of premium
MAX_SPREAD_PCT = 0.05             # bid-ask wider than 5% of LTP -> reject
MIN_OPTION_VOLUME = 100

# Open-interest confirmation (added 07-Aug-26, Harish). LIVE only -- see
# option_audit.oi_confirms for why BACKTEST can't honestly run this (no
# historical OI series, only a live snapshot). Snapshot composition, not a
# shift over time: call_oi / (call_oi + put_oi) across the +/-N strike
# window must clear this share for a BUY CE, and the mirror for BUY PE.
# UNCALIBRATED -- 0.55 is a starting guess, not measured against real
# trades yet. Watch Orders/Missed_Concurrent's OI columns before trusting
# this number the way MAX_COST_TO_T1_GAIN or VOL_MIN_RATIO are trusted.
OI_GATE_ENABLED = True
OI_MIN_OWN_SIDE_SHARE = 0.55

# OI CHANGE / buildup confirmation (16-Aug-26, Harish: "keep only OI
# Change (Long Buildup etc) for all Rise in Price and Rise in OI"). Unlike
# OI_GATE_ENABLED above (a live snapshot of composition), this compares
# the contract's own price+OI now against OI_BUILDUP_LOOKBACK_MINUTES
# earlier on its own candle history -- works in BACKTEST too, since
# option_data.fetch_option_history_kite now requests real historical OI
# per bar. See option_audit.oi_buildup_confirms for the full quadrant
# logic and the backtest evidence behind excluding SHORT_COVERING.
OI_BUILDUP_GATE_ENABLED = True
OI_BUILDUP_LOOKBACK_MINUTES = 5

# Greeks/IV capture (09-Aug-26). NOT a gate -- nothing reads these yet, they
# are captured for future strategy work only (option_chain.fetch_greeks,
# quote_history.py). LIVE only, and only for a run that already qualified
# (3-bar confluence, cleared the cheap pre-filters) -- one extra API call
# per real candidate, not per watchlist symbol per cycle.
GREEKS_FETCH_ENABLED = True

# Cost viability. THE most important gate given the 31-Jul-26 numbers:
# HDFCLIFE made +Rs 55 gross and -Rs 102 net. If the estimated round-trip cost
# eats more than this share of the T1 gain, the trade cannot pay for itself
# and is refused before it is taken.
MAX_COST_TO_T1_GAIN = 0.35

# If the live quote and the option candle's open disagree by more than this,
# one of the two data sources is wrong. Refuse the trade rather than pick.
# Measured cause of the Rs 94,207 HINDUNILVR loss on 28-Jul-26: the quote read
# ~7.70 (which sized 8 lots) while the candle opened at 54.85 (which filled
# them). Size and fill must agree or the position size is meaningless.
MAX_QUOTE_FILL_DRIFT = 0.25

# An option premium cannot realistically lose this fraction of its value
# inside one 5-minute candle. Beyond it, the bar is a bad tick, not a move.
# HINDUNILVR printed a low of 15.95 against an open of 54.85 -- a 71% collapse
# on an FMCG name -- and the backtest treated it as a real fill.
MAX_INTRABAR_COLLAPSE = 0.40

# --------------------------------------------------------------------------
# TRANSACTION COSTS  (NSE equity options, buying)
#
# CALIBRATED against the 14 real trades in 31-Jul-26 FNO-L.xlsx. That
# workbook's Costs column works out to 0.654% of round-trip turnover, and it
# is astonishingly constant -- 0.652% to 0.656% across every trade.
#
# Decomposing one of them (HDFCLIFE, buy Rs 11,990 / sell Rs 12,045):
#     brokerage 40.00 + exchange 12.02 + STT 12.04 + stamp 0.36 + GST 9.37
#       = Rs 73.81  statutory
#     workbook actual = Rs 157.07
#     gap = Rs 83.26 = 0.346% of turnover
# The gap is slippage, already folded into that Costs column. So the rates
# below reproduce the full observed figure, not just the statutory part.
#
# TODO: verify against a real Angel One contract note. Until then these are
# calibrated assumptions, and P/L is only as honest as they are.
# --------------------------------------------------------------------------
BROKERAGE_PER_ORDER = 20.0        # Angel One flat fee per executed order
STT_SELL_PCT = 0.0015             # 0.15% on sell-side premium.
                                  # Corrected from 0.10% on 02-Aug-26: the
                                  # old model's cost block cites a post
                                  # Apr-2026 STT hike. Ours was stale.
EXCHANGE_TXN_PCT = 0.0005         # 0.05% on premium turnover (options rate,
                                  # NOT the 0.03503% futures rate)
SEBI_CHARGES_PCT = 0.000001
STAMP_DUTY_BUY_PCT = 0.00003
GST_PCT = 0.18                    # on brokerage + exchange + SEBI

# Slippage per side, least-squares fitted to the workbook's 14 trades with
# the Rs 20/order brokerage included. Fit quality: worst error 8.3%, mean
# 4.3%, total across all 14 within 0.9%.
#
# Note the workbook's own cost model had NO flat brokerage -- it was a clean
# 0.654% of turnover at every trade size. Angel One does charge Rs 20 per
# executed order, so that is included here, which makes this model slightly
# more expensive than the workbook's on small trades and slightly cheaper on
# large ones. Erring toward more expensive is the right direction.
#
# Do not set this to zero. An options book is not tight, and a market order
# at a signal moment is filled by someone who knows that.
SLIPPAGE_PCT_PER_SIDE = 0.003870

# All-in cost as a fraction of round-trip turnover, measured from the
# workbook. Used as a sanity target by the self-test.
OBSERVED_ALL_IN_COST_PCT = 0.00654

# --------------------------------------------------------------------------
# LIVE SESSION TIMING
# --------------------------------------------------------------------------
# A 5-minute candle opening at 09:15 closes at 09:20:00. Data is only fetched
# at 09:20:05 -- five seconds later -- so the broker has definitely written
# the closed bar. Nothing in this system ever reads a forming candle, in
# LIVE or in BACKTEST.
CANDLE_CLOSE_BUFFER_SECS = 5
LIVE_POLL_TOLERANCE_SECS = 2      # how close to the mark the loop must wake


@dataclass(frozen=True)
class SignalRules:
    """
    Rules as stated by Harish on 31-Jul-26, RSI rule replaced 05-Aug-26.

    RSI  -- CURRENT (05-Aug-26): "RSI line to cross over EMA 9 line. If RSI
             (blue) is above EMA9 (yellow) then BUY CE. If RSI is below
             EMA9 then BUY PE. Else WAIT." rsi_mode = "ema_cross".
             Superseded rule, kept working, not deleted:
               "direction" (31-Jul-26) -- "RSI should have increasing
               values in 5-min candle": rising vs previous closed candle =
               BUY CE, falling = BUY PE.
               "level" -- RSI vs a fixed upper/lower band.

    ADX  -- "ADX & DI combination should give ADX Recomm". Trend strength
             gates, direction picks the side: ADX >= threshold and DI+ > DI-
             is BUY CE, ADX >= threshold and DI- > DI+ is BUY PE, otherwise
             WAIT. Threshold 20 comes from `th = input(20)` in his script.

    TW   -- "if the candle is above BLUE Ribbon, then BUY CE. If candle is
             below RED Ribbon then BUY PE. Else, WAIT."
    """

    rsi_mode: str = "ema_cross"      # "ema_cross" | "direction" | "level"
    rsi_upper: float = 60.0          # only used when rsi_mode == "level"
    rsi_lower: float = 40.0          # only used when rsi_mode == "level"
    rsi_flat_tolerance: float = 0.0  # movement smaller than this counts as WAIT (rsi_mode == "direction")

    # Raised from the script's th=20 on 01-Aug-26: ADX 20 is the *boundary*
    # of "no trend" and passed almost everything -- the 83-trade backtest ran
    # a 25% win rate through it. 25 is the conventional trend-entry floor.
    # The TW ALL sheet still displays the script's own 20 line; this gate is
    # about what we TRADE, not what the chart draws.
    adx_min: float = 25.0

    # Final Recomm: how many components must agree.
    # "all" reproduces the sample workbook, where any disagreement gave WAIT.
    confluence: str = "all"          # "all" | "majority"


RULES = SignalRules()

# --------------------------------------------------------------------------
# MATRIX SHEETS
# --------------------------------------------------------------------------
# EMA VWAP REMOVED (16-Aug-26, Harish: "keep only TW, RSI & ADX indicator
# and Final & Order sheet"). Was already hidden from the workbook since
# 15-Aug-26 (EMA_VWAP_SHEET_ENABLED=False); now dropped from computation
# and Final Recomm's confluence entirely, not just the display. Final
# Recomm is back to the original 3-way TW ALL / RSI / ADX vote.

# Which sheets get built, and the metric rows each one carries.
MATRIX_SHEETS: dict[str, list[str]] = {
    "TW ALL": ["Close", "N-Line", "MHULL", "SHULL", "TREND", "TW ALL Recomm"],
    "RSI": ["Close", "RSI", "RSI EMA9", "RSI Recomm"],
    "ADX": ["DI+", "DI-", "ADX", "ADX Recomm"],
}

FINAL_SHEET_NAME = "Final"
FINAL_ROWS = ["TW ALL Recomm", "RSI Recomm", "ADX Recomm", "Final Recomm"]

SIGNAL_BUY_CE = "BUY CE"
SIGNAL_BUY_PE = "BUY PE"
SIGNAL_WAIT = "WAIT"

# --------------------------------------------------------------------------
# PIPELINE MACD  (run_MACD.py, 13-Aug-26, Harish's request)
# --------------------------------------------------------------------------
# Same idea as MATRIX_SHEETS/FINAL_ROWS above, but for the second pipeline:
# TW ALL is DROPPED and MACD (macd.py) takes its place as a confluence
# component alongside RSI. Kept as separate constants, not edits to
# MATRIX_SHEETS/FINAL_ROWS above, so run_TW_ALL.py's original behaviour is
# completely untouched.
#
# ADX and EMA VWAP REMOVED (15-Aug-26, Harish): stripping the MACD pipeline
# down to RSI + MACD only, straight from Final Recomm to the order sheet --
# no option_audit gates either, see MACD_AUDIT_ENABLED below. Both are
# being rebuilt step by step from here rather than carried over from the
# original pipeline's tuning.
MACD_FAST_LENGTH = 12          # matches MACD.txt's own default
MACD_SLOW_LENGTH = 26
MACD_SIGNAL_LENGTH = 9
MACD_OSC_MA_TYPE = "EMA"       # "EMA" | "SMA"
MACD_SIGNAL_MA_TYPE = "EMA"    # "EMA" | "SMA"

# Skips option_audit.audit_option() entirely for the MACD pipeline (BACKTEST
# only -- run_MACD.py's process_date() call) -- spread/liquidity/min-premium/
# days-to-expiry/cost-viability/funding/OI all bypassed, straight from Final
# Recomm to a filled order. Re-enable gate by gate as they're worked through;
# does not touch run_TW_ALL.py, which always audits. The basic "is there a
# usable quote at all" check still applies -- that's data availability, not
# a quality gate, and the pipeline can't size or fill without it.
MACD_AUDIT_ENABLED = False

MATRIX_SHEETS_V2: dict[str, list[str]] = {
    # RSI EMA9 dropped (15-Aug-26): RSI Recomm here is the 50-midline rule
    # (matrix_sheets_v2.rsi_midline_recommendation), not the ema_cross rule
    # -- RSI EMA9 doesn't feed it, so it's not shown to avoid implying it does.
    "RSI": ["Close", "RSI", "RSI Recomm"],
    "MACD": ["Close", "MACD", "Signal", "Hist", "MACD Recomm"],
}

# Confluence rule (15-Aug-26, Harish):
#   RSI  : above 50 = BUY CE, below 50 = BUY PE -- the uploaded "Relative
#          Strength Index" script's own 'RSI Middle Band' hline(50), not
#          the ema_cross rule run_TW_ALL.py's RSI still uses. See
#          matrix_sheets_v2.rsi_midline_recommendation.
#   MACD : MACD line (blue) above Signal line (orange) AND histogram is
#          bright green -- hist >= 0 AND rising (macd.histogram_color()
#          == "teal"). A positive-but-fading histogram ("pale_teal") does
#          NOT confirm -- momentum has to actually be building, not just
#          be on the right side of zero. Mirror for BUY PE. See
#          matrix_sheets_v2.macd_recommendation.
FINAL_ROWS_V2 = ["RSI Recomm", "MACD Recomm", "Final Recomm"]

# --------------------------------------------------------------------------
# PIPELINE EMA-PIVOT  (run_EMA_PIVOT.py, 15-Aug-26, Harish's request)
# --------------------------------------------------------------------------
# Third pipeline, TW ALL and MACD both dropped: EMA10 + the daily Pivot
# Point are the whole confluence.
#
#   Long  (BUY CE): close crossing/above EMA10 AND close above the Pivot
#                   (P), at the same time.
#   Short (BUY PE): mirror -- close below EMA10 AND close below Pivot.
#
# "Long"/"Short" map to this project's existing BUY CE/BUY PE convention
# (bullish bet / bearish bet), same as every other pipeline. See
# matrix_sheets_ema_pivot.ema_pivot_recommendation.
EMA_PIVOT_EMA_LENGTH = 10       # overrides EMA.txt's own default of 9,
                                # per Harish's explicit "EMA 10"

# Audit checks off, straight from Final Recomm to the order sheet -- same
# starting point as MACD_AUDIT_ENABLED, this pipeline is also being built
# up step by step. BACKTEST only (order_engine.process_date).
EMA_PIVOT_AUDIT_ENABLED = False

MATRIX_SHEETS_EMA_PIVOT: dict[str, list[str]] = {
    "EMA-Pivot": ["Close", "EMA10", "Pivot P", "Pivot R1", "Pivot S1",
                 "EMA-Pivot Recomm"],
}

# Single-component confluence -- the rule itself is one combined AND
# condition (see ema_pivot_recommendation), not several independent
# components voted together the way the other two pipelines' FINAL_ROWS
# are. Final Recomm trivially equals EMA-Pivot Recomm.
FINAL_ROWS_EMA_PIVOT = ["EMA-Pivot Recomm", "Final Recomm"]

# --------------------------------------------------------------------------
# AUTHENTICATION
# --------------------------------------------------------------------------
# How Zerodha gets a fresh access_token when the cached one is stale.
#   "selenium" -- drive the browser through login + TOTP automatically
#   "manual"   -- print the login URL and wait for a pasted redirect URL
#
# Angel One is unaffected: SmartAPI has no browser step, so it always uses the
# direct generateSession() + pyotp call, which is already fully automatic.
ZERODHA_AUTH_METHOD = "selenium"    # "selenium" | "manual"

# Fall back to the manual paste if the browser login fails. Set False to make
# a Selenium failure hard-stop the run instead -- better for scheduled jobs,
# where there is nobody present to paste anything.
SELENIUM_FALLBACK_TO_MANUAL = True

# headless=False shows the browser. Keep it visible for the first few runs:
# when a selector breaks, watching it fail tells you more than any log line.
SELENIUM_HEADLESS = False
SELENIUM_TIMEOUT_SECS = 45

# --------------------------------------------------------------------------
# CACHING
# --------------------------------------------------------------------------
# Download once, reuse all day. A cached CSV is only re-fetched if it is
# missing, or shorter than expected, or contains candles after the requested
# cutoff (which means it was written by a different run mode).
REUSE_CACHE = True
FORCE_REFRESH = False  # set True for one run to rebuild every cache file


def describe_rules() -> str:
    ehma = "STANDARD Hull" if TW_USE_STANDARD_EHMA else "as-written (matches his chart)"
    return (
        "Signal rules\n"
        f"  TW ribbon    : close above blue band = CE, below red band = PE, "
        f"else WAIT\n"
        f"  TW hull      : {TW_MODE}({TW_LENGTH}) {ehma}, N-Line ema({TW_NLINE_LENGTH})\n"
        f"  RSI          : {RSI_SOURCE}"
        + (f" ({RSI_MIN_LENGTH}-{RSI_MAX_LENGTH})" if RSI_SOURCE == "multi"
           else f"({RSI_PERIOD})")
        + f", {RULES.rsi_mode}"
        + (f" (RSI above EMA{RSI_EMA_LENGTH} = CE, below = PE)"
           if RULES.rsi_mode == "ema_cross"
           else " (rising = CE, falling = PE)" if RULES.rsi_mode == "direction"
           else f" (upper {RULES.rsi_upper} / lower {RULES.rsi_lower})")
        + f"\n  ADX gate     : >= {RULES.adx_min} then DI+/DI- picks the side\n"
        f"  EMA-VWAP     : close above EMA{EMA_VWAP_LENGTH} & VWAP({VWAP_SOURCE}) = CE, "
        f"below both = PE, else WAIT\n"
        f"  Confluence   : {RULES.confluence} of "
        f"{len(FINAL_ROWS) - 1} components must agree"
    )


if __name__ == "__main__":
    print(describe_rules())

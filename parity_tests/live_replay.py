"""Replay a past date through advance_live_day cycle by cycle (PAPER, fake clock)
and compare against process_date. python live_replay.py <codedir> <DD-Mon-YY>"""
import os, sys, shutil
from datetime import timedelta
codedir, stamp = sys.argv[1], sys.argv[2]
sys.path.insert(0, codedir); os.chdir(codedir)
import config, paths, file_mgmt, ist_clock, angel_scrip, matrix_sheets_harish
import data_ingestion, order_engine as oe, quote_history, token_mgmt
import pandas as pd
config.CONSECUTIVE_SIGNALS_REQUIRED = config.HARISH_CONSECUTIVE_SIGNALS_REQUIRED
config.TSL_BREACH_CONFIRM_BARS = config.HARISH_TSL_BREACH_CONFIRM_BARS
config.BACKTEST_RESTRICT_TO_LIVE_ROWS = config.HARISH_BACKTEST_RESTRICT_TO_LIVE_ROWS
config.TARGET_FILL_AT_LEVEL = config.HARISH_TARGET_FILL_AT_LEVEL
config.LIVE_TRADING = False          # PAPER book -> same simulator as BACKTEST
import builtins; _p = builtins.print
builtins.print = lambda *a, **k: None if (a and isinstance(a[0], str) and a[0].startswith(("[quote-history]", "[chain]", "[data]"))) else _p(*a, **k)

class NoKite:
    def __getattr__(self, n):
        def f(*a, **k): raise RuntimeError(f"offline: kite.{n}")
        return f
kite = NoKite()
d = pd.to_datetime(stamp, format="%d-%b-%y").date()
wb = __import__("pathlib").Path(os.path.expanduser(f"~/work/replay_{stamp}.xlsx"))
shutil.copy(paths.BASE_DIR / f"{stamp} FNO-BT-HARISH.xlsx", wb)
ref = file_mgmt.read_reference_sheet(wb, mode=config.BACKTEST)
scrip = angel_scrip.load_scrip_master()
ref = angel_scrip.resolve_angel_tokens(ref, scrip)
full = {}
for s in ref[config.COL_SYMBOL].astype(str):
    x = data_ingestion.load_interval_data(s, config.INTERVAL, d)
    if x is not None: full[s] = x
final_df = matrix_sheets_harish.build_all_sheets_harish(wb, full, d)[config.FINAL_SHEET_NAME]
slots = ist_clock.candle_slots(d, config.INTERVAL_MINUTES, pd.Timestamp(config.MATRIX_LAST_SLOT).time())

# ---- BACKTEST reference -----------------------------------------------------
import pickle, time
T0 = time.time()
BTP = os.path.expanduser(f"~/work/bt_{stamp}.pkl")
if os.path.exists(BTP):
    bt = pickle.load(open(BTP, "rb"))
else:
    bt, *_ = oe.process_date(final_df, ref, full, scrip, None, d, config.BACKTEST, slots,
                             kite=kite, audit_enabled=False)
    pickle.dump(bt, open(BTP, "wb"))
_p("bt done", round(time.time() - T0), flush=True)

# ---- LIVE replay --------------------------------------------------------------
NOW = [None]
import datetime as _dt
_real_today = ist_clock.today_ist()
ist_clock.now_ist = lambda: NOW[0]
ist_clock.today_ist = lambda: _real_today   # keep option_data treating 15-Sep as a PAST date (full cache)
INT = timedelta(minutes=config.INTERVAL_MINUTES)
orig_fetch = oe._fetch_opt_candles
_cache = {}
def full_opt(contract, symbol, opt_type, nfo):
    k = (contract.trading_symbol,)
    if k not in _cache:
        _cache[k] = orig_fetch(contract, symbol, opt_type, d, None, kite, nfo, None)
    return _cache[k]
def fake_fetch(contract, symbol, opt_type, trade_date, angel, kite_, nfo, cutoff):
    df, src = full_opt(contract, symbol, opt_type, nfo)
    at = getattr(contract, "_pending_at", None)
    if at is not None:   # live LTP a few seconds after `at` ~ open of the bar opening at `at`
        contract.ltp = float(df.loc[at, "open"]) if (df is not None and at in df.index) else None
        del contract._pending_at
    if df is None: return None, None
    df = df[df.index + INT <= NOW[0]]
    return (df if not df.empty else None), src
oe._fetch_opt_candles = fake_fetch
import option_data
def _wrap(fn):
    def w(*a, cutoff=None, **k):
        df = fn(*a, cutoff=None, **k)
        if df is None or df.empty or cutoff is None: return df
        return df[df.index + INT <= cutoff]
    return w
option_data.fetch_option_history_kite = _wrap(option_data.fetch_option_history_kite)
option_data.fetch_option_candles = _wrap(option_data.fetch_option_candles)
orig_rq = quote_history.resolve_and_quote
def fake_rq(symbol, spot, step, scrip_, trade_date, mode, at, angel, lim, **kw):
    chain = orig_rq(symbol, spot, step, scrip_, trade_date, config.BACKTEST, at, angel, lim, **kw)
    if chain is None: return None
    for c in chain.calls + chain.puts:     # live LTP set lazily in fake_fetch (ATM only)
        c._pending_at = at
    return chain
quote_history.resolve_and_quote = fake_rq
quote_history.record_chain = lambda *a, **k: None

state = oe.new_live_state()
t = ist_clock.combine_ist(d, ist_clock.MARKET_OPEN) + INT
end = ist_clock.combine_ist(d, ist_clock.MARKET_OPEN.replace(hour=15, minute=15))
lv = None
while t <= end:
    NOW[0] = t + timedelta(seconds=5)
    cut = {s: x[x.index + INT <= NOW[0]] for s, x in full.items()}
    lv, rj, *_ = oe.advance_live_day(state, final_df, ref, cut, scrip, None, d, config.LIVE,
                                     slots, kite=kite, audit_enabled=False)
    t += INT
    _p("cycle", NOW[0].strftime("%H:%M:%S"), round(time.time() - T0), flush=True)
builtins.print = _p
cols = ["Symbol", "Signal", "Entry Type", "Entry Time", "Entry LTP", "Exit Time", "Exit Reason", "Net P/L (Rs)"]
def norm(o):
    o = o[cols].copy(); o["Net P/L (Rs)"] = o["Net P/L (Rs)"].astype(float).round(2)
    return o.sort_values(["Entry Time", "Symbol"]).reset_index(drop=True)
a, b = norm(bt), norm(lv)
print("\n==== BACKTEST", stamp); print(a.to_string())
print("\n==== LIVE REPLAY", stamp); print(b.to_string())
same = a.astype(str).equals(b.astype(str))
print("\nIDENTICAL:", same, "| BT net", round(a["Net P/L (Rs)"].sum(), 2), "| LIVE net", round(b["Net P/L (Rs)"].sum(), 2))

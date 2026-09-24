import os, sys
sys.path.insert(0, os.getcwd())
from datetime import date, timedelta
import pandas as pd
import config, ist_clock, order_engine as oe, order_sheet as osh
from types import SimpleNamespace

config.TSL_BREACH_CONFIRM_BARS = 1
config.TARGET_FILL_AT_LEVEL = True
config.EARLY_CANDLE_TIGHTEN_ENABLED = True
D = date(2026, 9, 15)
def T(h, m): return ist_clock.combine_ist(D, ist_clock.MARKET_OPEN.replace(hour=h, minute=m))
SQ = T(15, 15)
final_df = pd.DataFrame()  # no signal rows -> invalidation never fires
oe._signal_invalidation_reason = lambda *a, **k: ""

def mkpos(entry=100.0, sig=config.SIGNAL_BUY_CE, t=T(9, 25)):
    c = SimpleNamespace(trading_symbol="TEST", option_type="CE")
    return osh.Position(symbol="TEST", signal=sig, contract=c, entry_time=t,
                        entry_ltp=entry, quantity=100, lots=4, lot_size=25)
def bar(o, h, l, c): return pd.Series({"open": o, "high": h, "low": l, "close": c})
fails = 0
def check(name, cond, info=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name, info)
    fails += (not cond)

# 1. wick through stop, close above -> broker-touch stop, filled AT the stop
p = mkpos(); stop = p.stop_loss
oe._simulate_bar(p, pd.Timestamp(T(9, 30)), bar(100, 101, stop - 1, 100.5), final_df, {}, SQ)
check("wick through stop exits on first touch", p.closed and p.exit_reason == "Stop Loss Hit"
      and abs(p.exits[-1]["ltp"] - stop) < 1e-9, f"stop={stop} exits={p.exits}")

# 2. target on the high tick fills AT the target level, not the bar high
p = mkpos(); t1 = p.targets[0]
oe._simulate_bar(p, pd.Timestamp(T(9, 30)), bar(100.5, t1 + 5, 99.5, t1 + 1), final_df, {}, SQ)
check("T1 partial fills at the level", p.target_hit[0] and abs(p.exits[0]["ltp"] - t1) < 1e-9,
      f"t1={t1} exits={[e['ltp'] for e in p.exits]}")

# 3. gap over T1 -> fill at min(low-tick, open)
p = mkpos(); t1 = p.targets[0]
oe._simulate_bar(p, pd.Timestamp(T(9, 30)), bar(t1 + 3, t1 + 6, t1 + 2, t1 + 4), final_df, {}, SQ)
check("gap over T1 fills at the low tick (<= open)", abs(p.exits[0]["ltp"] - (t1 + 2)) < 1e-9)

# 4. EOD bar squares off at the CLOSE, not the low
p = mkpos(t=T(14, 0))
oe._simulate_bar(p, pd.Timestamp(T(15, 10)), bar(100, 102, 98, 101), final_df, {}, SQ)
check("EOD at 15:10-bar close", p.closed and p.exit_reason == "EOD Square-off"
      and p.exits[-1]["ltp"] == 101, str(p.exits))

# 5. tighten: fill candle skipped, first candle after it decides, effective NEXT bar
und = pd.DataFrame({"open": [1000, 1000, 1004], "high": [1003, 1006, 1010],
                    "low": [995, 994, 1000], "close": [1001, 996, 1008]},
                   index=pd.DatetimeIndex([T(9, 25), T(9, 30), T(9, 35)]))
p = mkpos(entry=100.0)  # BUY CE, fill candle 09:25
base_stop = p.stop_loss
oe._simulate_bar(p, pd.Timestamp(T(9, 25)), bar(100, 101, 99.5, 100.5), final_df, {"TEST": und}, SQ)
check("fill candle does not trigger tighten", not p.first_candle_checked and not p.closed)
# 09:30 underlying bearish (996<1000); prev low 995 -> dist 1 -> tighter = 99.5
oe._simulate_bar(p, pd.Timestamp(T(9, 30)), bar(100.5, 101, 99.0, 99.8), final_df, {"TEST": und}, SQ)
check("bar that decides tighten is NOT stopped by its own low", not p.closed,
      f"stop {base_stop}->{p.stop_loss}")
check("stop tightened after the bar", p.stop_loss > base_stop and p.first_candle_checked, f"{p.stop_loss}")
oe._simulate_bar(p, pd.Timestamp(T(9, 35)), bar(99.8, 100.2, 99.4, 100), final_df, {"TEST": und}, SQ)
check("next bar hits tightened stop", p.closed and "Stop" in p.exit_reason, f"{p.exit_reason} {p.exits}")

# 6. tighten with close already below new stop -> exit at close
p = mkpos(entry=100.0)
und2 = und.copy(); und2.loc[T(9, 30), "close"] = 990   # dist 5 -> tighter = 97.5
oe._simulate_bar(p, pd.Timestamp(T(9, 25)), bar(100, 101, 99.5, 100.5), final_df, {"TEST": und2}, SQ)
oe._simulate_bar(p, pd.Timestamp(T(9, 30)), bar(100, 100.5, 97.0, 97.2), final_df, {"TEST": und2}, SQ)
check("close below freshly tightened stop exits at close", p.closed and p.exit_reason ==
      "Stop Loss Hit (tightened)" and p.exits[-1]["ltp"] == 97.2, f"{p.stop_loss} {p.exit_reason} {p.exits}")
print("FAILURES:", fails)

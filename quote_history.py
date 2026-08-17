"""
STEP 9c — Point-in-time option-chain capture, so a later BACKTEST can
replay a date exactly as LIVE saw it.

THE PROBLEM THIS FIXES (found 09-Aug-26, comparing 07-Aug-26's actual LIVE
session against a BACKTEST of the same date run two days later)
--------------------------------------------------------------------------
Final Recomm signals reproduced exactly (199/199 symbols, byte-identical).
The trades taken did not: BAJAJ-AUTO and SHREECEM won live but failed the
BACKTEST's spread gate ("Spread too wide"), and MCX failed with "no CE
contract at ATM strike 2680" -- because option_chain.resolve_chain_window
and fetch_quotes always ask the CURRENT scrip master and CURRENT live
quotes, even inside a BACKTEST. Two days later, MCX's contract had rolled
out of the scrip master, and BAJAJ-AUTO/SHREECEM's spreads were simply
whatever they happened to be today, not on 07-Aug. One early wrong
rejection then cascades through MAX_CONCURRENT_POSITIONS and reshuffles
every later capacity-gated decision for the rest of the day.

The LTP side of this was already fixed (order_engine._historical_quote).
This module is the same fix for contract EXISTENCE and for spread/volume/OI.

HOW IT WORKS
------------
LIVE: every real resolve+quote gets captured to one CSV per trading day
(paths.QUOTE_HIST_DIR) -- which contracts existed, and their quotes at that
moment. Pure side effect, changes nothing about what LIVE actually does.

BACKTEST: resolve_and_quote()/resolve_only() try the capture first. If a
date was captured (i.e. it was run LIVE at some point), the backtest uses
ONLY that day's real data -- no scrip master call, no live quote call, and
therefore no drift from today's market. If nothing was captured for that
date (07-Aug's original run predates this feature, or the date was never
run live), it falls back to today's live scrip master/quotes exactly as
before, with a printed warning that the result may not match the real day.

CSV, not xlsx -- same reasoning as oi_log.py: many small appends through a
live day, from a process that's also writing the main workbook. A CSV
survives a torn read; an xlsx is a zip archive that doesn't.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

import config
import ist_clock
import option_chain
import paths
from option_chain import ChainWindow, OptionContract

COLUMNS = ["Date", "Time", "Symbol", "TradingSymbol", "Token", "Strike",
          "OptionType", "Expiry", "LotSize", "LTP", "Bid", "Ask",
          "SpreadPct", "Volume", "OI", "Delta", "Gamma", "Theta", "Vega", "IV"]

# One day's file, parsed once per process and reused -- a BACKTEST can call
# into this dozens of times per symbol across a session.
_day_cache: dict[date, pd.DataFrame | None] = {}


def day_file(trade_date: date) -> Path:
    return paths.QUOTE_HIST_DIR / f"{trade_date:%d-%b-%y}_Quotes.csv"


# --------------------------------------------------------------------------
# capture (LIVE)
# --------------------------------------------------------------------------
def record_chain(chain: ChainWindow, trade_date: date,
                 now: datetime | None = None) -> None:
    """
    Append one row per contract in the window. Safe to call every cycle --
    LIVE naturally re-captures the same contracts repeatedly through the
    day, which is the point: fill_historical_quotes later picks whichever
    captured row is nearest-before the moment a BACKTEST needs a price for.
    """
    contracts = chain.calls + chain.puts
    if not contracts:
        return
    now = now or ist_clock.now_ist()
    paths.QUOTE_HIST_DIR.mkdir(parents=True, exist_ok=True)
    path = day_file(trade_date)
    is_new = not path.exists()

    import csv
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(COLUMNS)
        for c in contracts:
            writer.writerow([
                trade_date.strftime("%d-%b-%y"), now.strftime("%H:%M:%S"),
                chain.symbol, c.trading_symbol, c.token, c.strike,
                c.option_type, c.expiry.strftime("%Y-%m-%d"), c.lot_size,
                c.ltp, c.bid, c.ask, c.spread_pct, c.volume, c.oi,
                c.delta, c.gamma, c.theta, c.vega, c.iv,
            ])

    # A fresh capture invalidates the in-memory cache for this date.
    _day_cache.pop(trade_date, None)


# --------------------------------------------------------------------------
# replay (BACKTEST)
# --------------------------------------------------------------------------
def _load_day(trade_date: date) -> pd.DataFrame | None:
    if trade_date in _day_cache:
        return _day_cache[trade_date]

    path = day_file(trade_date)
    if not path.exists() or path.stat().st_size == 0:
        _day_cache[trade_date] = None
        return None
    try:
        df = pd.read_csv(path, dtype={"Token": str})
        df["Timestamp"] = pd.to_datetime(
            df["Date"] + " " + df["Time"], format="%d-%b-%y %H:%M:%S")
        df["Timestamp"] = df["Timestamp"].dt.tz_localize(ist_clock.IST)
    except Exception as exc:
        print(f"[quote-history] {path.name}: unreadable ({exc}), ignoring capture")
        df = None
    _day_cache[trade_date] = df
    return df


def historical_chain_window(symbol: str, spot: float, strike_step: float,
                            trade_date: date,
                            window: int | None = None) -> ChainWindow | None:
    """
    Rebuild a ChainWindow purely from captured rows -- which contracts
    existed for this symbol on this date, per LIVE's own resolution that
    day. No scrip master involved, so a since-expired contract (the MCX
    case) still resolves correctly.
    """
    df = _load_day(trade_date)
    if df is None:
        return None
    sub = df[df["Symbol"] == symbol]
    if sub.empty:
        return None

    atm_strike = option_chain.round_to_atm(spot, strike_step)
    wanted = set(option_chain.strike_ladder(atm_strike, strike_step, window))
    expiry = pd.to_datetime(sub["Expiry"].iloc[-1]).date()

    chain = ChainWindow(symbol=symbol, spot=spot, atm_strike=atm_strike,
                        strike_step=strike_step, expiry=expiry)
    seen: set[tuple[float, str]] = set()
    for _, row in sub.iterrows():
        key = (float(row["Strike"]), str(row["OptionType"]))
        if key in seen or float(row["Strike"]) not in wanted:
            continue
        seen.add(key)
        bucket = chain.calls if key[1] == "CE" else chain.puts
        bucket.append(OptionContract(
            symbol=symbol, trading_symbol=str(row["TradingSymbol"]),
            token=str(row["Token"]), strike=key[0], option_type=key[1],
            expiry=expiry, lot_size=int(row["LotSize"]),
        ))

    if not chain.calls and not chain.puts:
        return None
    return chain


def fill_historical_quotes(chain: ChainWindow, trade_date: date,
                           at: datetime) -> bool:
    """
    Fill ltp/bid/ask/volume/oi (and Delta/Gamma/Theta/Vega/IV when the
    capture has them) on every contract from the capture row
    nearest-at-or-before `at`. Returns True only if EVERY contract in the
    window got a real quote -- a partially-filled window isn't trustworthy
    enough to skip the live fallback, since a missing contract would just
    silently size/audit against None.

    Greeks are filled opportunistically and do NOT count toward that
    all-filled requirement -- they're not gating anything (see
    config.GREEKS_FETCH_ENABLED's docstring), and a capture made before
    this field existed, or one where the optionGreek call simply failed
    that cycle, shouldn't block an otherwise-complete quote replay.
    """
    df = _load_day(trade_date)
    if df is None:
        return False

    all_filled = True
    for contract in chain.calls + chain.puts:
        rows = df[(df["TradingSymbol"] == contract.trading_symbol)
                  & (df["Timestamp"] <= at)]
        if rows.empty:
            all_filled = False
            continue
        row = rows.loc[rows["Timestamp"].idxmax()]
        contract.ltp = _f(row["LTP"])
        contract.bid = _f(row["Bid"])
        contract.ask = _f(row["Ask"])
        contract.volume = _f(row["Volume"])
        contract.oi = _f(row["OI"])
        contract.delta = _f(row.get("Delta"))
        contract.gamma = _f(row.get("Gamma"))
        contract.theta = _f(row.get("Theta"))
        contract.vega = _f(row.get("Vega"))
        contract.iv = _f(row.get("IV"))
    return all_filled


def _f(v) -> float | None:
    return None if pd.isna(v) else float(v)


# --------------------------------------------------------------------------
# the two entry points order_engine actually calls
# --------------------------------------------------------------------------
def resolve_only(symbol: str, spot: float, strike_step: float,
                 scrip: pd.DataFrame, trade_date: date, mode: str,
                 sheet_expiry=None, window: int | None = None) -> ChainWindow | None:
    """
    Contract EXISTENCE only, no quotes -- what _tradeable_precheck needs.
    BACKTEST tries the capture first (fixes the MCX case); LIVE always
    resolves live (nothing to gain from history there).
    """
    if mode != config.LIVE:
        hist = historical_chain_window(symbol, spot, strike_step, trade_date, window)
        if hist is not None:
            return hist
    return option_chain.resolve_chain_window(
        symbol, spot, strike_step, scrip, trade_date, sheet_expiry, window)


def _fetch_quotes(chain: ChainWindow, angel, kite, nfo_instruments,
                  angel_limiter, kite_limiter) -> None:
    """
    Kite-first, Angel-fallback (09-Aug-26) -- see
    option_chain.fetch_quotes_kite's docstring for why Kite is preferred
    (documented OI field vs Angel's unverified one). Falls back to
    Angel-only when no Kite session/instrument dump is available, so this
    still works exactly as before if `kite` is None.
    """
    if kite is not None and nfo_instruments is not None:
        option_chain.fetch_quotes_kite_first(
            chain, angel, kite, nfo_instruments,
            angel_limiter=angel_limiter, kite_limiter=kite_limiter)
    else:
        option_chain.fetch_quotes(chain, angel, limiter=angel_limiter)


def resolve_and_quote(symbol: str, spot: float, strike_step: float,
                      scrip: pd.DataFrame, trade_date: date, mode: str,
                      at: datetime, angel, angel_limiter,
                      sheet_expiry=None, window: int | None = None,
                      kite=None, nfo_instruments=None, kite_limiter=None
                      ) -> ChainWindow | None:
    """
    Contracts AND quotes -- what the real entry-construction path needs.

    LIVE: resolves and quotes live as always (Kite-first, Angel-fallback --
    see _fetch_quotes), then records the result for every future backtest
    of this date.

    BACKTEST: tries the capture (contracts + quotes as of `at`). Only uses
    it if EVERY contract in the window got a real historical quote --
    otherwise falls back to today's live scrip master/quotes, same as
    before this module existed, with a warning that this date's backtest
    may not match the actual day.
    """
    if mode == config.LIVE:
        chain = option_chain.resolve_chain_window(
            symbol, spot, strike_step, scrip, trade_date, sheet_expiry, window)
        if chain is not None:
            _fetch_quotes(chain, angel, kite, nfo_instruments,
                         angel_limiter, kite_limiter)
            # Only for a run that reached here -- already past 3-bar
            # confluence and every cheap pre-filter. Not for every
            # watchlist symbol every cycle (config.GREEKS_FETCH_ENABLED's
            # own docstring). Angel only -- no Kite equivalent exists.
            if config.GREEKS_FETCH_ENABLED:
                option_chain.fetch_greeks(chain, angel, limiter=angel_limiter)
            record_chain(chain, trade_date, at)
        return chain

    hist_chain = historical_chain_window(symbol, spot, strike_step, trade_date, window)
    if hist_chain is not None and fill_historical_quotes(hist_chain, trade_date, at):
        return hist_chain

    print(f"[quote-history] {symbol}: no full capture for {trade_date:%d-%b-%y} "
          f"at {at:%H:%M} -- falling back to today's live scrip "
          f"master/quotes for this backtest (may not match the actual day)")
    chain = option_chain.resolve_chain_window(
        symbol, spot, strike_step, scrip, trade_date, sheet_expiry, window)
    if chain is not None:
        _fetch_quotes(chain, angel, kite, nfo_instruments,
                     angel_limiter, kite_limiter)
    return chain


if __name__ == "__main__":
    import tempfile
    from datetime import timedelta

    orig_dir = paths.QUOTE_HIST_DIR
    try:
        paths.QUOTE_HIST_DIR = Path(tempfile.mkdtemp())
        _day_cache.clear()
        td = date(2026, 8, 7)
        t1 = ist_clock.combine_ist(td, ist_clock.MARKET_OPEN.replace(hour=9, minute=35))

        chain = ChainWindow(symbol="MCX", spot=2683.0, atm_strike=2680,
                            strike_step=20, expiry=date(2026, 8, 28))
        chain.calls.append(OptionContract(
            symbol="MCX", trading_symbol="MCX28AUG262680CE", token="99999",
            strike=2680, option_type="CE", expiry=date(2026, 8, 28),
            lot_size=100, ltp=76.9, bid=76.5, ask=77.3, volume=500,
            delta=0.52, gamma=0.003, theta=-4.1, vega=2.3, iv=16.33))
        record_chain(chain, td, t1)

        rebuilt = historical_chain_window("MCX", 2683.0, 20, td)
        assert rebuilt is not None and rebuilt.atm("CE") is not None
        print("contract resolution from history: OK "
              f"({rebuilt.atm('CE').trading_symbol})")

        ok = fill_historical_quotes(rebuilt, td, t1 + timedelta(minutes=5))
        assert ok and rebuilt.atm("CE").ltp == 76.9
        assert rebuilt.atm("CE").delta == 0.52 and rebuilt.atm("CE").iv == 16.33
        print(f"quote replay from history: OK (ltp={rebuilt.atm('CE').ltp}, "
              f"delta={rebuilt.atm('CE').delta}, iv={rebuilt.atm('CE').iv})")

        print("\nquote_history self-check passed")
    finally:
        paths.QUOTE_HIST_DIR = orig_dir
        _day_cache.clear()

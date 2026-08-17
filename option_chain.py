"""
STEP 9 — Find the right option contract, and fetch only the data needed.

ATM SELECTION
-------------
Round the underlying LTP to the nearest multiple of that symbol's strike step
-- the "Option Price Difference" column in the Reference sheet. HDFCLIFE has
step 5, so spot 545.75 rounds to strike 545. MARUTI has step 100, so 14207
rounds to 14200. Verified against the sample workbook: every ATM Strike in
the Orders sheet reproduces from this rule.

WHY ONLY +/-2 STRIKES
---------------------
A full chain is 40+ strikes per expiry per symbol. Across 50 symbols that is
thousands of quote calls against a 3/sec rate limit -- minutes of wall clock
per cycle, in a loop that has 5 minutes total. Five strikes per side covers
every contract this strategy can actually trade, and is what makes a 5-minute
cadence feasible at all.

SYMBOL FORMAT
-------------
Angel One NFO trading symbols look like HDFCLIFE26AUG545PE:
    <NAME><YY><MMM><STRIKE><CE|PE>
Confirmed against Option Symbol values in the sample workbook.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

import config


@dataclass
class OptionContract:
    symbol: str                 # underlying, e.g. HDFCLIFE
    trading_symbol: str         # HDFCLIFE26AUG545PE
    token: str                  # Angel One symboltoken
    strike: float
    option_type: str            # "CE" | "PE"
    expiry: date
    lot_size: int
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None
    oi: float | None = None     # open interest -- live snapshot only, see
                                 # option_audit.oi_confirms for why there is
                                 # no historical equivalent for BACKTEST

    # Greeks + IV (09-Aug-26) -- from Angel's separate optionGreek endpoint,
    # not part of getMarketData. Not used by any gate yet; captured for
    # future strategy work, same LIVE-only reasoning as OI. See
    # fetch_greeks() below and quote_history.py for the capture/replay.
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    iv: float | None = None

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float | None:
        s = self.spread
        if s is None or not self.ltp:
            return None
        return s / self.ltp

    @property
    def notional(self) -> float | None:
        if self.ltp is None:
            return None
        return self.ltp * self.lot_size


@dataclass
class ChainWindow:
    """The +/-N strikes around ATM, both CE and PE."""
    symbol: str
    spot: float
    atm_strike: float
    strike_step: float
    expiry: date
    calls: list[OptionContract] = field(default_factory=list)
    puts: list[OptionContract] = field(default_factory=list)

    def atm(self, option_type: str) -> OptionContract | None:
        """
        The contract at self.atm_strike -- or, if that exact strike isn't
        in the resolved pool (16-Aug-26: some strikes the ATM rounding
        lands on simply aren't listed in the scrip master/historical
        capture, e.g. a thinly-traded strike -- this was rejecting the
        whole trade as "no CE contract at ATM strike X" over a rounding
        miss rather than a real absence of tradeable strikes), the
        NEAREST available strike in the same pool instead. None only when
        the pool itself is empty.
        """
        pool = self.calls if option_type == "CE" else self.puts
        if not pool:
            return None
        for c in pool:
            if abs(c.strike - self.atm_strike) < 1e-6:
                return c
        return min(pool, key=lambda c: abs(c.strike - self.atm_strike))


# --------------------------------------------------------------------------
# strike maths
# --------------------------------------------------------------------------
def round_to_atm(ltp: float, strike_step: float) -> float:
    """
    Nearest strike to the spot price.

    Ties round up, matching NSE convention. Verified against the sample
    workbook: 545.75/step 5 -> 545, 14207/step 100 -> 14200,
    2013.9/step 100 -> 2000, 387.75/step 10 -> 390.
    """
    if strike_step <= 0:
        raise ValueError(f"strike step must be positive, got {strike_step}")
    return float(math.floor(ltp / strike_step + 0.5) * strike_step)


def strike_ladder(atm: float, step: float, window: int | None = None) -> list[float]:
    """ATM plus `window` strikes either side, ascending."""
    window = config.OPTION_STRIKE_WINDOW if window is None else window
    return [atm + i * step for i in range(-window, window + 1)]


def build_trading_symbol(symbol: str, expiry: date, strike: float,
                         option_type: str) -> str:
    """
    Angel One NFO format: HDFCLIFE26AUG545PE

    Strike is rendered without a decimal point when it is a whole number,
    which is how it appears in the scrip master for every equity option.
    """
    strike_txt = str(int(strike)) if float(strike).is_integer() else str(strike)
    return f"{symbol.upper()}{expiry:%y%b}".upper() + strike_txt + option_type.upper()


# --------------------------------------------------------------------------
# resolving contracts against the Angel scrip master
# --------------------------------------------------------------------------
def _option_universe(scrip: pd.DataFrame, symbol: str) -> pd.DataFrame:
    df = scrip[
        (scrip["name"].astype(str).str.upper() == symbol.upper())
        & (scrip["exch_seg"].astype(str).str.upper() == "NFO")
        & (scrip["instrumenttype"].astype(str).str.upper().isin(["OPTSTK", "OPTIDX"]))
    ].copy()
    if df.empty:
        return df
    # Angel stores strike in paise -- 54500 means 545.00
    df["strike_rs"] = pd.to_numeric(df["strike"], errors="coerce") / 100.0
    if "expiry_dt" not in df.columns:
        df["expiry_dt"] = pd.to_datetime(df["expiry"], format="%d%b%Y", errors="coerce")
    return df


def pick_expiry(universe: pd.DataFrame, trade_date: date,
                sheet_expiry=None) -> date | None:
    """
    Choose the expiry to trade.

    "sheet" honours the Reference sheet's Expiry Date. If that contract does
    not exist, falls back to the nearest expiry on or after it -- and the
    caller reports the substitution rather than trading a different month
    silently.
    """
    if universe.empty:
        return None
    expiries = sorted({d.date() for d in universe["expiry_dt"].dropna()})
    if not expiries:
        return None

    if config.OPTION_EXPIRY_MODE == "sheet" and sheet_expiry is not None and pd.notna(sheet_expiry):
        want = pd.Timestamp(sheet_expiry).date()
        if want in expiries:
            return want
        later = [e for e in expiries if e >= want]
        if later:
            return later[0]

    later = [e for e in expiries if e >= trade_date]
    return later[0] if later else None


def days_to_expiry(expiry: date, trade_date: date) -> int:
    return (expiry - trade_date).days


# --------------------------------------------------------------------------
# resolving contracts against Zerodha's own NFO instrument dump
# --------------------------------------------------------------------------
def resolve_kite_option_token(instruments: pd.DataFrame, symbol: str,
                              strike: float, expiry: date,
                              option_type: str) -> tuple[int | None, str | None]:
    """
    Match one option contract in Kite's NFO instrument dump (kite.instruments
    or token_mgmt's cached fetch of it).

    Zerodha stores strike in RUPEES (555.0 means Rs 555). Angel's scrip
    master stores it in PAISE (54500 means Rs 545) -- do not reuse that
    convention here, it is the single easiest way to silently match the
    wrong strike.

    Returns (instrument_token, tradingsymbol) or (None, None) if no contract
    matches -- never a guess at the nearest strike, since a wrong contract
    would feed the wrong premium into the exit ladder.
    """
    if instruments is None or instruments.empty:
        return None, None
    want_expiry = pd.Timestamp(expiry).normalize()
    seg = instruments[
        (instruments["name"].astype(str).str.upper() == symbol.upper())
        & (instruments["instrument_type"].astype(str).str.upper() == option_type.upper())
        & (instruments["expiry"].dt.normalize() == want_expiry)
        & (instruments["strike"].sub(strike).abs() < 0.5)
    ]
    if seg.empty:
        return None, None
    row = seg.iloc[0]
    return int(row["instrument_token"]), str(row["tradingsymbol"])


def resolve_chain_window(symbol: str, spot_ltp: float, strike_step: float,
                         scrip: pd.DataFrame, trade_date: date,
                         sheet_expiry=None,
                         window: int | None = None) -> ChainWindow | None:
    """
    Build the +/-N strike window for one underlying. No quotes fetched yet --
    this only resolves which contracts exist and what their tokens are.
    """
    universe = _option_universe(scrip, symbol)
    if universe.empty:
        return None

    expiry = pick_expiry(universe, trade_date, sheet_expiry)
    if expiry is None:
        return None

    atm = round_to_atm(spot_ltp, strike_step)
    wanted = strike_ladder(atm, strike_step, window)
    at_expiry = universe[universe["expiry_dt"].dt.date == expiry]

    chain = ChainWindow(symbol=symbol, spot=spot_ltp, atm_strike=atm,
                        strike_step=strike_step, expiry=expiry)

    for strike in wanted:
        for opt_type, bucket in (("CE", chain.calls), ("PE", chain.puts)):
            match = at_expiry[
                (at_expiry["strike_rs"].sub(strike).abs() < 1e-6)
                & (at_expiry["symbol"].astype(str).str.upper().str.endswith(opt_type))
            ]
            if match.empty:
                continue
            row = match.iloc[0]
            bucket.append(OptionContract(
                symbol=symbol,
                trading_symbol=str(row["symbol"]),
                token=str(row["token"]),
                strike=strike,
                option_type=opt_type,
                expiry=expiry,
                lot_size=int(float(row.get("lotsize", 0) or 0)),
            ))

    if not chain.calls and not chain.puts:
        return None
    return chain


# --------------------------------------------------------------------------
# quotes
# --------------------------------------------------------------------------
def fetch_quotes(chain: ChainWindow, angel, limiter=None) -> ChainWindow:
    """
    Populate ltp / bid / ask / volume for every contract in the window.

    Angel's getMarketData("FULL", ...) accepts a batch of tokens, so the whole
    window is one call rather than 2*(2N+1) calls. That is the difference
    between fitting inside a 5-minute cadence and not.

    Contracts that come back without a quote keep ltp=None and are rejected
    downstream by the audit -- never silently defaulted to zero.
    """
    contracts = chain.calls + chain.puts
    if not contracts:
        return chain

    tokens = [c.token for c in contracts]
    by_token = {c.token: c for c in contracts}

    try:
        if limiter is not None:
            limiter.acquire()
        resp = angel.getMarketData("FULL", {"NFO": tokens})
    except Exception as exc:
        print(f"[chain] {chain.symbol}: quote fetch failed ({exc})")
        return chain

    if not resp or not resp.get("status"):
        print(f"[chain] {chain.symbol}: quote response not OK")
        return chain

    for item in (resp.get("data", {}) or {}).get("fetched", []) or []:
        contract = by_token.get(str(item.get("symbolToken")))
        if contract is None:
            continue
        contract.ltp = _num(item.get("ltp"))
        contract.volume = _num(item.get("tradeVolume"))
        # Angel SmartAPI's FULL market-data field for open interest. Not
        # verified against a live response yet -- if oi_confirms fails
        # open on every single run ("no OI data"), check this field name
        # first against what getMarketData actually returns.
        contract.oi = _num(item.get("opnInterest"))
        depth = item.get("depth") or {}
        buys, sells = depth.get("buy") or [], depth.get("sell") or []
        if buys:
            contract.bid = _num(buys[0].get("price"))
        if sells:
            contract.ask = _num(sells[0].get("price"))

    priced = sum(1 for c in contracts if c.ltp is not None)
    print(f"[chain] {chain.symbol} {chain.expiry:%d-%b-%y}: "
          f"ATM {chain.atm_strike:g}, {priced}/{len(contracts)} contracts priced")
    return chain


def fetch_quotes_kite(chain: ChainWindow, kite, nfo_instruments,
                      limiter=None) -> set[str]:
    """
    LTP/bid/ask/volume/OI from Zerodha Kite's quote() (09-Aug-26) --
    preferred over Angel for these fields: Kite's `oi` field is officially
    documented (kite.trade/docs/connect/v3/market-quotes); Angel's
    `opnInterest` (see fetch_quotes above) was always a best guess, never
    verified against a live response. Mirrors the Kite-first/Angel-fallback
    pattern option_data.py already uses for option candles.

    Returns the set of trading_symbols that got a real Kite quote --
    fetch_quotes_kite_first() below only falls back to Angel for whatever
    this didn't cover (e.g. a contract Kite's NFO dump doesn't list).
    """
    contracts = chain.calls + chain.puts
    if not contracts or nfo_instruments is None or kite is None:
        return set()

    by_token: dict[int, OptionContract] = {}
    for c in contracts:
        ktoken, _ksym = resolve_kite_option_token(
            nfo_instruments, chain.symbol, c.strike, c.expiry, c.option_type)
        if ktoken is not None:
            by_token[ktoken] = c

    if not by_token:
        return set()

    try:
        if limiter is not None:
            limiter.acquire()
        resp = kite.quote(list(by_token.keys()))
    except Exception as exc:
        print(f"[chain] {chain.symbol}: Kite quote fetch failed ({exc})")
        return set()

    covered: set[str] = set()
    for token, contract in by_token.items():
        item = (resp or {}).get(str(token))
        if not item:
            continue
        contract.ltp = _num(item.get("last_price"))
        contract.volume = _num(item.get("volume"))
        contract.oi = _num(item.get("oi"))
        depth = item.get("depth") or {}
        buys, sells = depth.get("buy") or [], depth.get("sell") or []
        if buys:
            contract.bid = _num(buys[0].get("price"))
        if sells:
            contract.ask = _num(sells[0].get("price"))
        covered.add(contract.trading_symbol)

    print(f"[chain] {chain.symbol}: Kite quotes matched {len(covered)}/{len(contracts)} contract(s)")
    return covered


def fetch_quotes_kite_first(chain: ChainWindow, angel, kite, nfo_instruments,
                            angel_limiter=None, kite_limiter=None) -> ChainWindow:
    """
    Kite first, Angel only for whatever Kite didn't cover -- never both for
    the same contract (Angel would silently overwrite Kite's values with
    its own, defeating the whole point of preferring Kite).

    A missing contract gets its own throwaway ChainWindow for the Angel
    call rather than passing the full window -- the OptionContract objects
    are shared by reference either way, so fetch_quotes() still mutates the
    real contracts in `chain`; this just stops it from touching the ones
    Kite already answered.
    """
    covered = fetch_quotes_kite(chain, kite, nfo_instruments, limiter=kite_limiter)
    contracts = chain.calls + chain.puts
    missing = [c for c in contracts if c.trading_symbol not in covered]

    if missing:
        if covered:
            print(f"[chain] {chain.symbol}: {len(missing)} contract(s) not on "
                  f"Kite, falling back to Angel for those only")
        partial = ChainWindow(symbol=chain.symbol, spot=chain.spot,
                              atm_strike=chain.atm_strike,
                              strike_step=chain.strike_step, expiry=chain.expiry)
        for c in missing:
            (partial.calls if c.option_type == "CE" else partial.puts).append(c)
        fetch_quotes(partial, angel, limiter=angel_limiter)

    return chain


def fetch_greeks(chain: ChainWindow, angel, limiter=None) -> ChainWindow:
    """
    Delta/Gamma/Theta/Vega/IV for every contract in the window, from
    Angel's dedicated optionGreek endpoint (09-Aug-26) -- a SEPARATE call
    from fetch_quotes/getMarketData, confirmed against Angel's SmartAPI
    Python SDK: angel.optionGreek({"name": ..., "expirydate": "29FEB2024"}).

    The endpoint returns Greeks for the WHOLE expiry's chain, not just this
    window, so most returned rows are discarded -- matched back to this
    window's contracts by (strike, option_type).

    Deliberately called only for a run that already qualified (3-bar
    confluence, cleared the cheap pre-filters, has a resolved chain) --
    not for every watchlist symbol every cycle. One extra API call per
    real candidate is a reasonable cost; one per symbol per cycle across
    ~200 watchlist symbols is not, inside a 3-call/sec, 5-minute budget.
    """
    contracts = chain.calls + chain.puts
    if not contracts:
        return chain

    try:
        if limiter is not None:
            limiter.acquire()
        resp = angel.optionGreek({
            "name": chain.symbol,
            "expirydate": chain.expiry.strftime("%d%b%Y").upper(),
        })
    except Exception as exc:
        print(f"[chain] {chain.symbol}: option Greeks fetch failed ({exc})")
        return chain

    if not resp or not resp.get("status"):
        print(f"[chain] {chain.symbol}: option Greeks response not OK")
        return chain

    matched = 0
    for item in resp.get("data") or []:
        strike = _num(item.get("strikePrice"))
        opt_type = str(item.get("optionType", "")).upper()
        if strike is None:
            continue
        # Tolerance match, not exact-equality dict lookup -- two
        # independently-parsed float strings for the "same" strike aren't
        # guaranteed bit-identical. Same defensive pattern as
        # ChainWindow.atm()'s own strike comparison.
        contract = next((c for c in contracts if c.option_type == opt_type
                         and abs(c.strike - strike) < 1e-6), None)
        if contract is None:
            continue
        contract.delta = _num(item.get("delta"))
        contract.gamma = _num(item.get("gamma"))
        contract.theta = _num(item.get("theta"))
        contract.vega = _num(item.get("vega"))
        contract.iv = _num(item.get("impliedVolatility"))
        matched += 1

    print(f"[chain] {chain.symbol}: Greeks matched for {matched}/{len(contracts)} contract(s)")
    return chain


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    print("ATM rounding, checked against the sample workbook's Orders sheet:")
    cases = [
        ("HDFCLIFE", 545.75, 5, 545),
        ("BEL", 387.75, 10, 390),
        ("MARUTI", 14207, 100, 14200),
        ("SUNPHARMA", 2013.9, 100, 2000),
    ]
    for name, spot, step, expected in cases:
        got = round_to_atm(spot, step)
        flag = "OK " if abs(got - expected) < 1e-6 else "FAIL"
        print(f"  {flag} {name:<10} spot {spot:>9} step {step:>4} -> {got:g} "
              f"(workbook: {expected})")
        assert abs(got - expected) < 1e-6

    print("\nstrike ladder for MARUTI (+/-2):")
    print("  ", strike_ladder(14200, 100))
    print("\ntrading symbol:",
          build_trading_symbol("HDFCLIFE", date(2026, 8, 25), 545, "PE"),
          "(workbook: HDFCLIFE26AUG545PE)")
    assert build_trading_symbol("HDFCLIFE", date(2026, 8, 25), 545, "PE") == "HDFCLIFE26AUG545PE"
    print("\noption_chain self-check passed")

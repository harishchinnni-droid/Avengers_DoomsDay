"""
STEP 9b — Option candles from Angel One, cached to 04_Option_Historical_Data.

A BACKTEST cannot simulate the exit ladder from the underlying's candles --
it needs the OPTION's own price path. A 1% move in the underlying is not a
1% move in the premium, and the stop is set on the premium.

Cached per (token, date) so a re-run costs nothing. Same discipline as the
underlying cache in data_ingestion: atomic writes, no unclosed candles, and
a cache holding candles past the cutoff is purged rather than trusted.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import config
import ist_clock
import paths
from data_ingestion import RateLimiter, _drop_unclosed_candles

OPT_DIR = paths.OPTION_HIST_DIR

# Angel One's own limit is lower than Kite's; stay well under it.
_angel_limiter = RateLimiter(config.ANGEL_MAX_PER_SECOND)

_INTERVAL_MAP = {
    "1minute": "ONE_MINUTE",
    "3minute": "THREE_MINUTE",
    "5minute": "FIVE_MINUTE",
    "15minute": "FIFTEEN_MINUTE",
    "30minute": "THIRTY_MINUTE",
    "60minute": "ONE_HOUR",
}


def _effective_cutoff(target_date: date,
                      cutoff: datetime | None) -> datetime | None:
    """
    The cutoff a fetch should ACTUALLY honour, which is not always the one
    the caller passed.

    BUG FOUND 15-Sep-26 (BAJAJFINSV/AUBANK/BDL, Harish: "did not exit at
    11:00 when there was a dot, instead it continued till EOD"). BACKTEST
    passes cutoff=None, and both fetchers below took that to mean "any
    cached file for this date is a complete day, return it as-is". That
    assumption only holds for a PAST date. A backtest date can be TODAY --
    a very normal thing to do -- and that day's cache may have been written
    part-way through the session by the morning LIVE run. On 15-Sep-26 the
    LIVE run cached 3-5 bars (09:15-09:35) for exactly the contracts it was
    tracking; the evening BACKTEST then reused those stubs verbatim and
    never fetched the remaining ~70 bars. The exit-ladder walk in
    order_engine ran out of option candles at 09:35, so no Dot/Triangle
    reversal after that could ever be reached, every position fell through
    to EOD Square-off, and the P/L was marked at a stale 09:35 close.
    Contracts the morning run had NOT touched got a clean full-day fetch --
    which is why some of that date's cache files are ~4.9 KB (75 bars) and
    the three traded ones were 232-361 bytes.

    This is the same bug data_ingestion.download_historical_data fixed for
    UNDERLYING candles on 14-Sep-26 (keying the cutoff off whether
    target_date IS today, not off `mode`); it was never mirrored here,
    which is why the Final sheet had all 75 underlying slots while the
    option series stopped at 09:35.

    A genuinely past date still returns None (no cutoff, unchanged
    behaviour); LIVE's explicit cutoff is always returned untouched.
    """
    if cutoff is not None:
        return cutoff
    if target_date == ist_clock.today_ist():
        return ist_clock.now_ist()
    return None


def _cache_is_complete(cached: pd.DataFrame, target_date: date,
                       eff_cutoff: datetime | None,
                       interval_td: timedelta) -> bool:
    """
    True when the cache already covers everything that could have closed.

    With an effective cutoff (LIVE, or a same-day BACKTEST) the test is the
    original one -- the last cached bar must close after the cutoff. For a
    past date there is no cutoff, so the bar must reach the session close;
    a cache stopping short of it is a stub to top up, not a full day. The
    old code had no such test at all and returned any cache unconditionally.
    """
    last_close = cached.index.max() + interval_td
    if eff_cutoff is not None:
        return bool(last_close > eff_cutoff)
    return bool(last_close >= ist_clock.combine_ist(target_date,
                                                    ist_clock.MARKET_CLOSE))


def _cache_path(token: str, interval: str, target_date: date) -> Path:
    OPT_DIR.mkdir(parents=True, exist_ok=True)
    return OPT_DIR / f"{token}_{interval}_{target_date:%Y-%m-%d}.csv"


def load_cached(token: str, interval: str, target_date: date) -> pd.DataFrame | None:
    p = _cache_path(token, interval, target_date)
    if not p.exists() or p.stat().st_size == 0:
        return None
    try:
        df = pd.read_csv(p, parse_dates=["date"], index_col="date")
    except Exception:
        return None
    if df.empty:
        return None
    df.index = (df.index.tz_localize(ist_clock.IST) if df.index.tz is None
                else df.index.tz_convert(ist_clock.IST))
    return df


def _save(token: str, interval: str, target_date: date, df: pd.DataFrame) -> None:
    p = _cache_path(token, interval, target_date)
    tmp = p.with_suffix(".tmp")
    df.to_csv(tmp, index_label="date")
    tmp.replace(p)  # atomic -- never leaves a truncated cache


def fetch_option_candles(token: str, target_date: date, angel,
                         interval: str | None = None,
                         cutoff: datetime | None = None,
                         trading_symbol: str = "") -> pd.DataFrame | None:
    """
    5-minute OHLCV for one option contract on one date.

    Returns None when Angel has no data for the contract -- which is normal
    for a strike that never traded. None is a real answer here and must not
    be turned into an empty frame that later reads as "the price never moved".

    TOP-UP, not just reuse-or-refetch (10-Aug-26 fix): the old check only
    asked "does the cache hold candles PAST cutoff" (too much) and returned
    the cache unconditionally otherwise -- it never asked "is the cache
    missing candles UP TO cutoff" (too little). In LIVE, a contract's cache
    written once near entry then satisfied that check on every later cycle
    forever (a stale cache's max timestamp is never past a cutoff that keeps
    moving forward), so the exit-ladder walk kept replaying the same first
    handful of bars all day -- SL/Target/TSL could never fire because they
    never saw a new candle. Same top-up shape as
    data_ingestion.update_incremental_data already uses for underlying
    candles: if the cache doesn't reach cutoff, fetch only what's missing
    and merge, exactly like a real incremental poll should.
    """
    interval = interval or config.INTERVAL
    label = trading_symbol or token
    interval_td = timedelta(minutes=config.INTERVAL_MINUTES)

    # 15-Sep-26: a BACKTEST date can be TODAY, whose cache may be a stub the
    # morning LIVE run wrote. See _effective_cutoff's docstring.
    cutoff = _effective_cutoff(target_date, cutoff)

    cached = None
    if config.REUSE_CACHE and not config.FORCE_REFRESH:
        cached = load_cached(token, interval, target_date)
        if cached is not None:
            if cutoff is not None and (cached.index > cutoff).any():
                print(f"[optdata] {label}: cache holds candles past cutoff, refetching")
                cached = None
            elif _cache_is_complete(cached, target_date, cutoff, interval_td):
                # Nothing further could have closed -- the cache is current.
                return cached
            else:
                print(f"[optdata] {label}: cached bars end at "
                      f"{cached.index.max():%H:%M}, short of the session -- "
                      f"topping up")
            # else: cache exists but is STALE relative to cutoff -- top it up.

    angel_interval = _INTERVAL_MAP.get(interval)
    if angel_interval is None:
        raise ValueError(f"unsupported interval {interval!r}")

    start = (cached.index.max() + interval_td if cached is not None and not cached.empty
             else ist_clock.combine_ist(target_date, ist_clock.MARKET_OPEN))
    end = ist_clock.combine_ist(target_date, ist_clock.MARKET_CLOSE)
    if cutoff is not None:
        end = min(end, cutoff)

    params = {
        "exchange": "NFO",
        "symboltoken": str(token),
        "interval": angel_interval,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
    }

    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        _angel_limiter.acquire()
        try:
            resp = angel.getCandleData(params)
            if not resp or not resp.get("status"):
                return cached
            rows = resp.get("data") or []
            if not rows:
                return cached

            df = pd.DataFrame(rows, columns=["date", "open", "high", "low",
                                             "close", "volume"])
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index().astype(float)
            df.index = (df.index.tz_localize(ist_clock.IST) if df.index.tz is None
                        else df.index.tz_convert(ist_clock.IST))

            df, _ = _drop_unclosed_candles(df, config.INTERVAL_MINUTES,
                                           cutoff, label)
            if df.empty:
                return cached
            if cached is not None and not cached.empty:
                df = pd.concat([cached, df])
                df = df[~df.index.duplicated(keep="last")].sort_index()
            _save(str(token), interval, target_date, df)
            return df

        except Exception as exc:
            last_exc = exc
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF_SECS * attempt)

    suffix = " -- using last-known cache" if cached is not None else ""
    print(f"[optdata] {label}: failed after {config.MAX_RETRIES} attempts: "
          f"{last_exc}{suffix}")
    return cached


def latest_quote_from_candles(df: pd.DataFrame | None,
                              at: datetime) -> float | None:
    """
    Close of the most recent CLOSED candle at or before `at`.

    Used by the backtest in place of a live LTP. Deliberately the close of a
    finished candle -- never a forming one.
    """
    if df is None or df.empty:
        return None
    usable = df[df.index + timedelta(minutes=config.INTERVAL_MINUTES) <= at]
    if usable.empty:
        return None
    return float(usable["close"].iloc[-1])


def purge(token: str, interval: str, target_date: date) -> None:
    p = _cache_path(token, interval, target_date)
    if p.exists():
        p.unlink()


# --------------------------------------------------------------------------
# Zerodha Kite Connect -- premium historical data
# --------------------------------------------------------------------------
_kite_limiter = RateLimiter(config.KITE_MAX_PER_SECOND)


def _kite_cache_path(token: int, interval: str, target_date: date) -> Path:
    OPT_DIR.mkdir(parents=True, exist_ok=True)
    return OPT_DIR / f"{token}_{interval}_{target_date:%Y-%m-%d}_kite.csv"


def load_cached_kite(token: int, interval: str, target_date: date) -> pd.DataFrame | None:
    p = _kite_cache_path(token, interval, target_date)
    if not p.exists() or p.stat().st_size == 0:
        return None
    try:
        df = pd.read_csv(p, parse_dates=["date"], index_col="date")
    except Exception:
        return None
    if df.empty:
        return None
    df.index = (df.index.tz_localize(ist_clock.IST) if df.index.tz is None
                else df.index.tz_convert(ist_clock.IST))
    return df


def _save_kite(token: int, interval: str, target_date: date, df: pd.DataFrame) -> None:
    p = _kite_cache_path(token, interval, target_date)
    tmp = p.with_suffix(".tmp")
    df.to_csv(tmp, index_label="date")
    tmp.replace(p)


def fetch_option_history_kite(kite_api, token: int, target_date: date,
                              interval: str | None = None,
                              cutoff: datetime | None = None,
                              trading_symbol: str = "") -> pd.DataFrame | None:
    """
    OHLCV for one option contract from Zerodha Kite Connect.

    Returns None if Kite has no data for the contract AND nothing was
    already cached -- the caller falls back to the Angel path. Same TOP-UP
    fix as fetch_option_candles above (10-Aug-26) -- see its docstring for
    the bug this closes (a stale cache silently never refreshing in LIVE,
    so SL/Target/TSL never fired because the exit-ladder walk kept seeing
    the same first handful of bars all day).
    """
    interval = interval or config.INTERVAL
    label = trading_symbol or str(token)
    interval_td = timedelta(minutes=config.INTERVAL_MINUTES)

    # 15-Sep-26: a BACKTEST date can be TODAY, whose cache may be a stub the
    # morning LIVE run wrote. See _effective_cutoff's docstring.
    cutoff = _effective_cutoff(target_date, cutoff)

    cached = None
    if config.REUSE_CACHE and not config.FORCE_REFRESH:
        cached = load_cached_kite(token, interval, target_date)
        if cached is not None:
            if cutoff is not None and (cached.index > cutoff).any():
                print(f"[optdata] {label}: Kite cache holds bars past cutoff, refetching")
                cached = None
            elif "oi" not in cached.columns:
                # Cached before the oi=True fetch param existed (16-Aug-26) --
                # found 16-Aug-26 via the Orders sheet's new 'OI Check' column
                # reading blank on every row of a BACKTEST that should have
                # had OI data. A cache written before that date has no 'oi'
                # column, and without this check the BACKTEST branch below
                # (cutoff is None) returns it as-is forever, silently
                # starving oi_buildup_confirms/classify_oi_buildup of data
                # for that contract's cached date every single run.
                print(f"[optdata] {label}: cached bars predate OI capture -- "
                      f"refetching to backfill OI")
                cached = None
            elif _cache_is_complete(cached, target_date, cutoff, interval_td):
                # Nothing further could have closed -- the cache is current.
                return cached
            else:
                print(f"[optdata] {label}: Kite cached bars end at "
                      f"{cached.index.max():%H:%M}, short of the session -- "
                      f"topping up")
            # else: cache exists but is STALE relative to cutoff -- top it up.

    start = (cached.index.max() + interval_td if cached is not None and not cached.empty
             else ist_clock.combine_ist(target_date, ist_clock.MARKET_OPEN))
    end = ist_clock.combine_ist(target_date, ist_clock.MARKET_CLOSE)
    if cutoff is not None:
        end = min(end, cutoff)

    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        _kite_limiter.acquire()
        try:
            raw = kite_api.historical_data(
                instrument_token=token,
                from_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=end.strftime("%Y-%m-%d %H:%M:%S"),
                interval=interval,
                oi=True,   # 16-Aug-26: needed for option_audit.oi_buildup_confirms
                          # (price+OI change over time, not just a live snapshot).
                          # F&O-only field; harmless no-op for instruments without it.
            )
            if not raw:
                return cached
            df = pd.DataFrame(raw)
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            df.index = (df.index.tz_localize(ist_clock.IST) if df.index.tz is None
                        else df.index.tz_convert(ist_clock.IST))

            keep = [c for c in ("open", "high", "low", "close", "volume", "oi")
                   if c in df.columns]
            df = df[keep].astype(float)

            df, _ = _drop_unclosed_candles(df, config.INTERVAL_MINUTES,
                                           cutoff, label, "(Kite)")
            if df.empty:
                return cached
            if cached is not None and not cached.empty:
                df = pd.concat([cached, df])
                df = df[~df.index.duplicated(keep="last")].sort_index()
            _save_kite(token, interval, target_date, df)
            return df

        except Exception as exc:
            last_exc = exc
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF_SECS * attempt)

    suffix = " -- using last-known cache" if cached is not None else ""
    print(f"[optdata] {label}: Kite fetch failed after {config.MAX_RETRIES} "
          f"attempts: {last_exc}{suffix}")
    return cached


if __name__ == "__main__":
    paths.ensure_dirs()
    print(f"option cache dir: {OPT_DIR}")
    print(f"interval map: {_INTERVAL_MAP[config.INTERVAL]}")
    existing = list(OPT_DIR.glob("*.csv"))
    print(f"cached option files: {len(existing)}")
    for p in existing[:5]:
        print("  ", p.name)

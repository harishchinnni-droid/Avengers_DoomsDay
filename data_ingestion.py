"""
STEP 7 — Download 5-minute candles. Once. Then reuse them all day.

THE TWO THINGS THIS MODULE EXISTS TO GET RIGHT
----------------------------------------------
1. NO DUPLICATE DOWNLOADS. Candles are cached to CSV per (symbol, interval,
   date). A re-run mid-session fetches only the bars that appeared since the
   last fetch and merges them. Re-downloading 161 symbols every five minutes
   burns your rate limit and adds nothing.

2. NO UNCLOSED CANDLES. A 09:15 five-minute candle fetched at 09:17 is
   incomplete. Its close will change. An RSI computed from it will change
   too -- that is repainting, and it is the reason a backtest can look
   excellent while the live account does the opposite. _drop_unclosed_candles
   keeps a candle only if candle_open + interval <= cutoff.

In BACKTEST mode cutoff is None and the guard is a no-op, because every
candle in a past session is already closed.
"""

from __future__ import annotations

import threading
import time as _time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import config
import ist_clock
import paths
from config import BACKTEST, LIVE

HIST_DIR = paths.HIST_DIR


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------
class RateLimiter:
    """
    Thread-safe sliding-window limiter. Holds even when called from a pool.

    A per-thread limiter would let N threads each do 3 req/s and get the
    whole app throttled. The lock is the point.
    """

    def __init__(self, max_per_second: int = 3):
        self.max_per_second = max_per_second
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = _time.monotonic()
                while self._calls and now - self._calls[0] >= 1.0:
                    self._calls.popleft()
                if len(self._calls) < self.max_per_second:
                    self._calls.append(now)
                    return
                sleep_for = 1.0 - (now - self._calls[0])
            _time.sleep(max(sleep_for, 0.01))


_kite_limiter = RateLimiter(config.KITE_MAX_PER_SECOND)


# --------------------------------------------------------------------------
# the repaint guard
# --------------------------------------------------------------------------
def _drop_unclosed_candles(df: pd.DataFrame, interval_minutes: int,
                           cutoff: datetime | None, label: str,
                           context: str = "") -> tuple[pd.DataFrame, int]:
    """
    Keep a candle ONLY IF candle_open + interval <= cutoff.

    cutoff=None (BACKTEST) makes this a no-op.
    Returns (df, dropped_n).

    Logging policy: a 1-candle trim is the routine tail and is not logged.
    Dropping more than one means a real gap between the data and the clock,
    and that IS logged, because it usually means the feed is behind.
    """
    if df.empty or cutoff is None:
        return df, 0

    delta = timedelta(minutes=interval_minutes)
    keep = df.index + delta <= cutoff
    dropped = int((~keep).sum())
    out = df[keep]

    if dropped > 1:
        print(f"[data] {label}: dropped {dropped} unclosed/future candle(s) "
              f"at cutoff {cutoff:%H:%M}{' ' + context if context else ''}")
    return out, dropped


# --------------------------------------------------------------------------
# cache paths
# --------------------------------------------------------------------------
def _cache_path(symbol: str, interval: str, target_date: date) -> Path:
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    return HIST_DIR / f"{symbol}_{interval}_{target_date:%Y-%m-%d}.csv"


def historical_data_exists(symbol: str, interval: str, target_date: date) -> bool:
    p = _cache_path(symbol, interval, target_date)
    return p.exists() and p.stat().st_size > 0


def load_interval_data(symbol: str, interval: str,
                       target_date: date) -> pd.DataFrame | None:
    """Read cached candles. Returns None if absent or unreadable."""
    p = _cache_path(symbol, interval, target_date)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, parse_dates=["date"], index_col="date")
    except Exception as exc:
        print(f"[data] {symbol}: cache unreadable ({exc}), will re-fetch")
        return None
    if df.empty:
        return None
    if df.index.tz is None:
        df.index = df.index.tz_localize(ist_clock.IST)
    else:
        df.index = df.index.tz_convert(ist_clock.IST)
    return df


def _save_interval_data(symbol: str, interval: str, target_date: date,
                        df: pd.DataFrame) -> None:
    p = _cache_path(symbol, interval, target_date)
    tmp = p.with_suffix(".tmp")
    df.to_csv(tmp, index_label="date")
    tmp.replace(p)  # atomic: never leaves a half-written cache file


def purge_interval_data(symbol: str, interval: str, target_date: date) -> None:
    p = _cache_path(symbol, interval, target_date)
    if p.exists():
        p.unlink()
        print(f"[data] purged cache {p.name}")


def file_has_future_candles(symbol: str, interval: str, target_date: date,
                            cutoff: datetime) -> bool:
    """
    True if the cache contains candles beyond the cutoff.

    This catches the nasty case: a BACKTEST run wrote a full day of candles,
    then a LIVE run at 10:00 reads that cache and sees the whole session
    including the future. Any signal built on it is clairvoyant.
    """
    df = load_interval_data(symbol, interval, target_date)
    if df is None or df.empty:
        return False
    return bool((df.index > cutoff).any())


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------
def _fetch_from_kite(kite_api, token: int, interval: str,
                     start: datetime, end: datetime,
                     symbol: str) -> pd.DataFrame:
    """One rate-limited Kite historical call, with retries on transient errors."""
    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        _kite_limiter.acquire()
        try:
            raw = kite_api.historical_data(
                instrument_token=token,
                from_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=end.strftime("%Y-%m-%d %H:%M:%S"),
                interval=interval,
            )
            if not raw:
                return pd.DataFrame()
            df = pd.DataFrame(raw)
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            if df.index.tz is None:
                df.index = df.index.tz_localize(ist_clock.IST)
            else:
                df.index = df.index.tz_convert(ist_clock.IST)
            return df[["open", "high", "low", "close", "volume"]]
        except Exception as exc:
            last_exc = exc
            if attempt < config.MAX_RETRIES:
                wait = config.RETRY_BACKOFF_SECS * attempt
                print(f"[data] {symbol}: attempt {attempt} failed ({exc}), "
                      f"retrying in {wait:.0f}s")
                _time.sleep(wait)

    print(f"[data] {symbol}: FAILED after {config.MAX_RETRIES} attempts: {last_exc}")
    return pd.DataFrame()


def download_historical_data(df_ref: pd.DataFrame, target_date: date,
                             kite_api, mode: str = BACKTEST,
                             cap_to_now: bool = True,
                             lookback_days: int | None = None) -> dict[str, pd.DataFrame]:
    """
    Full backfill for every symbol in the watchlist. Cache-first.

    Returns {symbol: DataFrame}. Symbols that fail are absent from the dict
    and are listed in the summary, never silently replaced with empty frames.
    """
    lookback_days = lookback_days or config.LOOKBACK_DAYS
    interval = config.INTERVAL

    cutoff = None
    if mode.upper() == LIVE and cap_to_now:
        cutoff = ist_clock.now_ist()

    tokens: dict[str, int] = {}
    for _, row in df_ref.iterrows():
        tok = row.get(config.COL_ZERODHA_TOKEN)
        if pd.notna(tok):
            tokens[str(row[config.COL_SYMBOL])] = int(tok)

    if not tokens:
        raise RuntimeError(
            "no resolved Zerodha tokens in the watchlist -- run "
            "token_mgmt.update_instrument_tokens first"
        )

    start = ist_clock.combine_ist(target_date - timedelta(days=lookback_days),
                                  ist_clock.MARKET_OPEN)
    end = ist_clock.combine_ist(target_date, ist_clock.MARKET_CLOSE)
    if cutoff is not None:
        end = min(end, cutoff)

    out: dict[str, pd.DataFrame] = {}
    to_fetch: dict[str, int] = {}
    reused = 0

    for symbol, token in tokens.items():
        if config.FORCE_REFRESH or not config.REUSE_CACHE:
            to_fetch[symbol] = token
            continue
        cached = load_interval_data(symbol, interval, target_date)
        if cached is None or cached.empty:
            to_fetch[symbol] = token
            continue
        if cutoff is not None and (cached.index > cutoff).any():
            # cache written by a different mode -- contains the future
            print(f"[data] {symbol}: cache contains candles after cutoff, re-fetching")
            purge_interval_data(symbol, interval, target_date)
            to_fetch[symbol] = token
            continue
        out[symbol] = cached
        reused += 1

    print(f"[data] {reused} symbol(s) served from cache, "
          f"{len(to_fetch)} to download ({interval}, {lookback_days}d lookback)")

    if to_fetch:
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=config.KITE_MAX_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_from_kite, kite_api, tok, interval,
                            start, end, sym): sym
                for sym, tok in to_fetch.items()
            }
            done = 0
            for fut in as_completed(futures):
                sym = futures[fut]
                done += 1
                try:
                    df = fut.result()
                except Exception as exc:
                    print(f"[data] {sym}: worker raised {exc}")
                    failures.append(sym)
                    continue
                if df.empty:
                    failures.append(sym)
                    continue
                df, _ = _drop_unclosed_candles(df, config.INTERVAL_MINUTES,
                                               cutoff, sym)
                if df.empty:
                    failures.append(sym)
                    continue
                _save_interval_data(sym, interval, target_date, df)
                out[sym] = df
                if done % 25 == 0:
                    print(f"[data] {done}/{len(to_fetch)} downloaded")

        if failures:
            print(f"[data] {len(failures)} symbol(s) returned no data: "
                  f"{', '.join(failures[:15])}"
                  + (f" ... +{len(failures) - 15} more" if len(failures) > 15 else ""))

    print(f"[data] ready: {len(out)}/{len(tokens)} symbols")
    return out


def update_incremental_data(df_ref: pd.DataFrame, target_date: date,
                            kite_api) -> dict[str, pd.DataFrame]:
    """
    LIVE top-up. Fetches only candles newer than what the cache already holds.

    This is what makes a 5-minute polling loop cheap: after the first full
    backfill each cycle pulls a handful of bars per symbol, not 30 days.
    """
    interval = config.INTERVAL
    cutoff = ist_clock.now_ist()
    out: dict[str, pd.DataFrame] = {}
    topped = 0

    for _, row in df_ref.iterrows():
        tok = row.get(config.COL_ZERODHA_TOKEN)
        if pd.isna(tok):
            continue
        symbol = str(row[config.COL_SYMBOL])
        token = int(tok)

        cached = load_interval_data(symbol, interval, target_date)
        if cached is None or cached.empty:
            continue

        last_ts = cached.index.max()
        if last_ts + timedelta(minutes=config.INTERVAL_MINUTES) > cutoff:
            out[symbol] = cached  # nothing new has closed yet
            continue

        fresh = _fetch_from_kite(kite_api, token, interval,
                                 last_ts.to_pydatetime(), cutoff, symbol)
        if fresh.empty:
            out[symbol] = cached
            continue

        fresh, _ = _drop_unclosed_candles(fresh, config.INTERVAL_MINUTES,
                                          cutoff, symbol, "(incremental)")
        merged = pd.concat([cached, fresh])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()

        if len(merged) > len(cached):
            _save_interval_data(symbol, interval, target_date, merged)
            topped += 1
        out[symbol] = merged

    print(f"[data] incremental: {topped} symbol(s) topped up, {len(out)} ready")
    return out


def validate_candles(df: pd.DataFrame, symbol: str = "") -> list[str]:
    """Return a list of problems. Empty means clean."""
    problems: list[str] = []
    if df.empty:
        return [f"{symbol}: empty"]
    if df.index.duplicated().any():
        problems.append(f"{symbol}: {int(df.index.duplicated().sum())} duplicate timestamps")
    if not df.index.is_monotonic_increasing:
        problems.append(f"{symbol}: index not sorted")
    bad_hl = (df["high"] < df["low"]).sum()
    if bad_hl:
        problems.append(f"{symbol}: {int(bad_hl)} bars with high < low")
    outside = ((df["close"] > df["high"]) | (df["close"] < df["low"])).sum()
    if outside:
        problems.append(f"{symbol}: {int(outside)} bars with close outside high-low")
    return problems

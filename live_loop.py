"""
STEP 12 — LIVE session loop. Closed candles only, always.

THE TIMING RULE (Harish, 01-Aug-26)
-----------------------------------
A 5-minute candle opening at 09:15 covers 09:15:00 to 09:19:59 and CLOSES at
09:20:00. Data for it is fetched at 09:20:05 -- five seconds after close, so
the broker has definitely written the bar.

    09:15:00  candle opens
    09:19:59  last tick in the candle
    09:20:00  candle closes
    09:20:05  <-- fetch happens here
    09:20:06  indicators recomputed, signals evaluated, orders decided

Nothing in this system ever reads a forming candle, in LIVE or in BACKTEST.
A forming candle's close changes with every tick, so any indicator built on
it changes too -- that is repainting, and it is why a strategy can backtest
beautifully and lose money live.

The five-second buffer is not superstition. Broker candle APIs write the bar
asynchronously; querying at exactly 09:20:00.000 frequently returns the
previous bar, or the new bar with a partial close.
"""

from __future__ import annotations

import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Callable

import config
import ist_clock


def candle_close_times(trade_date: date,
                       interval_minutes: int | None = None,
                       first_open: dtime | None = None,
                       last_open: dtime | None = None) -> list[datetime]:
    """
    The moment each candle CLOSES, which is one interval after it opens.

    The 09:15 candle closes at 09:20. So the returned list starts at 09:20,
    not 09:15 -- an easy off-by-one that would have the loop acting on a bar
    five minutes before it exists.
    """
    interval_minutes = interval_minutes or config.INTERVAL_MINUTES
    first_open = first_open or ist_clock.MARKET_OPEN
    last_open = last_open or dtime(*map(int, config.MATRIX_LAST_SLOT.split(":")))

    out: list[datetime] = []
    cur = ist_clock.combine_ist(trade_date, first_open)
    stop = ist_clock.combine_ist(trade_date, last_open)
    step = timedelta(minutes=interval_minutes)
    while cur <= stop:
        out.append(cur + step)
        cur += step
    return out


def fetch_times(trade_date: date, buffer_secs: int | None = None,
                **kwargs) -> list[datetime]:
    """Candle close plus the safety buffer -- when the loop actually acts."""
    buffer_secs = config.CANDLE_CLOSE_BUFFER_SECS if buffer_secs is None else buffer_secs
    return [t + timedelta(seconds=buffer_secs)
            for t in candle_close_times(trade_date, **kwargs)]


def next_fetch_time(now: datetime | None = None,
                    trade_date: date | None = None) -> datetime | None:
    """The next scheduled wake-up after `now`, or None if the session is done."""
    now = now or ist_clock.now_ist()
    trade_date = trade_date or now.date()
    for t in fetch_times(trade_date):
        if t > now:
            return t
    return None


def sleep_until(target: datetime, check_interval: float = 1.0,
                should_stop: Callable[[], bool] | None = None) -> bool:
    """
    Block until `target`. Returns True if it arrived, False if interrupted.

    Sleeps in short slices rather than one long sleep so Ctrl-C is responsive
    and a stop flag can be honoured. A five-minute uninterruptible sleep in a
    trading loop is how you end up unable to stop a running system.
    """
    while True:
        now = ist_clock.now_ist()
        if now >= target:
            return True
        if should_stop is not None and should_stop():
            return False
        time.sleep(min(check_interval, max((target - now).total_seconds(), 0.05)))


def run_live_session(on_candle: Callable[[datetime, str], None],
                     trade_date: date | None = None,
                     should_stop: Callable[[], bool] | None = None) -> None:
    """
    Drive one trading day.

    on_candle(fetch_time, candle_slot) is called once per closed candle, at
    close + buffer. `candle_slot` is the OPEN time label of the candle that
    just closed -- the column it belongs in on the matrix sheets.

    Cycles that overrun their slot are reported and skipped rather than
    queued. Running a stale cycle late is worse than missing it: it acts on
    a signal the market has already moved past.
    """
    trade_date = trade_date or ist_clock.today_ist()
    closes = candle_close_times(trade_date)
    buffer_secs = config.CANDLE_CLOSE_BUFFER_SECS
    interval = config.INTERVAL_MINUTES

    print(f"[live] session {trade_date:%d-%b-%y}: {len(closes)} candles, "
          f"fetching at close + {buffer_secs}s")
    print(f"[live] first fetch {closes[0] + timedelta(seconds=buffer_secs):%H:%M:%S}, "
          f"last {closes[-1] + timedelta(seconds=buffer_secs):%H:%M:%S}")

    for close_at in closes:
        fetch_at = close_at + timedelta(seconds=buffer_secs)
        candle_open = close_at - timedelta(minutes=interval)
        slot = candle_open.strftime("%H:%M")

        now = ist_clock.now_ist()
        if now > fetch_at + timedelta(minutes=interval):
            continue  # started mid-session; this candle is long gone

        if now < fetch_at:
            if not sleep_until(fetch_at, should_stop=should_stop):
                print("[live] stop requested")
                return

        started = ist_clock.now_ist()
        late = (started - fetch_at).total_seconds()
        if late > config.LIVE_POLL_TOLERANCE_SECS:
            print(f"[live] {slot}: woke {late:.1f}s late")

        try:
            on_candle(started, slot)
        except KeyboardInterrupt:
            print("[live] interrupted")
            return
        except Exception as exc:
            # One bad cycle must not kill the session. Report and continue --
            # there is an open position to manage regardless.
            print(f"[live] {slot}: cycle FAILED: {exc}")

        elapsed = (ist_clock.now_ist() - started).total_seconds()
        if elapsed > interval * 60:
            print(f"[live] {slot}: cycle took {elapsed:.0f}s, longer than the "
                  f"{interval}-minute candle -- the next slot will be skipped")

    print("[live] session complete")


if __name__ == "__main__":
    td = date(2026, 8, 3)
    closes = candle_close_times(td)
    fetches = fetch_times(td)

    print(f"candles in session: {len(closes)}\n")
    print(f"{'candle opens':>14}{'closes':>10}{'fetch at':>12}")
    for i in (0, 1, 2):
        op = closes[i] - timedelta(minutes=config.INTERVAL_MINUTES)
        print(f"{op:%H:%M:%S>14}".replace(">14", "") .rjust(14)
              + f"{closes[i]:%H:%M:%S}".rjust(10)
              + f"{fetches[i]:%H:%M:%S}".rjust(12))
    print("   ...")
    op = closes[-1] - timedelta(minutes=config.INTERVAL_MINUTES)
    print(f"{op:%H:%M:%S}".rjust(14) + f"{closes[-1]:%H:%M:%S}".rjust(10)
          + f"{fetches[-1]:%H:%M:%S}".rjust(12))

    assert closes[0].strftime("%H:%M:%S") == "09:20:00", "09:15 candle must close at 09:20"
    assert fetches[0].strftime("%H:%M:%S") == "09:20:05", "fetch must be close + 5s"
    print("\n09:15 candle -> closes 09:20:00 -> fetched 09:20:05.  Correct.")
    print("live_loop self-check passed")

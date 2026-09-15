"""
SB-STEP 13 -- Decide whether it's safe to shut the PC down, and do it.

Hard safety rule, not configurable: NEVER shuts down while `tracking` (open
positions) is non-empty. A shutdown mid-trade would kill Stop Loss
monitoring on a real position -- that risk is always worse than the power
bill.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime

import sb_config as cfg
import sb_idle_check as idle_check

log = logging.getLogger("sb_bot")


def should_shutdown(watching: dict, tracking: dict, last_activity_at: datetime) -> bool:
    if not cfg.AUTO_SHUTDOWN_ENABLED:
        return False

    if tracking:
        return False  # hard rule -- an open position is being tracked, never shut down

    if watching:
        return False  # actively waiting on an entry trigger

    now = datetime.now()
    timer_start_today = now.replace(hour=cfg.SHUTDOWN_TIMER_START_HOUR,
                                     minute=cfg.SHUTDOWN_TIMER_START_MINUTE,
                                     second=0, microsecond=0)
    if now < timer_start_today:
        return False  # before the configured start hour -- clock hasn't started yet today

    # the idle clock only counts from whichever is later: the last real
    # activity, or today's configured start time
    effective_start = max(last_activity_at, timer_start_today)
    idle_minutes = (now - effective_start).total_seconds() / 60.0
    if idle_minutes < cfg.IDLE_SHUTDOWN_MINUTES:
        return False

    if cfg.AUTO_SHUTDOWN_REQUIRE_OS_IDLE:
        os_idle_seconds = idle_check.get_system_idle_seconds()
        if os_idle_seconds is None:
            log.warning("Could not detect keyboard/mouse idle time -- skipping auto-shutdown this check "
                        "(AUTO_SHUTDOWN_REQUIRE_OS_IDLE is True in sb_config.py)")
            return False
        if os_idle_seconds < cfg.IDLE_SHUTDOWN_MINUTES * 60:
            return False

    return True


def shutdown_pc() -> None:
    log.warning("=== No sheet activity and no PC activity for %s minutes. "
                "Shutting down in %s seconds. Run 'shutdown /a' NOW to cancel. ===",
                cfg.IDLE_SHUTDOWN_MINUTES, cfg.SHUTDOWN_WARNING_SECONDS)
    subprocess.run([
        "shutdown", "/s", "/t", str(cfg.SHUTDOWN_WARNING_SECONDS),
        "/c", "sb_live_monitor: no activity, saving power. Run 'shutdown /a' to cancel.",
    ], check=False)

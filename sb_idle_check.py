"""
SB-STEP 12 -- System idle detection (Windows), used only to decide whether
it's safe to auto-shutdown the PC when nothing is happening.

Best-effort: if this can't detect idle time (non-Windows, or the API call
fails), it returns None and sb_live_monitor.py treats that as "can't
confirm the user is away" -- see sb_config.AUTO_SHUTDOWN_REQUIRE_OS_IDLE.
"""
from __future__ import annotations


def get_system_idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input, system-wide. None if unavailable."""
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        millis_idle = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return millis_idle / 1000.0
    except Exception:
        return None

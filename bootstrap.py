"""
STEP 1 — Install required packages before anything else runs.

Import this first from run_TW_ALL.py / run_MACD.py. It checks what's missing and installs
only that, so a normal run costs nothing.

Deliberately NOT auto-upgrading anything already installed: silently bumping
a broker SDK version mid-project is how a working pipeline breaks overnight.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys

# (import name, pip name) -- these differ often enough to be worth stating
REQUIREMENTS: list[tuple[str, str]] = [
    ("pandas", "pandas"),
    ("numpy", "numpy"),
    ("openpyxl", "openpyxl"),
    ("requests", "requests"),
    ("pytz", "pytz"),
    ("pyotp", "pyotp"),                    # Angel One TOTP, no browser needed
    ("kiteconnect", "kiteconnect"),        # Zerodha
    ("SmartApi", "smartapi-python"),       # Angel One
    ("logzero", "logzero"),                # smartapi-python dependency
    ("selenium", "selenium"),              # Zerodha browser login (>=4.6)
    ("webdriver_manager", "webdriver-manager"),  # chromedriver resolution
]

# Windows ships no timezone database. Every scheduling decision here depends
# on Asia/Kolkata resolving, so this is not optional on Windows.
if sys.platform.startswith("win"):
    REQUIREMENTS.append(("tzdata", "tzdata"))


def _installed(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError):
        return False


def missing_packages() -> list[tuple[str, str]]:
    return [(imp, pip) for imp, pip in REQUIREMENTS if not _installed(imp)]


def install(packages: list[str], upgrade_pip: bool = False) -> None:
    if upgrade_pip:
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"],
                       check=False)
    cmd = [sys.executable, "-m", "pip", "install", *packages]
    print(f"[bootstrap] running: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"pip install failed with exit code {result.returncode}. "
            f"Install manually: pip install {' '.join(packages)}"
        )


def ensure_requirements(interactive: bool = True) -> None:
    """
    Install anything missing. Call at the very top of the pipeline.

    interactive=True asks before installing, because silently installing
    packages into whatever interpreter happens to be active is rude and
    occasionally destructive.
    """
    if sys.version_info < (3, 10):
        raise RuntimeError(
            f"Python 3.10+ required, running {sys.version_info.major}."
            f"{sys.version_info.minor}"
        )

    missing = missing_packages()
    if not missing:
        print("[bootstrap] all required packages present")
        return

    names = [pip for _, pip in missing]
    print(f"[bootstrap] missing: {', '.join(names)}")
    print(f"[bootstrap] target interpreter: {sys.executable}")

    if interactive:
        answer = input("[bootstrap] install these now? (y/n): ").strip().lower()
        if answer != "y":
            raise RuntimeError("required packages not installed; cannot continue")

    install(names)

    still_missing = missing_packages()
    if still_missing:
        raise RuntimeError(
            f"still missing after install: {[p for _, p in still_missing]}. "
            f"Check whether pip installed into a different interpreter than "
            f"{sys.executable}"
        )
    print("[bootstrap] all packages installed")


def requirements_txt() -> str:
    return "\n".join(pip for _, pip in REQUIREMENTS)


if __name__ == "__main__":
    print(f"Python {sys.version}")
    print(f"Interpreter: {sys.executable}\n")
    for imp, pip in REQUIREMENTS:
        print(f"  {'OK  ' if _installed(imp) else 'MISS'}  {pip}")
    ensure_requirements(interactive=True)

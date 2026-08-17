"""
STEP 2 — Log in to Angel One and Zerodha. Once per day, no browser automation.

HOW EACH BROKER LOGS IN
-----------------------
ANGEL ONE -- no browser, ever. SmartAPI's generateSession() takes client code
+ PIN + a TOTP that pyotp computes locally. Fully headless, officially
supported, already unattended. There is no browser step for Selenium to
automate here: driving the Angel One website would yield a web session
cookie, and SmartAPI does not accept those.

ZERODHA -- Kite Connect's request_token arrives on a browser redirect. That
is how OAuth works and no API skips it. Three paths, tried in order:

    1. cached access_token for today   -> no login at all
    2. Selenium auto-login             -> browser driven start to finish
    3. manual redirect paste           -> fallback if the browser login breaks

Path 1 is what runs almost always. The browser opens at most once per trading
day; every re-run after that is instant. See selenium_auth.py for path 2.

CREDENTIALS
-----------
Read from JSON in 01_JSON_Files. Never hardcoded, never printed, never
logged. The TOTP value generated at runtime is a live credential too and is
equally never logged.

    harish_angel_one.json : api_key, client_id, password, totp_secret
    harish_zerodha.json   : user_id, password, totp_secret, api_key,
                            api_secret, access_token
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import config
import ist_clock
import paths


class AuthError(RuntimeError):
    """Authentication failed. Never returns a partially working session."""


# --------------------------------------------------------------------------
# credential loading
# --------------------------------------------------------------------------
def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise AuthError(f"{what} credentials file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise AuthError(f"{what} credentials file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise AuthError(f"{what} credentials file must contain a JSON object: {path}")
    return data


def _require(creds: dict, keys: list[str], what: str, path: Path) -> None:
    missing = [k for k in keys if not creds.get(k)]
    if missing:
        raise AuthError(
            f"{what} credentials missing key(s) {missing} in {path.name}. "
            f"Values are not shown here by design -- open the file yourself."
        )


# --------------------------------------------------------------------------
# token cache (date-keyed, so a same-day re-run does not log in again)
# --------------------------------------------------------------------------
def _read_cache(cache_file: Path, today: date) -> dict | None:
    """Return the whole cache blob if it was written today, else None."""
    if not cache_file.exists():
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(blob, dict) or blob.get("date") != today.isoformat():
        return None
    return blob


def _read_cached_token(cache_file: Path, today: date) -> str | None:
    blob = _read_cache(cache_file, today)
    return (blob or {}).get("access_token") or None


def _write_cache(cache_file: Path, today: date, **fields) -> None:
    """Write a date-stamped cache blob. Values are credentials -- never echoed."""
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    blob = {"date": today.isoformat(), **fields}
    with open(cache_file, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=2)
    print(f"[auth] token cached for {today.isoformat()} -> {cache_file.name}")


def _write_cached_token(cache_file: Path, today: date, token: str) -> None:
    _write_cache(cache_file, today, access_token=token)


# --------------------------------------------------------------------------
# ANGEL ONE -- fully headless
# --------------------------------------------------------------------------
def initialize_angel_one(force_login: bool = False):
    """
    Return an authenticated SmartConnect session.

    Raises AuthError on any failure. Never returns a half-live client -- a
    session object that failed to authenticate will produce confusing errors
    several modules downstream instead of here.
    """
    try:
        import pyotp
        from SmartApi import SmartConnect
    except ImportError as exc:
        raise AuthError(
            "Angel One dependencies missing. Run: pip install smartapi-python pyotp"
        ) from exc

    creds = _load_json(paths.ANGEL_CRED_FILE, "Angel One")
    _require(creds, ["api_key", "client_id", "password", "totp_secret"],
             "Angel One", paths.ANGEL_CRED_FILE)

    today = ist_clock.today_ist()
    smart = SmartConnect(api_key=creds["api_key"])

    # --- cached session ----------------------------------------------------
    # Angel One issues three tokens and all three are needed to restore a
    # session properly: jwt (REST auth), refresh (renewal), feed (websocket).
    cached = None if force_login else _read_cache(paths.ANGEL_TOKEN_CACHE, today)
    if cached and cached.get("jwt_token") and cached.get("refresh_token"):
        try:
            smart.setAccessToken(cached["jwt_token"])
            smart.setRefreshToken(cached["refresh_token"])
            if cached.get("feed_token"):
                smart.setFeedToken(cached["feed_token"])
            print("[auth] Angel One: reused cached session (no login)")
            return smart
        except Exception:
            print("[auth] Angel One: cached session unusable, logging in fresh")

    try:
        otp = pyotp.TOTP(creds["totp_secret"]).now()  # live credential, never logged
    except Exception as exc:
        raise AuthError(
            "Could not generate TOTP from totp_secret. It must be the base32 "
            "secret from the Angel One 2FA setup, not a 6-digit code."
        ) from exc

    try:
        session = smart.generateSession(creds["client_id"], creds["password"], otp)
    except Exception as exc:
        raise AuthError(f"Angel One login call failed: {exc}") from exc

    if not session or not session.get("status"):
        msg = (session or {}).get("message", "no message returned")
        raise AuthError(f"Angel One login rejected: {msg}")

    # Read the tokens off the SmartConnect object, NOT off the response dict.
    #
    # session["data"]["jwtToken"] comes back with a "Bearer " prefix already
    # baked in. Feeding that straight into setAccessToken() on a later run
    # produces a header reading "Bearer Bearer <token>" and every API call
    # fails authentication. generateSession() has already stored the clean
    # values on the object, so take them from there.
    _write_cache(
        paths.ANGEL_TOKEN_CACHE, today,
        jwt_token=smart.access_token,
        refresh_token=smart.refresh_token,
        feed_token=getattr(smart, "feed_token", None),
    )
    print("[auth] Angel One: fresh login OK")
    return smart


# --------------------------------------------------------------------------
# ZERODHA -- cached token, one manual redirect paste per day
# --------------------------------------------------------------------------
def initialize_zerodha(force_login: bool = False, interactive: bool = True,
                       method: str | None = None):
    """
    Return an authenticated KiteConnect session.

    Order of attempts:
      1. Cached access_token for today -- no login at all, no browser.
      2. Selenium auto-login (config.ZERODHA_AUTH_METHOD == "selenium").
      3. Manual redirect paste, if Selenium fails and fallback is enabled.

    Step 1 is the one that matters day to day: the browser opens once per
    trading day at most, and every re-run after that is instant.
    """
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:
        raise AuthError("Zerodha dependency missing. Run: pip install kiteconnect") from exc

    creds = _load_json(paths.ZERODHA_CRED_FILE, "Zerodha")
    _require(creds, ["api_key", "api_secret"], "Zerodha", paths.ZERODHA_CRED_FILE)

    today = ist_clock.today_ist()
    kite = KiteConnect(api_key=creds["api_key"])
    method = (method or config.ZERODHA_AUTH_METHOD).lower()

    # --- 1. cached token ---------------------------------------------------
    cached = None if force_login else _read_cached_token(paths.ZERODHA_TOKEN_CACHE, today)
    if cached:
        try:
            kite.set_access_token(cached)
            kite.profile()  # cheapest call that proves the token works
            print("[auth] Zerodha: reused cached token (no login, no browser)")
            return kite
        except Exception:
            print("[auth] Zerodha: cached token expired, fresh login needed")

    # --- 2. Selenium -------------------------------------------------------
    if method == "selenium":
        try:
            import selenium_auth
            token = selenium_auth.zerodha_login_selenium(kite, creds)
            kite.set_access_token(token)
            _write_cached_token(paths.ZERODHA_TOKEN_CACHE, today, token)
            print("[auth] Zerodha: Selenium login OK")
            return kite
        except Exception as exc:
            message = str(exc)
            print(f"[auth] Zerodha: Selenium login failed -- {message}")

            # If Zerodha's login service is down, the manual path is equally
            # dead -- he would open the same URL and get the same 503. Fail
            # straight away rather than parking on an input prompt.
            if "503" in message or "is down" in message or "service is down" in message:
                raise AuthError(
                    "Zerodha's login service is unavailable (503). Manual "
                    "paste would hit the same page. Nothing to do but retry "
                    "later -- Kite Connect login is routinely down during "
                    "weekend and overnight maintenance."
                ) from exc

            if not config.SELENIUM_FALLBACK_TO_MANUAL:
                raise AuthError(f"Selenium login failed and fallback is off: {message}")
            if not interactive:
                raise AuthError(
                    f"Selenium login failed and no interactive fallback available: {message}"
                )
            print("[auth] falling back to manual paste")

    # --- 3. manual paste ---------------------------------------------------
    if not interactive:
        raise AuthError(
            "Zerodha access token is stale and interactive=False. Run the "
            "pipeline once interactively to refresh it for today."
        )

    login_url = kite.login_url()
    print("\n" + "=" * 68)
    print("ZERODHA LOGIN -- once per trading day")
    print("=" * 68)
    print("1. Open this URL in your browser and log in:\n")
    print(f"   {login_url}\n")
    print("2. You land on a redirect URL containing request_token=...")
    print("3. Paste the FULL redirect URL below.\n")
    pasted = input("Redirect URL (or bare request_token): ").strip()
    print("=" * 68 + "\n")

    request_token = _extract_request_token(pasted)
    if not request_token:
        raise AuthError("No request_token found in what you pasted.")

    try:
        data = kite.generate_session(request_token, api_secret=creds["api_secret"])
    except Exception as exc:
        raise AuthError(
            f"Zerodha session exchange failed: {exc}. A request_token is "
            f"single-use and expires within minutes -- get a fresh one."
        ) from exc

    token = data["access_token"]
    kite.set_access_token(token)
    _write_cached_token(paths.ZERODHA_TOKEN_CACHE, today, token)
    print("[auth] Zerodha: fresh login OK")
    return kite


def _extract_request_token(pasted: str) -> str | None:
    """Accept a full redirect URL or a bare token."""
    if not pasted:
        return None
    if "request_token" in pasted:
        qs = parse_qs(urlparse(pasted).query)
        values = qs.get("request_token")
        if values:
            return values[0]
    if "://" not in pasted and len(pasted) > 8:
        return pasted  # user pasted the bare token
    return None


# --------------------------------------------------------------------------
def initialize_all(interactive: bool = True) -> dict[str, Any]:
    """Log in to both. Caller does sys.exit(1) on AuthError."""
    return {
        "angel": initialize_angel_one(),
        "kite": initialize_zerodha(interactive=interactive),
    }


if __name__ == "__main__":
    paths.ensure_dirs()
    try:
        sessions = initialize_all()
    except AuthError as exc:
        print(f"\n[auth] FAILED: {exc}")
        sys.exit(1)
    print("\n[auth] both sessions live")

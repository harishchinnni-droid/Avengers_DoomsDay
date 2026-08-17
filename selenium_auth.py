"""
STEP 2b — Selenium auto-login for Zerodha.

Built on Harish's previously working implementation. His version had already
solved several things my first pass got wrong, and those are preserved here
with the reasoning kept:

  * LOGIN HOST. He used https://kite.trade/connect/login, not the
    kite.zerodha.com URL that KiteConnect.login_url() returns. On 01-Aug-26
    the kite.zerodha.com endpoint served "503 Service Unavailable". kite.trade
    is tried first for that reason, with the SDK URL as fallback.
  * XPATH over CSS. //input[@type='text'] survives Kite's class-name churn
    in a way that #userid does not.
  * STALENESS WAIT. Waiting for the password field to go stale is a real
    signal that the page advanced. A fixed sleep is a guess.
  * IFRAME SEARCH. Kite has served the TOTP step inside an iframe. Without
    switching into frames the field is simply invisible to Selenium.
  * AUTO-SUBMIT RACE. Kite's TOTP field submits itself on the 6th digit, so
    a following RETURN can hit a detached element. Swallowed, because the
    login has almost certainly already gone through.

WHY THERE IS NO ANGEL ONE FUNCTION HERE
---------------------------------------
Angel One SmartAPI has no browser step. generateSession(client_id, pin, totp)
is a direct API call that pyotp automates end to end. A Selenium session on
their website yields a web cookie that SmartAPI does not accept.

CREDENTIALS
-----------
Read from 01_JSON_Files/harish_zerodha.json. The password and the generated
TOTP are typed into the browser and never printed, logged, or written into
the debug dumps.
"""

from __future__ import annotations

import json
import os
import time
import traceback
import urllib.parse as urlparse
from pathlib import Path

import config
import ist_clock
import paths


class SeleniumLoginError(RuntimeError):
    """Browser login failed. Never returns a partial session."""


# --------------------------------------------------------------------------
# login URLs, tried in order
# --------------------------------------------------------------------------
def login_urls(api_key: str, kite=None) -> list[str]:
    """
    kite.trade first -- it is what worked historically and it stayed up on
    01-Aug-26 when the SDK's kite.zerodha.com URL returned 503.
    """
    urls = [f"https://kite.trade/connect/login?v=3&api_key={api_key}"]
    if kite is not None:
        try:
            sdk_url = kite.login_url()
            if sdk_url not in urls:
                urls.append(sdk_url)
        except Exception:
            pass
    fallback = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
    if fallback not in urls:
        urls.append(fallback)
    return urls


_OUTAGE_MARKERS = (
    "503 service unavailable",
    "no server is available to handle this request",
    "502 bad gateway",
    "504 gateway",
    "service temporarily unavailable",
)


def page_is_outage(page_source: str) -> str | None:
    """
    Return the matched outage marker, or None.

    Distinguishing "Zerodha is down" from "my selectors broke" matters: one is
    fixed by waiting, the other by editing code. Without this check a 503
    presents as 'could not find the user id field', which sends you off
    rewriting selectors that were never the problem.
    """
    if not page_source:
        return None
    low = page_source.lower()
    for marker in _OUTAGE_MARKERS:
        if marker in low:
            return marker
    return None


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def _make_driver(headless: bool):
    """
    Chrome driver, with the container/cloud accommodations from his version.

    Driver resolution order:
      1. CHROMEDRIVER_PATH env var, if set
      2. webdriver_manager, if installed
      3. Selenium Manager (built into Selenium 4.6+), which needs neither
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
    except ImportError as exc:
        raise SeleniumLoginError("Selenium not installed. Run: pip install selenium") from exc

    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    # Containers give Chrome a tiny /dev/shm; the default shared-memory usage
    # overflows it and Chrome dies immediately after start. Harmless on
    # desktop Windows, so always set rather than gated behind an env var.
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--start-maximized")
    options.add_argument("--window-size=1366,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    chrome_binary = os.environ.get("CHROME_BINARY_LOCATION")
    if chrome_binary:
        options.binary_location = chrome_binary

    service = None
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    if chromedriver_path:
        service = Service(chromedriver_path)
    else:
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
        except ImportError:
            service = None  # Selenium Manager handles it

    try:
        if service is not None:
            return webdriver.Chrome(service=service, options=options)
        return webdriver.Chrome(options=options)
    except Exception as exc:
        raise SeleniumLoginError(
            f"could not start Chrome: {exc}\n"
            f"Chrome must be installed. Set CHROME_BINARY_LOCATION and/or "
            f"CHROMEDRIVER_PATH if they live somewhere non-standard."
        ) from exc


def _dump_debug(driver, tag: str) -> Path | None:
    """Screenshot + HTML on failure. Contains no credentials."""
    try:
        stamp = ist_clock.now_ist().strftime("%Y%m%d_%H%M%S")
        png = paths.JSON_DIR / f"zerodha_login_{tag}_{stamp}.png"
        html = paths.JSON_DIR / f"zerodha_login_{tag}_{stamp}.html"
        driver.save_screenshot(str(png))
        html.write_text(driver.page_source, encoding="utf-8")
        print(f"[selenium] debug dump: {png.name}, {html.name}")
        return png
    except Exception as exc:
        print(f"[selenium] could not write debug dump: {exc}")
        return None


# --------------------------------------------------------------------------
# the login itself
# --------------------------------------------------------------------------
def _open_login_page(driver, urls: list[str], timeout: int) -> str:
    """
    Try each login URL until one serves a real page. Returns the URL that
    worked. Raises if every candidate is down.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    problems: list[str] = []
    for url in urls:
        host = urlparse.urlparse(url).netloc
        print(f"[selenium] trying login host {host} ...")
        try:
            driver.get(url)
        except Exception as exc:
            problems.append(f"{host}: navigation failed ({exc})")
            continue

        outage = page_is_outage(driver.page_source)
        if outage:
            print(f"[selenium] {host} is down -- matched {outage!r}")
            problems.append(f"{host}: {outage}")
            continue

        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='text']"))
            )
            print(f"[selenium] {host} served the login form")
            return url
        except Exception:
            outage = page_is_outage(driver.page_source)
            problems.append(f"{host}: {outage or 'no login form appeared'}")

    raise SeleniumLoginError(
        "no Zerodha login host served a usable page.\n  "
        + "\n  ".join(problems)
        + "\nIf every host reports 503, Zerodha's login service is down -- "
          "that is on their side, not in this code. Kite Connect login is "
          "routinely unavailable during weekend/overnight maintenance. Retry "
          "later, or set ZERODHA_AUTH_METHOD='manual' in config.py to paste "
          "a token by hand once the service is back."
    )


def zerodha_login_selenium(kite, creds: dict,
                           headless: bool | None = None,
                           timeout: int | None = None) -> str:
    """
    Drive the Kite login end to end. Returns a fresh access_token.

    Raises SeleniumLoginError with a message that distinguishes a Zerodha
    outage from a broken selector.
    """
    headless = config.SELENIUM_HEADLESS if headless is None else headless
    timeout = timeout or config.SELENIUM_TIMEOUT_SECS

    for key in ("user_id", "password", "totp_secret", "api_key", "api_secret"):
        if not creds.get(key):
            raise SeleniumLoginError(
                f"harish_zerodha.json is missing {key!r}. Selenium login needs "
                f"user_id, password, totp_secret, api_key and api_secret."
            )

    try:
        import pyotp
        from selenium.common.exceptions import (
            ElementNotInteractableException, StaleElementReferenceException,
        )
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError as exc:
        raise SeleniumLoginError(f"missing dependency: {exc}") from exc

    driver = _make_driver(headless)
    try:
        _open_login_page(driver, login_urls(creds["api_key"], kite), timeout)
        wait = WebDriverWait(driver, timeout)

        # --- credentials -------------------------------------------------
        wait.until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='text']"))
        ).send_keys(creds["user_id"])
        driver.find_element(By.XPATH, "//input[@type='password']").send_keys(
            creds["password"]
        )
        password_field = driver.find_element(By.XPATH, "//input[@type='password']")
        driver.find_element(By.XPATH, "//button[@type='submit']").click()

        # Staleness of the password field is real evidence the page advanced.
        # A fixed sleep is a guess that fails on a slow morning.
        wait.until(EC.staleness_of(password_field))
        print("[selenium] credentials accepted")

        # --- TOTP ---------------------------------------------------------
        # Generated as late as possible: a TOTP lives 30s and the password
        # step can eat most of one.
        totp_value = pyotp.TOTP(creds["totp_secret"]).now()

        def _find_visible_input():
            xpath = ("//input[@type='text' or @type='tel' or @type='number' "
                     "or @type='password']")
            for candidate in driver.find_elements(By.XPATH, xpath):
                try:
                    if candidate.is_displayed() and candidate.is_enabled():
                        return candidate
                except Exception:
                    continue
            return None

        wait.until(lambda d: _find_visible_input() is not None
                   or d.find_elements(By.TAG_NAME, "iframe"))

        totp_input = _find_visible_input()
        in_frame = False
        if totp_input is None:
            # Kite has served this step inside an iframe. Without switching in,
            # the field is invisible to Selenium and this looks like a broken
            # selector when it is not.
            for frame in driver.find_elements(By.TAG_NAME, "iframe"):
                driver.switch_to.frame(frame)
                totp_input = _find_visible_input()
                if totp_input is not None:
                    in_frame = True
                    break
                driver.switch_to.default_content()

        if totp_input is None:
            driver.switch_to.default_content()
            raise SeleniumLoginError("could not locate a visible TOTP input field")

        totp_input.click()
        totp_input.send_keys(totp_value)

        # Kite's TOTP field auto-submits on the 6th digit via its own JS. This
        # RETURN races that redirect: if auto-submit already fired the element
        # may be detached. The login has very likely gone through, so swallow.
        try:
            totp_input.send_keys(Keys.RETURN)
        except (ElementNotInteractableException, StaleElementReferenceException):
            pass

        try:
            driver.find_element(
                By.XPATH, "//button[@type='submit' or contains(text(),'Continue')]"
            ).click()
        except Exception:
            pass

        if in_frame:
            driver.switch_to.default_content()
        print("[selenium] TOTP submitted")

        # --- redirect -------------------------------------------------------
        wait.until(EC.url_contains("request_token"))
        request_token = urlparse.parse_qs(
            urlparse.urlparse(driver.current_url).query
        )["request_token"][0]
        print("[selenium] request_token captured")

    except SeleniumLoginError:
        _dump_debug(driver, "failure")
        driver.quit()
        raise
    except Exception as exc:
        outage = page_is_outage(getattr(driver, "page_source", ""))
        _dump_debug(driver, "error")
        driver.quit()
        if outage:
            raise SeleniumLoginError(
                f"Zerodha login service is down (matched {outage!r}). "
                f"Not a code problem -- retry later."
            ) from exc
        raise SeleniumLoginError(
            f"browser automation failed:\n{traceback.format_exc()}"
        ) from exc

    driver.quit()

    try:
        data = kite.generate_session(request_token, api_secret=creds["api_secret"])
    except Exception as exc:
        raise SeleniumLoginError(
            f"token exchange failed: {exc}. A request_token is single-use and "
            f"expires within minutes."
        ) from exc

    return data["access_token"]


def _extract_request_token(url: str) -> str | None:
    """Kept for tests and for the manual-paste path in broker_auth."""
    if not url or "request_token" not in url:
        return None
    values = urlparse.parse_qs(urlparse.urlparse(url).query).get("request_token")
    return values[0] if values else None


if __name__ == "__main__":
    from kiteconnect import KiteConnect

    paths.ensure_dirs()
    creds = json.loads(paths.ZERODHA_CRED_FILE.read_text(encoding="utf-8"))
    k = KiteConnect(api_key=creds["api_key"])
    print("login URLs in priority order:")
    for u in login_urls(creds["api_key"], k):
        print("   ", u)
    token = zerodha_login_selenium(k, creds, headless=False)
    print(f"[selenium] OK -- token length {len(token)} (value not shown)")

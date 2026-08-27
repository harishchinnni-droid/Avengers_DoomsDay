"""
STEP 6 — Angel One scrip master download.

Angel One publishes its full instrument list as a single public JSON file, no
authentication required. It is large (tens of MB) and changes slowly, so it
is cached to 01_JSON_Files and re-downloaded once per CALENDAR DAY -- the
first run of a given day fetches a fresh copy, every run after that same day
reuses it, regardless of how many hours old it is.

Changed 24-Aug-26, Harish's request -- was a rolling SCRIP_MASTER_MAX_AGE_DAYS
(7 days) window, which could go most of a week trading Tuesday's contract
list without the code ever noticing a new one was published. A calendar-day
check catches new listings/expiries the same morning they appear, and still
only downloads once no matter how many times the pipeline runs that day.

This is what maps a symbol to the `symboltoken` that SmartAPI needs, and it
is also where option contract symbols come from later.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import config
import paths

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)


def _cached_date(path: Path) -> date | None:
    """Calendar date the cached file was last written, or None if absent."""
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime).date()


def download_scrip_master(force: bool = False) -> Path:
    """
    Download the scrip master on the first call of a new calendar day;
    every later call the same day reuses the cached copy as-is. Returns its
    path.
    """
    import requests

    target = paths.ANGEL_SCRIP_MASTER
    cached_date = _cached_date(target)
    today = date.today()

    if not force and cached_date == today:
        print(f"[scrip] cached master is from today ({cached_date}), reusing")
        return target

    print(f"[scrip] downloading Angel One scrip master ...")
    try:
        resp = requests.get(SCRIP_MASTER_URL, timeout=120)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        if target.exists():
            print(f"[scrip] download failed ({exc}); falling back to cached copy "
                  f"from {cached_date}")
            return target
        raise RuntimeError(f"scrip master download failed and no cache exists: {exc}")

    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then rename, so an interrupted download never
    # leaves a truncated JSON that fails to parse on every later run.
    tmp = target.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    tmp.replace(target)

    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"[scrip] saved {len(data):,} instruments ({size_mb:.1f} MB) -> {target.name}")
    return target


def load_scrip_master(force_download: bool = False) -> pd.DataFrame:
    """Return the scrip master as a DataFrame, downloading if needed."""
    path = download_scrip_master(force=force_download)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    df = pd.DataFrame(data)
    for col in ("symbol", "name", "exch_seg", "instrumenttype"):
        if col in df.columns:
            df[col] = df[col].astype(str)
    if "expiry" in df.columns:
        df["expiry_dt"] = pd.to_datetime(df["expiry"], format="%d%b%Y", errors="coerce")
    return df


def resolve_angel_tokens(df_ref: pd.DataFrame,
                         scrip: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Fill the Angel_Token column on the watchlist DataFrame.

    Unresolved symbols get null, same policy as token_mgmt: reported, never
    silently dropped.
    """
    scrip = load_scrip_master() if scrip is None else scrip

    fut = scrip[
        scrip["instrumenttype"].str.upper().isin(["FUTSTK", "FUTIDX"])
        & (scrip["exch_seg"].str.upper() == "NFO")
    ].copy()

    tokens: list[int | None] = []
    failures: list[str] = []

    for _, row in df_ref.iterrows():
        sym = str(row[config.COL_SYMBOL]).upper()
        expiry = row.get(config.COL_EXPIRY)

        cand = fut[fut["name"].str.upper() == sym]
        if cand.empty:
            tokens.append(None)
            failures.append(sym)
            continue

        if pd.notna(expiry) and "expiry_dt" in cand.columns:
            want = pd.Timestamp(expiry).normalize()
            exact = cand[cand["expiry_dt"].dt.normalize() == want]
            if not exact.empty:
                tokens.append(int(exact.iloc[0]["token"]))
                continue
            future = cand[cand["expiry_dt"] >= want].sort_values("expiry_dt")
            if not future.empty:
                tokens.append(int(future.iloc[0]["token"]))
                continue

        tokens.append(int(cand.iloc[0]["token"]))

    df_ref = df_ref.copy()
    df_ref[config.COL_ANGEL_TOKEN] = tokens

    resolved = sum(t is not None for t in tokens)
    print(f"[scrip] Angel tokens resolved {resolved}/{len(df_ref)}")
    if failures:
        print(f"[scrip] unresolved: {', '.join(failures[:15])}"
              + (f" ... +{len(failures) - 15} more" if len(failures) > 15 else ""))
    return df_ref


if __name__ == "__main__":
    paths.ensure_dirs()
    master = load_scrip_master()
    print(f"\ncolumns: {list(master.columns)}")
    print(f"rows: {len(master):,}")

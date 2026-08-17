"""
STANDALONE — incremental backup of the whole project folder to Google
Drive via rclone.

WHY RCLONE, NOT THE GOOGLE DRIVE API DIRECTLY
----------------------------------------------
Talking to the Drive API directly means writing (and maintaining) our own
OAuth token refresh, resumable/chunked upload, retry-on-rate-limit, and
"has this file already changed on the far end" comparison logic. rclone
already does all of that, is a single well-tested binary, and its `copy`
command already only transfers files that are new or changed (size +
modified-time by default, content checksum with --checksum) -- which is
exactly "push only the incremental data." This script is a thin wrapper:
it builds the rclone command, runs it, and logs the result.

ONE-TIME SETUP -- see the numbered steps given alongside this file:
    1. Install rclone (https://rclone.org/downloads/)
    2. Run `rclone config` once, add a remote named 'gdrive' pointing at
       your Google account (interactive browser OAuth, rclone's own
       registered app -- no Google Cloud Console project needed)
    3. Run this script

WHAT'S EXCLUDED BY DEFAULT
---------------------------
01_JSON_Files/ holds live broker credentials and access tokens (Zerodha,
Angel One). Backing those up to Google Drive means a copy of your broker
login ends up in cloud storage -- excluded here by default, deliberately,
not because of a technical limit. Edit EXCLUDE_PATTERNS below if you want
them included too.

Also excluded: __pycache__, Excel lock files (~$...), and .tmp files --
none of that is real content worth a copy.

USAGE
-----
    py sync_to_drive.py                 actually uploads (copy only, never
                                         deletes anything on Drive)
    py sync_to_drive.py --dry-run       show what WOULD be transferred,
                                         changes nothing
    py sync_to_drive.py --mirror        use `rclone sync` instead of
                                         `copy` -- makes Drive match this
                                         folder EXACTLY, including deleting
                                         on Drive whatever no longer
                                         exists locally. Off by default.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime

import paths

REMOTE_NAME = "gdrive"                       # must match the name used in `rclone config`
REMOTE_FOLDER = "Avengers_Backup"             # created automatically on Drive if missing
LOG_FILE = paths.BASE_DIR / "05_Logs" / "gdrive_sync.log"

EXCLUDE_PATTERNS = [
    "01_JSON_Files/**",      # broker credentials & access tokens -- see module docstring
    "**/__pycache__/**",
    "~$*",                   # Excel lock files (a workbook currently open)
    "*.tmp",
]


def build_command(dry_run: bool, mirror: bool) -> list[str]:
    verb = "sync" if mirror else "copy"
    cmd = [
        "rclone", verb,
        str(paths.BASE_DIR),
        f"{REMOTE_NAME}:{REMOTE_FOLDER}",
        "--log-file", str(LOG_FILE),
        "--log-level", "INFO",
        "-v",
    ]
    for pat in EXCLUDE_PATTERNS:
        cmd += ["--exclude", pat]
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be transferred, change nothing")
    ap.add_argument("--mirror", action="store_true",
                    help="use `rclone sync` (deletes on Drive what's gone "
                         "locally) instead of the default `copy`")
    args = ap.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_command(args.dry_run, args.mirror)
    print(f"[gdrive-sync] {datetime.now():%Y-%m-%d %H:%M:%S} -- running:")
    print("  " + " ".join(cmd))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print("\n[gdrive-sync] ERROR: 'rclone' was not found on PATH.")
        print("  Install it from https://rclone.org/downloads/ and make "
              "sure the folder containing rclone.exe is on your PATH, or "
              "edit this script to call its full path instead of just "
              "'rclone'.")
        return 1

    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        print(f"[gdrive-sync] FAILED (exit code {result.returncode}) -- "
              f"see {LOG_FILE} for detail")
        return result.returncode

    print(f"[gdrive-sync] done -- log at {LOG_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

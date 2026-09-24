"""
GIT PUSH -- one command to commit + push 02_Codes to GitHub.

    py git_push.py                         -> auto message "Update 24-Sep-26 16:45"
    py git_push.py "Add Google Sheet sync" -> your own message
    py git_push.py --dry-run               -> show what WOULD be committed, push nothing

Repo   : 02_Codes (already linked to github.com/harishchinnni-droid/Avengers_DoomsDay)
Branch : whatever is checked out (main)

SAFETY -- refuses to commit if any staged file looks like a secret or data:
  *.json, *.xlsx, *.csv, *.log, *.bak*, *.env, token/credential/password in name.
  Credentials live in 01_JSON_Files (outside this repo) and must stay there.

First time on a new PC: git asks for GitHub login once (browser popup via
Git Credential Manager). After that it's silent.
"""
from __future__ import annotations

import fnmatch
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent

BLOCKED_PATTERNS = [
    "*.json", "*.xlsx", "*.xls", "*.csv", "*.log", "*.bak*", "*.env", "*.pkl",
    "*token*", "*credential*", "*password*", "*secret*", "logs/*",
]


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(f"[git] FAILED: git {' '.join(args)}\n{r.stderr.strip() or r.stdout.strip()}")
        sys.exit(1)
    return r.stdout.strip()


def blocked(path: str) -> bool:
    name = path.lower()
    base = name.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(base, p) or fnmatch.fnmatch(name, p) for p in BLOCKED_PATTERNS)


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    msg_args = [a for a in argv[1:] if a != "--dry-run"]
    message = " ".join(msg_args) or f"Update {datetime.now():%d-%b-%y %H:%M}"

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    git("add", "-A")
    staged = [p for p in git("diff", "--cached", "--name-only").splitlines() if p]

    if not staged:
        print("[git] nothing to commit -- already up to date.")
        return 0

    bad = [p for p in staged if blocked(p)]
    if bad:
        git("reset", "-q")                      # unstage everything, touch no files
        print("[git] STOPPED -- these look like secrets/data, not code:")
        for p in bad:
            print(f"        {p}")
        print("      Add them to .gitignore (or delete them), then run again.")
        return 1

    print(f"[git] {len(staged)} file(s) on branch '{branch}':")
    for p in staged:
        print(f"        {p}")

    if dry:
        git("reset", "-q")
        print("[git] dry run -- nothing committed.")
        return 0

    git("commit", "-q", "-m", message)
    print(f"[git] committed: {message}")
    print("[git] pushing ...")
    git("push", "origin", branch)
    print(f"[git] done -> {git('remote', 'get-url', 'origin')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

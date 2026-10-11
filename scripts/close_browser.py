r"""Close a browser left behind by a previous run.

    venv\python.exe scripts\close_browser.py

The agent's browser keeps a persistent profile, and a profile can only
be held by one Chromium at a time. A process that exits while its
browser is still running leaves the profile locked, and the next run
cannot start.

Processes are matched by executable path, never by name: chrome.exe is
also the user's own browser, and this must never touch that.
"""

from __future__ import annotations

import subprocess
import sys

MARKER = "ms-playwright"   # only ever the browser Playwright installed


def main() -> int:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process chrome -ErrorAction SilentlyContinue | "
         f"Where-Object {{ $_.Path -like '*{MARKER}*' }} | "
         "ForEach-Object { $_.Kill(); $_.Id }"],
        capture_output=True, text=True,
    )
    killed = [line for line in result.stdout.split() if line.strip()]

    if killed:
        print(f"Closed {len(killed)} leftover browser process(es).")
        print("Your own Chrome was not touched.")
    else:
        print("No leftover agent browser found -- nothing to close.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

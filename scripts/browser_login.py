"""Sign in to sites in the agent's browser, once.

    venv\\python.exe scripts\\browser_login.py
    venv\\python.exe scripts\\browser_login.py linkedin.com

Opens the agent's browser so you can log in by hand. The session is kept
in the profile at data/browser-profile, so the agent is still signed in
on later runs -- the same way your normal browser remembers you.

Done interactively on purpose. An agent should never be handling your
account password: you type it, into a real browser window, and the agent
only ever inherits the resulting session. That also means two-factor
prompts, passkeys and CAPTCHAs are yours to answer, which is how it
should be.

The profile is separate from your everyday Chrome. Chrome locks its own
profile while running, and pointing an agent at the browser holding
every live session you have is a much larger blast radius than this
needs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.browser as browser  # noqa: E402


def main() -> int:
    site = sys.argv[1] if len(sys.argv) > 1 else "mail.google.com"

    print(f"Opening {site} in the agent's browser...")
    print(f"Profile: {browser.PROFILE_DIR}\n")

    try:
        print(browser.browser_open(site))
    except browser.BrowserError as exc:
        print(f"\nFAILED: {exc}")
        return 1

    print(
        "\nSign in in the window that just opened, then come back here.\n"
        "Leave it on the signed-in page -- do not sign out.\n"
    )
    try:
        input("Press Enter once you are signed in... ")
    except (EOFError, KeyboardInterrupt):
        print()

    try:
        text = browser.browser_read(200)
        signed_in = "accounts.google.com" not in text[:200] and "Sign in" not in text
        print("\nLooks signed in." if signed_in else "\nStill on a sign-in page.")
    except browser.BrowserError:
        pass

    # Closing is what flushes the session to disk. Killing the window
    # instead can lose the cookies that were the point of the exercise.
    browser.close_browser()
    print("Browser closed. The session is saved for the agent to reuse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Driving a real browser.

These tools are not about Gmail. `browser_open`, `browser_read`,
`browser_type` and `browser_click` work on any website -- an internal
portal, a supplier's invoice download page, a form someone has to fill
in every Monday. That generality is the point: it is the difference
between an agent that does the tasks we anticipated and one that can
attempt the tasks we did not.

Three decisions worth knowing about.

**A dedicated profile.** The browser runs with its own profile directory,
so sign-ins persist: log into Gmail once by hand and the agent reuses the
session afterwards, exactly like a normal browser. It is deliberately
*not* the user's real Chrome profile -- Chrome locks that while running,
and pointing an agent at the browser holding every live session is a far
larger blast radius than this needs.

**Visible, always.** headless=False. The browser opens on screen and the
user watches. A headless browser would be faster and completely useless
for the thing this is for.

**The risk problem.** Every other tool in this project has a fixed risk
tier, which is what makes the approval gate meaningful: read_file can
only read, send_email is always irreversible. A generic click breaks
that. The same function can click "Scroll down" or "Delete all mail" --
one tool spanning every risk tier, which the gate cannot classify.

The fix is that the tool inspects what it is about to click. Anything
matching a list of consequential words is refused and must go through
browser_click_confirmed, which is IRREVERSIBLE and so stops for the
human with the exact target on screen. The list lives in code, so a plan
cannot argue its way past it.
"""

from __future__ import annotations

import atexit
import re
import urllib.parse
from pathlib import Path
from typing import Any

from core.config import DATA_DIR
from core.schemas import Risk
from tools.registry import tool

PROFILE_DIR = DATA_DIR / "browser-profile"

# Clicking any of these can spend money, send something to a person, or
# destroy data -- the three things that cannot be undone by reloading.
# Matched as whole words so "Resend" trips it but "Suspended" does not.
_CONSEQUENTIAL = (
    "send", "delete", "remove", "buy", "purchase", "pay", "order",
    "confirm", "submit", "publish", "post", "transfer", "withdraw",
    "accept", "agree", "subscribe", "unsubscribe", "discard", "archive",
)

_playwright: Any = None
_context: Any = None


class BrowserError(RuntimeError):
    """The browser could not be driven."""


def _browser():
    """The persistent browser context, started on first use.

    Kept at module level so a session survives between tool calls: step
    2 of a plan lands on the page step 1 opened. Starting a fresh
    browser per call would log the user out of everything, every time.
    """
    global _playwright, _context
    if _context is not None:
        return _context

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserError(
            "Playwright is not installed. Run: pip install playwright "
            "&& python -m playwright install chromium"
        ) from exc

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    # A persistent profile can only be held by one browser at a time.
    # Without this, a process that exits while its Chromium is still
    # running leaves the profile locked, and the next run dies with an
    # EPIPE from deep inside the Playwright driver -- an error that says
    # nothing about the actual cause.
    atexit.register(close_browser)

    try:
        _playwright = sync_playwright().start()
        _context = _playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,          # the user is meant to watch this
            no_viewport=True,
            args=["--start-maximized"],
            # Drops Chrome's "controlled by automated software" infobar,
            # which otherwise covers part of the page in every screenshot
            # and in anything the user is watching.
            ignore_default_args=["--enable-automation"],
        )
    except Exception as exc:
        _playwright = _context = None
        if "ProcessSingleton" in str(exc) or "already running" in str(exc).lower():
            raise BrowserError(
                "Another browser is already using the agent's profile. "
                "Close it and try again."
            ) from exc
        raise BrowserError(f"Could not start the browser: {exc}") from exc

    return _context


def _page():
    context = _browser()
    return context.pages[0] if context.pages else context.new_page()


def _is_consequential(target: str) -> str | None:
    """The dangerous word in this label, if there is one.

    Substring matching, not whole words. Whole words miss "Resend" and
    "Unsubscribe", and the two kinds of mistake are not symmetric: a
    false positive costs one human approval, while a false negative
    means something irreversible happens without anyone being asked.
    Erring toward refusal routes a click through the gate rather than
    blocking it, so over-matching is cheap.
    """
    lowered = target.lower()
    for word in _CONSEQUENTIAL:
        if word in lowered:
            return word
    return None


def _find(page, text: str):
    """Locate something by its visible text, trying the likely roles first.

    Buttons and links before raw text: on a real page the same words
    often appear both as a heading and as the control, and clicking the
    heading does nothing while looking like it worked.
    """
    attempts = [
        page.get_by_role("button", name=text, exact=False),
        page.get_by_role("link", name=text, exact=False),
        page.get_by_text(text, exact=False),
    ]
    for locator in attempts:
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    raise BrowserError(
        f"Nothing on the page matches {text!r}. Use browser_read to see "
        "what is actually there."
    )


@tool(risk=Risk.READ)
def browser_open(url: str) -> str:
    """Open a URL in the agent's browser, visibly on screen.

    The browser keeps its own profile, so sites stay logged in between
    runs. Returns the page title and address once it has loaded. Use
    browser_read afterwards to see the content.
    """
    if not url.strip():
        raise BrowserError("No URL given.")
    address = url.strip()
    if not address.startswith(("http://", "https://")):
        address = "https://" + address

    page = _page()
    try:
        page.goto(address, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)  # let client-rendered pages settle
    except Exception as exc:
        raise BrowserError(f"Could not open {address}: {exc}") from exc

    return f"Opened {page.url}\nTitle: {page.title()}"


@tool(risk=Risk.READ)
def browser_read(limit: int = 3000) -> str:
    """Return the visible text of the page currently open in the browser.

    Use this to see what is on screen before deciding what to click or
    type. Returns text only -- layout and images are lost.
    """
    page = _page()
    try:
        text = page.inner_text("body")
    except Exception as exc:
        raise BrowserError(f"Could not read the page: {exc}") from exc

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit] + f"\n... [{len(text) - limit} more characters]"
    return f"{page.url}\n\n{text}"


@tool(risk=Risk.WRITE)
def browser_type(field: str, text: str) -> str:
    """Type text into a field on the current page, identified by its label.

    `field` is the field's label, placeholder or name as it appears on
    screen, for example 'Search', 'To' or 'Subject'. Typing changes only
    what is on screen -- nothing is submitted until something is clicked.
    """
    page = _page()
    attempts = [
        lambda: page.get_by_label(field, exact=False),
        lambda: page.get_by_placeholder(field, exact=False),
        lambda: page.get_by_role("textbox", name=field, exact=False),
    ]
    for build in attempts:
        try:
            locator = build()
            if locator.count() > 0:
                locator.first.fill(text)
                return f"Typed {len(text)} characters into {field!r}."
        except Exception:
            continue
    raise BrowserError(
        f"No field matching {field!r}. Use browser_read to see the page."
    )


@tool(risk=Risk.WRITE)
def browser_click(target: str) -> str:
    """Click a button or link by its visible text. Refuses consequential clicks.

    Safe for navigation and opening things -- 'Compose', 'Next', 'Sign
    in', a link title. Anything that sends, deletes, buys, pays or
    submits is refused here and must use browser_click_confirmed, which
    asks the human first.
    """
    danger = _is_consequential(target)
    if danger:
        raise BrowserError(
            f"Refusing to click {target!r}: {danger!r} can do something that "
            "cannot be undone. Use browser_click_confirmed, which shows the "
            "human exactly what will be clicked and waits for approval."
        )

    page = _page()
    _find(page, target).click()
    page.wait_for_timeout(1200)
    return f"Clicked {target!r}. Now on: {page.title()}"


@tool(risk=Risk.IRREVERSIBLE)
def browser_click_confirmed(target: str) -> str:
    """Click something consequential -- send, delete, pay, submit. Needs approval.

    Use this only for the final, committing action. The human is shown
    the exact text to be clicked and must approve before it happens.
    """
    page = _page()
    _find(page, target).click()
    page.wait_for_timeout(2000)
    return f"Clicked {target!r}. Now on: {page.title()}"


@tool(risk=Risk.WRITE)
def gmail_compose(to: str, subject: str, body: str) -> str:
    """Open Gmail with a new message already filled in, ready to review. Does not send.

    Opens the real Gmail compose window on screen with the recipient,
    subject and body prepared. Nothing is sent -- the message sits there
    for a person to read. Sending afterwards needs
    browser_click_confirmed('Send'), which asks for approval first.
    """
    if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}", to.strip()):
        raise BrowserError(
            f"{to!r} is not a single valid email address. "
            "Give exactly one recipient."
        )

    # Gmail's own compose URL, rather than clicking through the interface
    # and hunting for fields. Gmail's DOM is generated and changes often,
    # so finding 'the To box' by selector is the least reliable thing we
    # could do; handing it a prefilled URL is the most. The window is
    # just as visible either way.
    query = urllib.parse.urlencode(
        {"view": "cm", "fs": "1", "to": to.strip(),
         "su": subject.strip(), "body": body}
    )
    page = _page()
    page.goto(f"https://mail.google.com/mail/?{query}",
              wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)

    if "accounts.google.com" in page.url or "signin" in page.url.lower():
        return (
            "Gmail opened but this browser profile is not signed in. "
            "Sign in once in the open window; the session is kept for "
            "later runs. Nothing has been composed."
        )

    return (
        f"Composed a message to {to.strip()} with subject {subject.strip()!r} "
        f"({len(body.split())} words). It is open in Gmail and has NOT been "
        "sent."
    )


def close_browser() -> None:
    """Shut the browser down. Only for tests and cleanup."""
    global _playwright, _context
    try:
        if _context is not None:
            _context.close()
        if _playwright is not None:
            _playwright.stop()
    except Exception:
        pass
    _context = _playwright = None

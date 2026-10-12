"""Driving any Windows application.

Everything else in `tools/` talks to a specific thing: Excel through COM,
the web through Playwright, the ledger through SQL. Those are reliable
precisely because they are specific, and useless for an application
nobody anticipated.

This is the fallback. It reads the accessibility tree that Windows
exposes for every window -- the same information a screen reader uses --
and clicks and types by control name. Notepad, a bank's desktop client,
an internal tool from 2009: if it draws a window, this can usually see
inside it.

It is also the least reliable layer in the project, and that ordering is
deliberate:

    an API            ~99%   openpyxl, sqlite
    COM automation    ~99%   Excel, Word, Outlook
    accessibility     ~85%   this file
    pixel coordinates ~65%   last resort, below

So the tool docstrings say "when no dedicated tool exists". The planner
reads those when choosing, and should reach for this only when it must.

Three safety decisions, because a tool that can drive any window can do
anything a person can:

  * A denylist of windows it will not touch at all -- the registry
    editor, terminals, the credential manager. Not configurable from a
    plan.
  * The same consequential-click guard as the browser: anything matching
    delete, send, pay, confirm is refused and must go through the
    IRREVERSIBLE variant, which stops for human approval.
  * It refuses to type into a password field, which Windows marks for us.
"""

from __future__ import annotations

import re
from typing import Any

from core.config import OUTPUTS_DIR
from core.schemas import Risk
from tools.registry import tool

# Windows the agent will not drive, whatever a plan says. These are the
# ones where a stray click is not a mistake but an incident: editing the
# registry, running shell commands, changing security settings, reading
# saved passwords.
_FORBIDDEN = (
    "registry editor", "regedit", "command prompt", "windows powershell",
    "powershell", "terminal", "task manager", "credential manager",
    "local security policy", "group policy", "services", "device manager",
    "control panel", "windows security", "defender", "bitlocker",
    "user account control", "disk management", "system configuration",
)

# Same list as the browser's, for the same reason: one click tool cannot
# have one risk tier, so the tier is decided by what is being clicked.
_CONSEQUENTIAL = (
    "send", "delete", "remove", "buy", "purchase", "pay", "order",
    "confirm", "submit", "publish", "post", "transfer", "withdraw",
    "accept", "agree", "install", "uninstall", "format", "erase",
    "discard", "shut down", "restart", "sign out", "log off",
)

MAX_CONTROLS = 120


class DesktopError(RuntimeError):
    """A window could not be driven."""


def _ready() -> None:
    """Prepare this thread for UI Automation.

    The UIA backend is COM underneath, so a thread that has not called
    CoInitialize fails with an error that mentions neither COM nor
    threads. The web server runs each request on a pool thread, so this
    matters here exactly as it did for Office.
    """
    try:
        import pythoncom

        pythoncom.CoInitialize()
    except Exception:
        pass


def _desktop():
    _ready()
    try:
        from pywinauto import Desktop
    except ImportError as exc:  # pragma: no cover - Windows only
        raise DesktopError(
            "pywinauto is not installed, so desktop control is unavailable."
        ) from exc
    return Desktop(backend="uia")


def _forbidden(title: str) -> str | None:
    lowered = title.lower()
    for name in _FORBIDDEN:
        if name in lowered:
            return name
    return None


def _consequential(label: str) -> str | None:
    lowered = label.lower()
    for word in _CONSEQUENTIAL:
        if word in lowered:
            return word
    return None


def _windows() -> list[Any]:
    """Visible top-level windows that have a title."""
    found = []
    for window in _desktop().windows():
        try:
            if window.is_visible() and window.window_text().strip():
                found.append(window)
        except Exception:
            continue
    return found


def _find_window(title: str):
    """The window whose title best matches, refusing the forbidden ones."""
    blocked = _forbidden(title)
    if blocked:
        raise DesktopError(
            f"Refusing to touch a window matching {blocked!r}. System and "
            "security windows are off limits to the agent."
        )

    wanted = title.strip().lower()
    candidates = _windows()

    for window in candidates:
        if window.window_text().strip().lower() == wanted:
            return window

    partial = [
        window
        for window in candidates
        if wanted in window.window_text().strip().lower()
        and not _forbidden(window.window_text())
    ]

    if len(partial) == 1:
        return partial[0]

    if len(partial) > 1:
        # Refused rather than resolved by picking the first. "Notepad"
        # matched a document the user already had open, and typing into
        # it replaced their work -- the agent had no idea it was not a
        # blank window. When several windows match, the plan has to say
        # which, exactly as an ambiguous date has to be resolved before
        # a record is filed.
        names = "; ".join(repr(w.window_text()[:50]) for w in partial[:8])
        raise DesktopError(
            f"{len(partial)} windows match {title!r}, and acting on the "
            f"wrong one could damage someone's work: {names}. Use the full "
            "title of the one you mean."
        )

    titles = ", ".join(repr(w.window_text()[:40]) for w in candidates[:8])
    raise DesktopError(
        f"No window matching {title!r}. Open ones include: {titles}. "
        "Use list_windows to see them all."
    )


def _controls(window) -> list[Any]:
    try:
        return window.descendants()[:MAX_CONTROLS]
    except Exception as exc:
        raise DesktopError(f"Could not read that window: {exc}") from exc


def _describe(control) -> str | None:
    """One readable line for a control, or None if it is not worth showing."""
    try:
        kind = control.friendly_class_name()
        text = (control.window_text() or "").strip()
    except Exception:
        return None
    if not text and kind not in ("Edit", "CheckBox", "ComboBox"):
        return None
    return f"{kind}: {text[:70]}" if text else f"{kind}: (empty)"


@tool(risk=Risk.READ)
def list_windows() -> str:
    """List the application windows currently open on screen.

    Use this when no dedicated tool exists for an application and you
    need to drive it directly. Prefer a dedicated tool when there is one
    -- excel_read for spreadsheets, read_pdf for PDFs -- because they are
    far more reliable than driving a window.
    """
    titles = []
    for window in _windows():
        text = window.window_text().strip()
        mark = "  [off limits]" if _forbidden(text) else ""
        titles.append(f"  {text[:80]}{mark}")

    if not titles:
        return "No application windows are open."
    return f"{len(titles)} window(s) open:\n" + "\n".join(titles)


@tool(risk=Risk.READ)
def read_window(title: str) -> str:
    """Read the contents of an application window: its buttons, fields and text.

    `title` is the window's title, or any distinctive part of it. Returns
    the controls Windows exposes, which is what you click and type into.
    Use this before acting, so you work from what is actually on screen
    rather than a guess.

    Note that a title can change as you work -- Notepad renames its
    window after the text it contains, and most editors add a marker
    when there are unsaved changes. If a title stops matching after an
    edit, call list_windows again rather than assuming the window is
    gone.
    """
    window = _find_window(title)
    window_text = window.window_text()

    lines = []
    for control in _controls(window):
        described = _describe(control)
        if described:
            lines.append(f"  {described}")

    if not lines:
        return (
            f"[{window_text}] exposes no readable controls. Some applications "
            "draw their own interface and are invisible to the accessibility "
            "tree; a screenshot may be the only way to see inside it."
        )
    return f"[{window_text}] {len(lines)} control(s):\n" + "\n".join(lines)


@tool(risk=Risk.WRITE)
def focus_window(title: str) -> str:
    """Bring an application window to the front so it can be worked with.

    Typing goes to whichever window has focus, so do this before typing
    into an application.
    """
    window = _find_window(title)
    try:
        window.set_focus()
    except Exception as exc:
        raise DesktopError(f"Could not bring that window forward: {exc}") from exc
    return f"Focused [{window.window_text()}]."


@tool(risk=Risk.WRITE)
def click_control(window_title: str, control_name: str) -> str:
    """Click a button, menu item or control in a window, by its visible name.

    Safe for navigating and opening things. Anything that sends, deletes,
    pays, installs or submits is refused here and must use
    click_control_confirmed, which asks the human first.
    """
    danger = _consequential(control_name)
    if danger:
        raise DesktopError(
            f"Refusing to click {control_name!r}: {danger!r} can do something "
            "that cannot be undone. Use click_control_confirmed, which shows "
            "the human exactly what will be clicked and waits for approval."
        )
    return _click(window_title, control_name)


@tool(risk=Risk.IRREVERSIBLE)
def click_control_confirmed(window_title: str, control_name: str) -> str:
    """Click something consequential in a window -- delete, send, install. Needs approval.

    Use this only for the final, committing action. The human is shown
    the window and the control before anything happens.
    """
    return _click(window_title, control_name)


def _click(window_title: str, control_name: str) -> str:
    window = _find_window(window_title)
    wanted = control_name.strip().lower()

    for control in _controls(window):
        try:
            if (control.window_text() or "").strip().lower() == wanted:
                control.set_focus()
                control.click_input()
                return f"Clicked {control_name!r} in [{window.window_text()}]."
        except Exception:
            continue

    for control in _controls(window):
        try:
            if wanted in (control.window_text() or "").strip().lower():
                control.set_focus()
                control.click_input()
                return (
                    f"Clicked {control.window_text()!r} in "
                    f"[{window.window_text()}]."
                )
        except Exception:
            continue

    raise DesktopError(
        f"Nothing named {control_name!r} in that window. Use read_window to "
        "see what is actually there."
    )


@tool(risk=Risk.WRITE)
def type_into(window_title: str, field_name: str, text: str) -> str:
    """Type text into a named field of a window.

    `field_name` is the field's label or current contents, as read_window
    shows it. Refuses password fields -- the agent must never enter
    credentials, and Windows marks those fields for us.
    """
    window = _find_window(window_title)
    wanted = field_name.strip().lower()

    for control in _controls(window):
        try:
            label = (control.window_text() or "").strip().lower()
            if wanted and wanted not in label:
                continue
            if control.friendly_class_name() not in ("Edit", "Document", "ComboBox"):
                continue

            # Windows marks password boxes, and that is the one field the
            # agent must never fill. Checked here rather than asked for in
            # a prompt, because a prompt is a request and this is a rule.
            try:
                if control.legacy_properties().get("Value") is None and getattr(
                    control.element_info.element, "CurrentIsPassword", False
                ):
                    raise DesktopError(
                        "Refusing to type into a password field. Credentials "
                        "are for the person to enter, never the agent."
                    )
            except DesktopError:
                raise
            except Exception:
                pass

            control.set_focus()
            try:
                control.set_edit_text(text)
            except Exception:
                from pywinauto.keyboard import send_keys

                send_keys("^a{BACKSPACE}", pause=0.02)
                send_keys(_escape(text), pause=0.01, with_spaces=True)
            return f"Typed {len(text)} characters into {field_name!r}."
        except DesktopError:
            raise
        except Exception:
            continue

    raise DesktopError(
        f"No editable field matching {field_name!r}. Use read_window to see "
        "what the window exposes."
    )


@tool(risk=Risk.WRITE)
def press_keys(keys: str) -> str:
    """Send a keyboard shortcut to whatever window has focus.

    Use pywinauto notation: '^s' for Ctrl+S, '%{F4}' for Alt+F4, '{ENTER}',
    '{TAB}'. Focus the window first. Prefer clicking a named control when
    there is one -- a shortcut goes wherever focus happens to be.
    """
    _ready()
    from pywinauto.keyboard import send_keys

    try:
        send_keys(keys, pause=0.03)
    except Exception as exc:
        raise DesktopError(f"Could not send {keys!r}: {exc}") from exc
    return f"Sent {keys!r} to the focused window."


@tool(risk=Risk.READ)
def screenshot(label: str = "screen") -> str:
    """Capture the screen to an image file and return its path.

    Use this when a window exposes no readable controls, or to record
    what the screen looked like at a point in a task. The image is saved
    in the outputs folder.
    """
    try:
        import mss
    except ImportError as exc:
        raise DesktopError("mss is not installed, so screenshots are unavailable.") from exc

    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", label.strip() or "screen")[:40]
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    target = OUTPUTS_DIR / f"{safe}.png"

    with mss.mss() as capture:
        image = capture.grab(capture.monitors[0])
        import mss.tools

        mss.tools.to_png(image.rgb, image.size, output=str(target))

    return f"Saved a screenshot to outputs/{target.name} ({image.width}x{image.height})."


def _escape(text: str) -> str:
    """Escape characters that send_keys would otherwise treat as commands."""
    for character in "^%+~(){}":
        text = text.replace(character, "{" + character + "}")
    return text


# Applications the agent may start. A denylist rather than an allowlist,
# because the point of this layer is reaching programs nobody
# anticipated -- but a shell is not an application in that sense, it is
# a way around every other guard in this project.
_UNLAUNCHABLE = (
    "cmd", "powershell", "pwsh", "wt", "terminal", "regedit", "mmc",
    "taskmgr", "gpedit", "secpol", "lusrmgr", "compmgmt", "diskmgmt",
    "msconfig", "control", "rundll32", "wscript", "cscript", "bash",
)


@tool(risk=Risk.WRITE)
def open_application(name: str) -> str:
    """Start an application by name, for example 'notepad', 'calc' or 'winword'.

    Use this when the task needs a program that is not already open. Give
    the executable's name without a path. Wait and then call list_windows
    to find the new window -- applications take a moment to appear, and
    the title is often not what you expect.

    Shells and system tools cannot be started this way.
    """
    import shutil
    import subprocess

    wanted = name.strip().lower().removesuffix(".exe")
    if not wanted:
        raise DesktopError("No application name given.")

    if any(blocked == wanted or blocked in wanted.split() for blocked in _UNLAUNCHABLE):
        raise DesktopError(
            f"Refusing to start {name!r}. Shells and system tools are off "
            "limits -- a command prompt would route around every other "
            "safeguard the agent has."
        )

    if shutil.which(wanted) is None and shutil.which(wanted + ".exe") is None:
        raise DesktopError(
            f"No application called {name!r} was found on this machine. "
            "Use the executable's name, like 'notepad' or 'winword'."
        )

    try:
        subprocess.Popen([wanted], shell=False)
    except Exception as exc:
        raise DesktopError(f"Could not start {name!r}: {exc}") from exc

    return (
        f"Started {wanted}. Give it a moment, then use list_windows to find "
        "its window -- the title is often not the application's name."
    )

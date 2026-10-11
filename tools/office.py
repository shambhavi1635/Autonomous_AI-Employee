"""Driving Microsoft Office through COM.

This is the interesting layer. There are three ways to make an agent
"use an application", and the usual trade-off is that the visible one is
unreliable and the reliable one is invisible:

    1. write the file directly (openpyxl)   reliable, nothing on screen
    2. automate the app (COM)               reliable AND on screen
    3. click at pixel coordinates           visible, and wrong ~1 time in 3

COM collapses that trade-off on Windows. Excel genuinely opens, the user
watches the cells change, and every instruction is an API call rather
than a guess about where a button is. `Visible = True` is the whole
point: the demo requirement was to *see* the work happen, and this is
the only way to get that without betting the task on coordinates.

What this costs: Office must be installed, and it is Windows-only. The
headless libraries stay the fallback for a machine without Office --
which is why the error says so rather than just failing.

A note on state. Each tool opens the file, does its work and leaves the
application open on screen. That is deliberate: the user is watching, and
an agent that opened and closed Excel between every step would be
useless to watch and slower besides.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.schemas import Risk
from tools.files import FileAccessError, _resolve
from tools.registry import tool

# Excel will show a modal dialog -- and hang forever, waiting for a click
# nobody is going to make -- if it wants to ask about links, overwriting
# or recovered files. Every app we open gets these switched off.
_QUIET = {"Visible": True, "DisplayAlerts": False}


class OfficeError(RuntimeError):
    """An Office application could not be driven."""


def _app(prog_id: str):
    """Get a running Office application, or start one.

    CoInitialize matters more than it looks. Streamlit runs this code in
    a worker thread, and COM must be initialised per thread -- without
    it the first call fails with a bewildering "CoInitialize has not
    been called" from deep inside pywin32.
    """
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:  # pragma: no cover - Windows only
        raise OfficeError(
            "pywin32 is not available, so Office automation cannot run. "
            "Use the file tools instead."
        ) from exc

    try:
        pythoncom.CoInitialize()
    except Exception:
        pass  # already initialised on this thread

    try:
        # GetActiveObject reuses the window the user already has open,
        # rather than starting a second copy that fights over the file.
        try:
            app = win32com.client.GetActiveObject(prog_id)
        except Exception:
            app = win32com.client.Dispatch(prog_id)
    except Exception as exc:
        raise OfficeError(
            f"Could not start {prog_id}. Is Microsoft Office installed? "
            f"({type(exc).__name__}: {exc})"
        ) from exc

    for setting, value in _QUIET.items():
        try:
            setattr(app, setting, value)
        except Exception:
            pass  # not every app exposes every setting
    return app


def _open_workbook(app, path: Path):
    """Return the workbook for `path`, reusing it if already open.

    The awkward case is a second Excel process. COM attaches to one
    instance, and if the file is open in a different one, Excel will not
    open it again -- Workbooks.Open returns None rather than raising,
    and the next attribute access fails with a bare AttributeError that
    says nothing about the cause.

    Worse, that left two tools pointing at each other: excel_update
    failed on the None, repair switched to write_file, which failed
    because the file was locked and suggested excel_update. The error
    has to offer a way out that does not involve either of them, so it
    names writing to outputs/ instead.
    """
    target = str(path)
    for book in app.Workbooks:
        try:
            if Path(book.FullName).resolve() == path.resolve():
                return book
        except Exception:
            continue

    if path.exists():
        book = app.Workbooks.Open(target)
        if book is None:
            raise OfficeError(
                f"{path.name} is open in a different Excel window, which "
                "prevents this one from opening it. Either close that "
                "window and try again, or write the result to a new file "
                f"under outputs/ instead of changing {path.name} in place."
            )
        return book

    book = app.Workbooks.Add()
    book.SaveAs(target)
    return book


def _sheet(book, name: str):
    if not name.strip():
        return book.Worksheets(1)
    for sheet in book.Worksheets:
        if sheet.Name.lower() == name.strip().lower():
            return sheet
    available = ", ".join(s.Name for s in book.Worksheets)
    raise OfficeError(f"No sheet named {name!r}. Available: {available}")


@tool(risk=Risk.READ)
def excel_read(path: str, sheet: str = "") -> str:
    """Open a spreadsheet in Excel on screen and return its contents as text.

    Works on .xlsx and .csv. Opens the real Excel window so the user can
    see it. Leave `sheet` empty for the first one. Rows come back one per
    line, cells separated by ' | ', with a header line naming the sheet.
    """
    resolved = _resolve(path, must_exist=True)
    app = _app("Excel.Application")
    book = _open_workbook(app, resolved)
    worksheet = _sheet(book, sheet)

    used = worksheet.UsedRange
    values = used.Value
    if values is None:
        return f"[{worksheet.Name}] is empty"

    # A single cell comes back as a bare value, a single row as a flat
    # tuple; only a true 2-D range is a tuple of tuples.
    if not isinstance(values, tuple):
        rows: Any = ((values,),)
    elif values and not isinstance(values[0], tuple):
        rows = (values,)
    else:
        rows = values

    lines = [f"[{worksheet.Name}] {len(rows)} rows"]
    for row in rows:
        lines.append(" | ".join("" if cell is None else str(cell) for cell in row))
    return "\n".join(lines)


def _parse_changes(text: str) -> dict[str, Any]:
    """Parse "E2=approved; E4=1250.50" into {cell: value}.

    Deliberately not JSON. The first version of this tool took a
    changes_json string, which meant the model had to produce JSON
    nested inside a JSON argument -- double-encoded, escaped twice, and
    wrong often enough to fail the task. A flat `CELL=value` list has no
    nesting to get wrong.

    Values that look numeric become numbers, so a total lands in Excel
    as a number rather than text that silently breaks SUM().
    """
    changes: dict[str, Any] = {}
    for part in text.replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise OfficeError(
                f"Could not read {part!r}. Use CELL=value, "
                'for example "E2=approved; F3=1250.50".'
            )
        cell, _, value = part.partition("=")
        cell, value = cell.strip().upper(), value.strip().strip('"').strip("'")
        try:
            changes[cell] = float(value) if "." in value else int(value)
        except ValueError:
            changes[cell] = value
    return changes


@tool(risk=Risk.WRITE)
def excel_update(path: str, changes: str, sheet: str = "") -> str:
    """Change cells in a spreadsheet and save it, with Excel visible on screen.

    `changes` is a list of CELL=value assignments separated by semicolons,
    for example "E2=approved; E3=rejected; F5=1250.50". Values that look
    like numbers are written as numbers. The file is saved immediately.
    Use excel_read first to see which cells you need.
    """
    resolved = _resolve(path, must_exist=False)

    parsed = _parse_changes(changes)
    if not parsed:
        raise OfficeError('No changes given. Use "E2=approved; E3=rejected".')
    changes = parsed  # type: ignore[assignment]

    app = _app("Excel.Application")
    book = _open_workbook(app, resolved)
    worksheet = _sheet(book, sheet)

    applied = []
    for cell, value in changes.items():
        try:
            worksheet.Range(cell).Value = value
        except Exception as exc:
            raise OfficeError(f"Could not set {cell!r}: {exc}") from exc
        applied.append(f"{cell}={value!r}")

    book.Save()
    return (
        f"Updated {len(applied)} cell(s) in [{worksheet.Name}] of "
        f"{resolved.name}: {', '.join(applied)}"
    )


@tool(risk=Risk.READ)
def word_read(path: str) -> str:
    """Open a Word document on screen and return its text.

    Works on .docx and .doc. The real Word window opens so the user can
    see the document being read.
    """
    resolved = _resolve(path, must_exist=True)
    app = _app("Word.Application")
    document = app.Documents.Open(str(resolved))
    return document.Content.Text


@tool(risk=Risk.WRITE)
def word_write(path: str, text: str, heading: str = "") -> str:
    """Create or replace a Word document with the given text, visibly in Word.

    Writes a .docx the user can watch being produced. `heading` is
    optional and becomes a styled title at the top. Paragraphs are split
    on blank lines. Replaces the whole document if it already exists.
    """
    resolved = _resolve(path, must_exist=False)
    if resolved.suffix.lower() not in (".docx", ".doc"):
        raise FileAccessError(f"{path!r} is not a Word document (.docx).")

    app = _app("Word.Application")
    document = (
        app.Documents.Open(str(resolved))
        if resolved.exists()
        else app.Documents.Add()
    )
    document.Content.Delete()

    # InsertAfter, not Paragraphs.Add() plus Range.Text. A new paragraph's
    # range includes the paragraph mark, so assigning to its text swallows
    # what came before it -- a four-paragraph document came back as its
    # last line only. InsertAfter appends and leaves earlier content alone.
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]

    if heading.strip():
        document.Content.InsertAfter(heading.strip() + "\r")
        try:
            document.Paragraphs(1).Range.Style = document.Styles("Heading 1")
        except Exception:
            document.Paragraphs(1).Range.Bold = True

    for block in blocks:
        document.Content.InsertAfter(block + "\r")

    document.SaveAs(str(resolved))
    words = len(text.split())
    return f"Wrote {words} words to {resolved.name} and saved it."

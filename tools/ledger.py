"""The invoice ledger: a stand-in for the company's internal system.

The problem statement assumes somewhere to file an invoice once it has
been read. There is no such system here, so we build a small one. It is
deliberately real -- a SQLite table with constraints, not a Python list --
because the point of this use case is that the agent's work can be
independently checked afterwards. A record either exists in the database
or it does not, and `find_invoice` reads it back from disk rather than
from anything the agent said.

Three tools:

    add_invoice     file one invoice                     [WRITE]
    find_invoice    look one up by number                [READ]
    list_invoices   browse, optionally filtered by vendor [READ]

The two READ tools exist for verification as much as for planning. A
postcondition like "find_invoice returns the Acme invoice" is only
checkable because the agent can look the record back up.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from typing import Any

from core.config import LEDGER_DB, OUTPUTS_DIR, ensure_dirs
from core.schemas import Risk
from tools.registry import tool
from tools.source_check import (
    SourceMismatch,
    check_amount,
    check_date,
    check_text,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor         TEXT    NOT NULL,
    invoice_number TEXT    NOT NULL UNIQUE,
    invoice_date   TEXT    NOT NULL,
    total_amount   REAL    NOT NULL,
    currency       TEXT    NOT NULL,
    filed_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


# A human-readable mirror of the ledger, rewritten after every change.
# SQLite is the real store -- it enforces the unique invoice numbers and
# the date format that make the agent's work checkable -- but a binary
# file cannot be shown to anyone. This mirror opens in Excel, so "here is
# the system before, here it is after" needs no special software.
#
# One-way on purpose: the agent never reads it, and editing it changes
# nothing. A second writable copy of the same data is how two sources of
# truth start disagreeing.
LEDGER_CSV = OUTPUTS_DIR / "ledger_view.csv"


class LedgerError(RuntimeError):
    """A ledger operation was refused or failed."""


def _require_iso_date(value: str) -> None:
    """Reject anything that is not an unambiguous YYYY-MM-DD date."""
    try:
        datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        raise LedgerError(
            f"invoice_date must be ISO format YYYY-MM-DD, got {value!r}. "
            "Convert it first: '02/10/2026' is ambiguous, but if the "
            "document is Indian or European it means 2026-10-02, and if "
            "American it means 2026-02-10. Decide from the document."
        ) from None


def _connect() -> sqlite3.Connection:
    """Open the ledger, creating it on first use."""
    ensure_dirs()
    conn = sqlite3.connect(LEDGER_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


@tool(risk=Risk.WRITE)
def add_invoice(
    vendor: str,
    invoice_number: str,
    invoice_date: str,
    total_amount: float,
    currency: str = "USD",
    source_text: str = "",
) -> str:
    """File an invoice. Pass source_text so the values are checked against the document.

    invoice_date must be ISO YYYY-MM-DD. Duplicate invoice numbers are
    refused, so re-running a task cannot file the same invoice twice.

    Always pass `source_text`: the text the details were read from, as
    returned by read_pdf or read_file. The vendor, invoice number, amount
    and date are then checked against it, and the filing is refused if
    they do not match. Without it the values are taken on trust.
    """
    if total_amount <= 0:
        raise LedgerError(f"total_amount must be positive, got {total_amount}")
    if not invoice_number.strip():
        raise LedgerError("invoice_number cannot be empty")

    # Enforced here rather than requested in the prompt. A date like
    # "02/10/2026" is genuinely ambiguous -- 2 October or 10 February,
    # depending on the country the document came from -- so a tool that
    # guessed would quietly file wrong records that look fine. Refusing
    # pushes the decision back to the step that can still see the
    # document, and the error says exactly what is wrong, so the repair
    # ladder can act on it.
    _require_iso_date(invoice_date)

    # Checked in Python, not by asking a model whether it matches. An
    # invoice was once filed with the date read the wrong way round and
    # every check passed, because each check asked whether a record
    # existed rather than whether it was right.
    if source_text.strip():
        try:
            check_text("vendor", vendor, source_text)
            check_text("invoice_number", invoice_number, source_text)
            check_amount(float(total_amount), source_text)
            check_date(invoice_date.strip(), source_text)
        except SourceMismatch as exc:
            raise LedgerError(str(exc)) from exc

    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO invoices "
                "(vendor, invoice_number, invoice_date, total_amount, currency) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    vendor.strip(),
                    invoice_number.strip(),
                    invoice_date.strip(),
                    float(total_amount),
                    currency.strip().upper(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            # A duplicate is a business rule, not a crash. The message
            # names the conflict so the agent can report it to the human
            # rather than retrying the same insert forever.
            existing = find_invoice(invoice_number)
            raise LedgerError(
                f"Invoice {invoice_number!r} is already in the ledger: {existing}"
            ) from exc

    _export_csv()

    return (
        f"Filed invoice {invoice_number} from {vendor}: "
        f"{currency.upper()} {float(total_amount):,.2f} dated {invoice_date}"
    )


def _export_csv() -> None:
    """Rewrite the human-readable mirror. Never fails the write it follows."""
    try:
        rows = list_invoices()
        LEDGER_CSV.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER_CSV, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["vendor", "invoice_number", "invoice_date",
                 "total_amount", "currency", "filed_at"]
            )
            for row in rows:
                writer.writerow([row[c] for c in (
                    "vendor", "invoice_number", "invoice_date",
                    "total_amount", "currency", "filed_at")])
    except Exception:
        # A failed mirror must not fail a successful filing. The ledger
        # is the record; this is a convenience.
        pass


@tool(risk=Risk.READ)
def find_invoice(invoice_number: str) -> dict[str, Any] | str:
    """Look up one invoice in the ledger by its invoice number.

    Returns the stored record, or a message saying it is not there. Use
    this to check whether an invoice has already been filed, and to
    confirm afterwards that filing worked.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM invoices WHERE invoice_number = ?",
            (invoice_number.strip(),),
        ).fetchone()

    if row is None:
        return f"No invoice numbered {invoice_number!r} is in the ledger."
    return dict(row)


@tool(risk=Risk.READ)
def list_invoices(vendor: str = "") -> list[dict[str, Any]]:
    """List invoices in the ledger, newest first, optionally by vendor.

    Leave vendor empty to list everything. Vendor matching is partial and
    case-insensitive, so 'acme' finds 'ACME CORPORATION'.
    """
    with _connect() as conn:
        if vendor.strip():
            rows = conn.execute(
                "SELECT * FROM invoices WHERE vendor LIKE ? ORDER BY id DESC",
                (f"%{vendor.strip()}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM invoices ORDER BY id DESC"
            ).fetchall()

    return [dict(row) for row in rows]


# Fields a correction may touch. invoice_number is excluded on purpose:
# it is the identity of the record and the thing the UNIQUE constraint
# protects, so "changing" it is really filing a different invoice. id
# and filed_at are the ledger's own bookkeeping, not business data.
EDITABLE_FIELDS = ("vendor", "invoice_date", "total_amount", "currency")


@tool(risk=Risk.WRITE)
def update_invoice(invoice_number: str, field: str, value: str) -> str:
    """Correct one field of an invoice already in the ledger.

    field must be one of: vendor, invoice_date, total_amount, currency.
    Dates must be ISO YYYY-MM-DD and amounts must be positive, the same
    rules that apply when filing. The invoice number itself cannot be
    changed -- that identifies the record.

    Returns what the value was and what it now is, so the change is
    visible rather than silent.
    """
    column = field.strip().lower()
    if column not in EDITABLE_FIELDS:
        raise LedgerError(
            f"{field!r} cannot be changed. Editable fields: "
            f"{', '.join(EDITABLE_FIELDS)}."
        )

    existing = find_invoice(invoice_number)
    if isinstance(existing, str):
        raise LedgerError(existing)

    # The same validation as filing. A correction that bypassed the rules
    # would be a back door into the ledger: refuse an ambiguous date on
    # the way in, accept it on the way through update_invoice.
    cleaned: object = value.strip()
    if column == "invoice_date":
        _require_iso_date(value)
    elif column == "total_amount":
        try:
            cleaned = float(value)
        except ValueError:
            raise LedgerError(f"total_amount must be a number, got {value!r}") from None
        if cleaned <= 0:
            raise LedgerError(f"total_amount must be positive, got {cleaned}")
    elif column == "currency":
        cleaned = value.strip().upper()

    before = existing[column]
    with _connect() as conn:
        conn.execute(
            f"UPDATE invoices SET {column} = ? WHERE invoice_number = ?",
            (cleaned, invoice_number.strip()),
        )
    _export_csv()

    return (
        f"Invoice {invoice_number.strip()}: {column} changed from "
        f"{before!r} to {cleaned!r}."
    )


@tool(risk=Risk.IRREVERSIBLE)
def delete_invoice(invoice_number: str) -> str:
    """Remove an invoice from the ledger permanently. Always needs approval.

    There is no undo and no archive -- the row is gone. Use
    update_invoice to correct a mistake; use this only when a record
    should never have existed at all.
    """
    existing = find_invoice(invoice_number)
    if isinstance(existing, str):
        raise LedgerError(existing)

    with _connect() as conn:
        conn.execute(
            "DELETE FROM invoices WHERE invoice_number = ?",
            (invoice_number.strip(),),
        )
    _export_csv()

    return (
        f"Deleted invoice {existing['invoice_number']} from "
        f"{existing['vendor']} ({existing['currency']} "
        f"{existing['total_amount']:,.2f}, dated {existing['invoice_date']})."
    )

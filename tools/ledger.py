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

import sqlite3
from datetime import datetime
from typing import Any

from core.config import LEDGER_DB, ensure_dirs
from core.schemas import Risk
from tools.registry import tool

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
) -> str:
    """File an invoice. invoice_date MUST be ISO YYYY-MM-DD; invoice numbers must be unique.

    Use this once the invoice details have been extracted. Documents write
    dates in many formats -- convert to ISO before calling. Duplicate
    invoice numbers are refused, so re-running a task cannot file the same
    invoice twice.
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

    return (
        f"Filed invoice {invoice_number} from {vendor}: "
        f"{currency.upper()} {float(total_amount):,.2f} dated {invoice_date}"
    )


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
